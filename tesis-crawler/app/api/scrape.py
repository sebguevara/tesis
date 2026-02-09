import asyncio
import traceback
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field, HttpUrl, field_validator
from app.tasks.worker import CrawlWorker
from app.core.job_manager import job_manager

router = APIRouter(tags=["Scrape"])


class ScrapeRequest(BaseModel):
    url: HttpUrl
    max_pages: int = 100
    concurrency: int = Field(default=10, ge=1, le=50)
    max_depth: int = Field(default=5, ge=1, le=10)
    persist_to_db: bool = True
    save_markdown_files: bool = False
    use_allow_filter: bool = True
    min_content_words: int = Field(default=5, ge=1, le=200)
    count_valid_pages_only: bool = True
    block_old_years: bool = True

    @field_validator("url")
    @classmethod
    def validate_https_url(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme.lower() != "https":
            raise ValueError("La URL debe usar HTTPS")
        return value


def _safe_progress(value: float) -> float:
    return max(0.0, min(100.0, round(value, 2)))


def _simulated_tracking_progress(elapsed_seconds: float) -> float:
    # Curva pensada para crawls de 3-5 min: sube gradual y evita saltos tempranos.
    t = max(0.0, float(elapsed_seconds))
    if t <= 20:
        return _safe_progress(2.0 + (t / 20.0) * 8.0)  # 2 -> 10
    if t <= 60:
        return _safe_progress(10.0 + ((t - 20.0) / 40.0) * 15.0)  # 10 -> 25
    if t <= 120:
        return _safe_progress(25.0 + ((t - 60.0) / 60.0) * 15.0)  # 25 -> 40
    if t <= 180:
        return _safe_progress(40.0 + ((t - 120.0) / 60.0) * 12.0)  # 40 -> 52
    if t <= 240:
        return _safe_progress(52.0 + ((t - 180.0) / 60.0) * 10.0)  # 52 -> 62
    if t <= 300:
        return _safe_progress(62.0 + ((t - 240.0) / 60.0) * 8.0)  # 62 -> 70
    return _safe_progress(min(82.0, 70.0 + ((t - 300.0) / 120.0) * 12.0))


def _estimate_progress(
    metrics: dict, max_pages: int, count_valid_pages_only: bool
) -> tuple[float, str, str]:
    accepted = int(metrics.get("accepted_valid_pages", 0))
    successful = int(metrics.get("successful_results", 0))
    total_results = int(metrics.get("total_results", 0))
    crawl_budget_pages = int(metrics.get("crawl_budget_pages", max_pages))
    finished_reason = str(metrics.get("finished_reason", "running"))

    if count_valid_pages_only:
        target = max(1, max_pages)
        observed_total = total_results if total_results > 0 else max(successful, accepted)

        # Curva no lineal: muestra avance temprano sin perder relación con el objetivo real.
        valid_ratio = min(1.0, accepted / target)
        budget_ratio = min(1.0, observed_total / max(1, crawl_budget_pages))
        valid_curve = (valid_ratio ** 0.55) * 95.0
        budget_curve = (budget_ratio ** 0.60) * 90.0
        pct = _safe_progress(max(valid_curve, budget_curve))
    else:
        target = max(1, max_pages)
        pct = _safe_progress((successful / target) * 100)

    if finished_reason == "frontier_exhausted" and pct < 95:
        pct = max(pct, 95.0)

    phase = "procesando"
    message = f"Procesando resultados ({accepted} válidas)"
    if total_results == 0 and successful == 0:
        phase = "rastreando"
        message = "Explorando enlaces y descargando páginas"

    return pct, phase, message


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
):
    worker = CrawlWorker()
    debug_output_dir = f"./crawl_debug/{job_id}"
    started_at = datetime.now()
    first_metrics_seen = False

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
        },
    )

    async def simulated_progress_pulse():
        # Keep the UI moving while crawl discovery is still running and no metrics arrived.
        while True:
            await asyncio.sleep(1)
            job = job_manager.get_job(job_id)
            if not job or job.status != "running":
                return
            if first_metrics_seen:
                continue
            elapsed = (datetime.now() - started_at).total_seconds()
            pct = _simulated_tracking_progress(elapsed)
            job_manager.update_job(
                job_id,
                phase="rastreando",
                message="Explorando enlaces y descargando páginas",
                progress_pct=pct,
            )

    def on_progress(metrics: dict):
        nonlocal first_metrics_seen
        first_metrics_seen = True
        pct, phase, message = _estimate_progress(
            metrics, max_pages=max_pages, count_valid_pages_only=count_valid_pages_only
        )
        current_job = job_manager.get_job(job_id)
        current_pct = 0.0
        if current_job is not None:
            current_pct = float(current_job.progress_pct or 0.0)
        pct = max(pct, current_pct)
        if (
            str(metrics.get("finished_reason", "running")) == "running"
            and pct <= current_pct
            and current_pct < 99.9
        ):
            pct = _safe_progress(current_pct + 0.1)
        elapsed = max((datetime.now() - started_at).total_seconds(), 0)
        eta = None
        if pct >= 3 and pct < 100 and elapsed > 0:
            eta = int((100 - pct) * elapsed / pct)
        job_manager.update_job(
            job_id,
            phase=phase,
            message=message,
            progress_pct=pct,
            eta_seconds=eta,
            pages_crawled=metrics.get(
                "accepted_valid_pages", metrics.get("successful_results", 0)
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
            debug_output_dir=debug_output_dir,
            progress_hook=on_progress,
        )
        job_manager.update_job(
            job_id,
            status="completed",
            phase="completado",
            message="Scraping finalizado",
            progress_pct=100.0,
            eta_seconds=0,
            finished_at=datetime.now(),
        )
    except Exception as e:
        message = str(e).strip() or repr(e)
        job_manager.update_job(
            job_id,
            status="failed",
            phase="error",
            message="Falló el scraping",
            finished_at=datetime.now(),
            progress_pct=100.0,
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
    )
    return {"job_id": job_id, "status": "accepted"}
