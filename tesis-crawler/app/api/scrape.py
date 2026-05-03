import asyncio
import math
import traceback
from datetime import datetime
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field, HttpUrl, field_validator
from app.tasks.worker import CrawlWorker
from app.auth.service import link_user_to_source, upsert_user_from_clerk_event
from app.core.domain_utils import domain_variants, normalize_domain
from app.core.job_manager import job_manager
from app.core.scraping_service import ScrapingService
from app.core.ingestion_service import IngestionService
from app.core.page_classifier import PageClassifier
from app.storage.db_client import async_session
from sqlalchemy import text

router = APIRouter(tags=["Scrape"])


class ScrapeRequest(BaseModel):
    url: HttpUrl
    max_pages: int = 100
    concurrency: int = Field(default=20, ge=1, le=50)
    max_depth: int = Field(default=5, ge=1, le=10)
    persist_to_db: bool = True
    save_markdown_files: bool = False
    use_allow_filter: bool = True
    min_content_words: int = Field(default=5, ge=1, le=200)
    count_valid_pages_only: bool = True
    block_old_years: bool = True
    # Stage 1: opt-in. BFS discovery surfaces more URLs than the sitemap on
    # WordPress sites that don't list every page; sitemap-first is faster
    # but has lower recall. Keep BFS as the default for quality.
    use_sitemap_seed: bool = False
    clerk_user_id: str | None = None

    @field_validator("url")
    @classmethod
    def validate_https_url(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme.lower() != "https":
            raise ValueError("La URL debe usar HTTPS")
        return value


class QuickRefreshRequest(BaseModel):
    url: HttpUrl
    clerk_user_id: str | None = None
    include_pdf_links: bool = Field(default=False)

    @field_validator("url")
    @classmethod
    def validate_https_url(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme.lower() != "https":
            raise ValueError("La URL debe usar HTTPS")
        return value


# ── Progress ranges ──
# Phase A  (BFS crawling, simulated):    0 % →  60 %
# Phase B  (HTML processing + saving):  60 % →  99 %
# 100 % when HTML processing is done (PDFs continue silently).
_PHASE_A_MAX = 60.0
_PHASE_B_START = 60.0
_PHASE_B_RANGE = 39.0  # 60 → 99


def _safe_progress(value: float) -> float:
    return max(0.0, min(100.0, round(value, 2)))


def _safe_incomplete_progress(value: float) -> float:
    # Never report 100 % until the job is truly completed.
    return min(99.4, _safe_progress(value))


def _simulated_crawl_progress(elapsed_seconds: float) -> float:
    """Time-based simulated progress, capped at _PHASE_A_MAX (60 %)."""
    t = max(0.0, float(elapsed_seconds))
    if t <= 15:
        pct = 2.0 + (t / 15.0) * 6.0        # 2 → 8
    elif t <= 45:
        pct = 8.0 + ((t - 15.0) / 30.0) * 7.0   # 8 → 15
    elif t <= 90:
        pct = 15.0 + ((t - 45.0) / 45.0) * 10.0  # 15 → 25
    elif t <= 150:
        pct = 25.0 + ((t - 90.0) / 60.0) * 10.0   # 25 → 35
    elif t <= 240:
        pct = 35.0 + ((t - 150.0) / 90.0) * 12.0  # 35 → 47
    elif t <= 360:
        pct = 47.0 + ((t - 240.0) / 120.0) * 8.0  # 47 → 55
    else:
        # After 6 min, slowly approach 58 % but never pass 60 %
        pct = min(58.0, 55.0 + ((t - 360.0) / 180.0) * 3.0)
    return _safe_progress(min(pct, _PHASE_A_MAX))


def _estimate_real_progress(
    metrics: dict,
    persist_to_db: bool,
) -> tuple[float, str, str]:
    """
    Phase B (60 % → 99 %): based on HTML result processing only.
    PDF processing happens silently in the background.
    """
    total_results = int(metrics.get("total_results", 0))
    processed = int(metrics.get("processed_results", 0))
    saved_docs = int(metrics.get("saved_docs", 0))
    accepted = int(metrics.get("accepted_valid_pages", 0))
    ingest_pending = int(metrics.get("ingest_pending_total", 0))

    # If we haven't started processing yet, stay at 60 %.
    if total_results == 0 and processed == 0:
        return _safe_incomplete_progress(_PHASE_B_START), "rastreando", "Explorando enlaces y descargando páginas"

    if total_results <= 0:
        # Unknown total scope while deep crawl is still discovering URLs:
        # grow progressively but avoid jumping to 99%.
        growth = min(30.0, math.log1p(max(0, processed)) * 8.0)
        pct = _PHASE_B_START + growth
        if ingest_pending > 0:
            pct = min(pct, 95.0)
        else:
            pct = min(pct, 97.0)
    else:
        ratio = min(1.0, processed / max(1, total_results))
        pct = _PHASE_B_START + ratio * _PHASE_B_RANGE
        if ingest_pending > 0:
            pct = min(pct, 97.0)

    phase = "procesando"
    message = f"Analizando y guardando ({saved_docs} docs guardados, {accepted} páginas válidas)"

    return _safe_incomplete_progress(pct), phase, message


def _estimate_eta(
    metrics: dict,
    processing_elapsed_seconds: float,
) -> int | None:
    """
    ETA based on HTML processing rate only.
    Returns seconds remaining, or None if no reliable estimate yet.
    """
    if processing_elapsed_seconds < 3.0:
        return None

    total_results = int(metrics.get("total_results", 0))
    processed = int(metrics.get("processed_results", 0))

    total_work = max(1, total_results)
    done_work = processed

    if done_work < 3:
        return None

    rate = done_work / processing_elapsed_seconds
    if rate <= 0:
        return None

    remaining = max(0, total_work - done_work)
    eta = int(remaining / rate)

    # Sanity cap: never show more than 60 min
    return min(eta, 3600)


async def run_scrape_task(
    job_id: str,
    url: str,
    max_pages: int,
    concurrency: int,
    max_depth: int,
    persist_to_db: bool,
    save_markdown_files: bool,
    use_allow_filter: bool,
    min_content_words: int,
    count_valid_pages_only: bool,
    block_old_years: bool,
    use_sitemap_seed: bool = False,
    clerk_user_id: str | None = None,
):
    worker = CrawlWorker()
    debug_output_dir = f"./crawl_debug/{job_id}"
    started_at = datetime.now()
    first_metrics_seen = False
    processing_started_at: datetime | None = None
    job_marked_complete = False

    # Ensure source exists from the beginning so /api/sources/lookup works
    # while the crawl is still running (before first document is ingested).
    parsed_source = urlparse(url)
    source_domain = normalize_domain(parsed_source.netloc or "")
    if source_domain:
        variants = sorted(domain_variants(source_domain))
        if variants:
            async with async_session() as session:
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT source_id::text AS source_id
                            FROM sources
                            WHERE lower(domain) = :domain1 OR lower(domain) = :domain2
                            LIMIT 1
                            """
                        ),
                        {"domain1": variants[0], "domain2": variants[1]},
                    )
                ).mappings().first()
                if row is None:
                    await session.execute(
                        text(
                            """
                            INSERT INTO sources (source_id, domain, created_at)
                            VALUES (CAST(:source_id AS uuid), :domain, NOW())
                            ON CONFLICT (domain) DO NOTHING
                            """
                        ),
                        {"source_id": str(uuid4()), "domain": source_domain},
                    )
                    await session.commit()

    job_manager.update_job(
        job_id,
        status="running",
        phase="iniciando",
        message="Iniciando scraping",
        progress_pct=2.0,
        eta_seconds=None,
        errors=[],
        finished_at=None,
        pages_crawled=0,
        metrics={
            "total_results": 0,
            "processed_results": 0,
            "successful_results": 0,
            "saved_docs": 0,
            "saved_markdown_files": 0,
            "skipped_invalid_content": 0,
            "skipped_ingestion": 0,
            "skipped_save_markdown": 0,
            "skipped_processing_errors": 0,
            "skipped_db_disabled": 0,
            "blocked_by_host_filter": 0,
            "blocked_by_allow_filter": 0,
            "matched_allow_filter": 0,
            "blocked_by_block_filter": 0,
            "ingest_workers": 0,
            "ingest_queue_size": 0,
            "ingest_queue_maxsize": 0,
            "ingest_inflight": 0,
            "ingest_pending_total": 0,
        },
    )

    async def simulated_progress_pulse():
        """Phase A: simulated progress while BFS crawl runs (0 % → 60 % max)."""
        while True:
            await asyncio.sleep(1)
            job = job_manager.get_job(job_id)
            if not job or job.status != "running":
                return
            if first_metrics_seen:
                return  # Stop the pulse; Phase B takes over
            elapsed = (datetime.now() - started_at).total_seconds()
            pct = _safe_incomplete_progress(_simulated_crawl_progress(elapsed))
            remaining_discovery = max(0.0, (_PHASE_A_MAX - min(pct, _PHASE_A_MAX)) / _PHASE_A_MAX)
            eta_discovery = int(remaining_discovery * 360)  # up to ~6 min
            eta_processing_buffer = 120  # ~2 min post-discovery buffer
            eta = max(30, eta_discovery + eta_processing_buffer)
            job_manager.update_job(
                job_id,
                phase="rastreando",
                message="Explorando enlaces y descargando páginas",
                progress_pct=pct,
                eta_seconds=eta,
            )

    def on_progress(metrics: dict):
        nonlocal first_metrics_seen, processing_started_at, job_marked_complete

        # Once we've marked the job as completed, ignore all subsequent
        # progress updates (e.g. from PDF processing in the background).
        if job_marked_complete:
            return

        if not first_metrics_seen:
            first_metrics_seen = True
            processing_started_at = datetime.now()

        finished_reason = str(metrics.get("finished_reason", "running"))

        # When HTML processing is done, mark job completed immediately.
        # PDFs will continue processing silently in the background.
        if finished_reason != "running":
            job_marked_complete = True
            saved_docs = int(metrics.get("saved_docs", 0))
            accepted = int(metrics.get("accepted_valid_pages", 0))
            job_manager.update_job(
                job_id,
                status="completed",
                phase="completado",
                message=f"Scraping finalizado ({saved_docs} docs guardados, {accepted} páginas válidas)",
                progress_pct=100.0,
                eta_seconds=0,
                finished_at=datetime.now(),
                pages_crawled=metrics.get(
                    "processed_results",
                    metrics.get("successful_results", metrics.get("accepted_valid_pages", 0)),
                ),
                metrics=metrics,
            )
            return

        # Phase B: real progress (60 % → 99 %)
        pct, phase, message = _estimate_real_progress(
            metrics, persist_to_db=persist_to_db,
        )

        # Ensure progress never goes backwards
        current_job = job_manager.get_job(job_id)
        current_pct = 0.0
        if current_job is not None:
            current_pct = float(current_job.progress_pct or 0.0)
        pct = max(pct, current_pct)
        pct = _safe_incomplete_progress(pct)

        # ETA based on actual processing rate
        proc_elapsed = 0.0
        if processing_started_at is not None:
            proc_elapsed = max(0.0, (datetime.now() - processing_started_at).total_seconds())
        eta = _estimate_eta(metrics, proc_elapsed)
        if eta is None:
            elapsed_total = max(1.0, (datetime.now() - started_at).total_seconds())
            if pct >= 2.0:
                projected_total = elapsed_total / max(0.02, pct / 100.0)
                eta = int(max(0.0, projected_total - elapsed_total))
            else:
                eta = 300
        if eta <= 0 and int(metrics.get("ingest_pending_total", 0) or 0) > 0:
            eta = max(15, int(metrics.get("ingest_pending_total", 0) or 0) * 3)

        job_manager.update_job(
            job_id,
            phase=phase,
            message=message,
            progress_pct=pct,
            eta_seconds=eta,
            pages_crawled=metrics.get(
                "processed_results",
                metrics.get(
                    "successful_results", metrics.get("accepted_valid_pages", 0)
                ),
            ),
            metrics=metrics,
        )

    pulse_task = asyncio.create_task(simulated_progress_pulse())

    try:
        await worker.run_institutional_crawl(
            start_url=url,
            max_pages=max_pages,
            concurrency=concurrency,
            max_depth=max_depth,
            persist_to_db=persist_to_db,
            save_markdown_files=save_markdown_files,
            use_allow_filter=use_allow_filter,
            min_content_words=min_content_words,
            count_valid_pages_only=count_valid_pages_only,
            block_old_years=block_old_years,
            use_sitemap_seed=use_sitemap_seed,
            debug_output_dir=debug_output_dir,
            progress_hook=on_progress,
        )

        # If on_progress didn't mark it yet (edge case), mark now.
        if not job_marked_complete:
            job_manager.update_job(
                job_id,
                status="completed",
                phase="completado",
                message="Scraping finalizado",
                progress_pct=100.0,
                eta_seconds=0,
                finished_at=datetime.now(),
            )

        user_key = (clerk_user_id or "").strip()
        if user_key:
            try:
                await upsert_user_from_clerk_event(
                    clerk_user_id=user_key,
                    email=None,
                    last_sign_in_at=None,
                )
                parsed = urlparse(url)
                host = normalize_domain(parsed.netloc or "")
                variants = sorted(domain_variants(host))
                if variants:
                    async with async_session() as session:
                        row = (
                            await session.execute(
                                text(
                                    """
                                    SELECT source_id::text AS source_id
                                    FROM sources
                                    WHERE lower(domain) = :domain1 OR lower(domain) = :domain2
                                    ORDER BY created_at DESC
                                    LIMIT 1
                                    """
                                ),
                                {"domain1": variants[0], "domain2": variants[1]},
                            )
                        ).mappings().first()
                    if row and row.get("source_id"):
                        await link_user_to_source(
                            clerk_user_id=user_key,
                            source_id=str(row.get("source_id")),
                        )
            except Exception:
                # No romper el estado "completed" si falla solo el enlace usuario-source.
                pass
    except Exception as e:
        message = str(e).strip() or repr(e)
        job_manager.update_job(
            job_id,
            status="failed",
            phase="error",
            message="Falló el scraping",
            finished_at=datetime.now(),
            progress_pct=99.4,
            eta_seconds=None,
            errors=[message, traceback.format_exc()],
        )
    finally:
        pulse_task.cancel()
        try:
            await pulse_task
        except asyncio.CancelledError:
            pass


@router.post("/scrape")
async def start_scraping(req: ScrapeRequest, bt: BackgroundTasks):
    job_id = job_manager.create_job()
    bt.add_task(
        run_scrape_task,
        job_id,
        str(req.url),
        req.max_pages,
        req.concurrency,
        req.max_depth,
        req.persist_to_db,
        req.save_markdown_files,
        req.use_allow_filter,
        req.min_content_words,
        req.count_valid_pages_only,
        req.block_old_years,
        req.use_sitemap_seed,
        (req.clerk_user_id or "").strip() or None,
    )
    return {"job_id": job_id, "status": "accepted"}


@router.post("/scrape/refresh-page")
async def quick_refresh_page(req: QuickRefreshRequest):
    """
    Refresh a single canonical page without deep crawl.
    Useful for fast precision fixes (e.g., /carreras/medicina) in seconds.
    """
    url = str(req.url)
    scraper = ScrapingService()
    ingestor = IngestionService()
    classifier = PageClassifier()

    scrape_result = await scraper.scrape_page(url)
    if not scrape_result.success or not (scrape_result.markdown or "").strip():
        return {
            "status": "failed",
            "url": url,
            "message": "No se pudo extraer contenido de la URL",
        }

    classification = classifier.classify_with_content(
        url=url,
        title=scrape_result.title or "",
        content=(scrape_result.markdown or "")[:8000],
    )

    variants = sorted(domain_variants(normalize_domain(req.url.host or "")))
    d1 = variants[0] if variants else normalize_domain(req.url.host or "")
    d2 = variants[1] if len(variants) > 1 else d1

    async with async_session() as session:
        saved = await ingestor.process_and_save(
            url=url,
            title=scrape_result.title or "",
            content=scrape_result.markdown or "",
            session=session,
            page_type=classification.page_type,
            content_type="html",
            authority_score=float(classification.authority_score),
        )
        await session.commit()

        source_row = (
            await session.execute(
                text(
                    """
                    SELECT s.source_id::text AS source_id
                    FROM sources s
                    WHERE lower(s.domain) = :domain1 OR lower(s.domain) = :domain2
                    ORDER BY s.created_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "domain1": d1,
                    "domain2": d2,
                },
            )
        ).mappings().first()

    user_key = (req.clerk_user_id or "").strip()
    source_id = str(source_row.get("source_id")) if source_row and source_row.get("source_id") else None
    if user_key and source_id:
        try:
            await upsert_user_from_clerk_event(
                clerk_user_id=user_key,
                email=None,
                last_sign_in_at=None,
            )
            await link_user_to_source(clerk_user_id=user_key, source_id=source_id)
        except Exception:
            pass

    return {
        "status": "ok",
        "url": url,
        "saved": saved,
        "page_type": classification.page_type,
        "authority_score": classification.authority_score,
        "source_id": source_id,
        "pdf_links_found": len(scrape_result.pdf_links or []),
        "pdf_links_sample": (scrape_result.pdf_links or [])[:10] if req.include_pdf_links else [],
    }
