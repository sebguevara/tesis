"""
Crawl Worker — institutional crawl with async HTML + PDF ingestion.
"""

from datetime import datetime
import hashlib
import json
import logging
import re
import asyncio
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin
from typing import Callable, Optional, Any
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler
from crawl4ai.async_dispatcher import MemoryAdaptiveDispatcher, SemaphoreDispatcher
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from crawl4ai.deep_crawling.filters import FilterChain, URLFilter, URLPatternFilter
from app.config import settings
from app.core.content_filters import should_index_page
from app.core.domain_utils import normalize_host_exact
from app.core.page_classifier import PageClassifier
from app.core.pdf_service import PDFService
from app.core.scraping_service import ScrapingService
from app.core.ingestion_service import IngestionService
from app.storage.db_client import async_session

logger = logging.getLogger(__name__)


class ExactHostFilter(URLFilter):
    def __init__(self, host: str, sample_limit: int = 50000):
        super().__init__()
        self._allowed_host = normalize_host_exact(host)
        self._sample_limit = sample_limit
        self.rejected_url_samples: list[str] = []

    def is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        candidate_host = normalize_host_exact(parsed.netloc or parsed.hostname or "")
        return (
            parsed.scheme.lower() == "https"
            and bool(self._allowed_host)
            and candidate_host == self._allowed_host
        )

    def apply(self, url: str) -> bool:
        result = self.is_allowed(url)
        if not result and len(self.rejected_url_samples) < self._sample_limit:
            self.rejected_url_samples.append(url)
        self._update_stats(result)
        return result


class TrackingPatternFilter(URLPatternFilter):
    def __init__(self, *args, sample_limit: int = 50000, **kwargs):
        super().__init__(*args, **kwargs)
        self._sample_limit = sample_limit
        self.rejected_url_samples: list[str] = []

    def apply(self, url: str) -> bool:
        result = super().apply((url or "").lower())
        if not result and len(self.rejected_url_samples) < self._sample_limit:
            self.rejected_url_samples.append(url)
        return result


class CrawlWorker:
    ALLOW_PRIORITY_TOKENS = (
        "/admision",
        "/admisiones",
        "/ingreso",
        "/ingresantes",
        "/inscripcion",
        "/inscripciones",
        "/carreras",
        "/oferta-academica",
        "/ofertas-academicas",
        "/programas",
        "/plan-de-estudios",
        "/planes-de-estudio",
        "/requisitos",
        "/tramites",
        "/trámites",
    )

    SITEMAP_PATHS = ("/sitemap_index.xml", "/sitemap.xml")
    SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    def __init__(self):
        self.scraper = ScrapingService()
        self.ingestor = IngestionService()
        self.classifier = PageClassifier()
        self.pdf_service = PDFService()

    @classmethod
    async def _fetch_sitemap_urls(cls, start_url: str, host_filter: "ExactHostFilter") -> list[str]:
        """
        Try to discover the site's sitemap and return up to a few thousand URLs.
        Walks one level of <sitemapindex> if present. Returns [] on any failure.
        """
        parsed = urlparse(start_url)
        if not parsed.scheme or not parsed.netloc:
            return []
        base = f"{parsed.scheme}://{parsed.netloc}"
        seen: set[str] = set()
        urls: list[str] = []

        async def _fetch(url: str) -> str | None:
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=httpx.Timeout(15.0),
                    headers={"User-Agent": "tesis-crawler/0.1 (sitemap-discovery)"},
                ) as client:
                    resp = await client.get(url)
                if resp.status_code != 200 or not (resp.content or b"").strip():
                    return None
                return resp.text
            except Exception:
                return None

        def _parse(xml_text: str) -> tuple[list[str], list[str]]:
            """Return (page_urls, child_sitemap_urls)."""
            try:
                root = ET.fromstring(xml_text)
            except ET.ParseError:
                return [], []
            tag = root.tag.split("}")[-1]
            child_sitemaps: list[str] = []
            page_urls: list[str] = []
            if tag == "sitemapindex":
                for sm in root.findall("sm:sitemap", cls.SITEMAP_NS):
                    loc = sm.find("sm:loc", cls.SITEMAP_NS)
                    if loc is not None and loc.text:
                        child_sitemaps.append(loc.text.strip())
            elif tag == "urlset":
                for u in root.findall("sm:url", cls.SITEMAP_NS):
                    loc = u.find("sm:loc", cls.SITEMAP_NS)
                    if loc is not None and loc.text:
                        page_urls.append(loc.text.strip())
            return page_urls, child_sitemaps

        candidates: list[str] = [base + p for p in cls.SITEMAP_PATHS]
        explored: set[str] = set()

        while candidates and len(urls) < 5000:
            current = candidates.pop(0)
            if current in explored:
                continue
            explored.add(current)
            xml_text = await _fetch(current)
            if not xml_text:
                continue
            page_urls, child_sitemaps = _parse(xml_text)
            for child in child_sitemaps:
                if child not in explored:
                    candidates.append(child)
            for u in page_urls:
                if u in seen:
                    continue
                if not host_filter.is_allowed(u):
                    continue
                low = u.lower()
                if low.endswith(".pdf") or ".pdf?" in low:
                    continue  # PDFs flow through the dedicated PDF queue
                seen.add(u)
                urls.append(u)
                if len(urls) >= 5000:
                    break

        return urls

    @classmethod
    def _matches_allow_priority(cls, url: str, title: str = "") -> bool:
        haystack = f"{(url or '').lower()} {(title or '').lower()}".strip()
        return any(token in haystack for token in cls.ALLOW_PRIORITY_TOKENS)

    def _invalid_reason(
        self, url: str, title: str, content: str, min_content_words: int = 5
    ) -> Optional[str]:
        """
        Check if a page is invalid and should be skipped.
        Uses PageClassifier for blocking decisions and should_index_page
        for content filtering.
        """
        url_lc = (url or "").lower()
        parsed = urlparse(url_lc)
        if not content:
            return "empty_content"
        normalized = content.strip().lower()
        if not normalized:
            return "blank_content"
        if parsed.path in ("", "/"):
            return "root_path"
        if "página no encontrada" in normalized or "pagina no encontrada" in normalized:
            return "not_found_page"
        if len(normalized.split()) < max(1, min_content_words):
            return "too_short"

        # Use the classifier to check if this page type should be blocked
        classification = self.classifier.classify(url, title, normalized[:2000])
        if classification.should_block and classification.page_type == "news_blocked":
            return f"classifier_blocked:{classification.reason}"

        # Use smart content filter
        should_index, filter_reason = should_index_page(url, title, normalized)
        if not should_index:
            return filter_reason

        return None

    @staticmethod
    def _slugify(value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r"[^a-z0-9]+", "-", value)
        value = re.sub(r"-{2,}", "-", value).strip("-")
        return value or "page"

    def _save_markdown_to_disk(self, url: str, title: str, content: str) -> tuple[bool, str]:
        base_dir = Path(settings.SITE_MD_DIR)
        base_dir.mkdir(parents=True, exist_ok=True)
        url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        slug = self._slugify(title or url)[:80]
        filename = f"{slug}-{url_hash}.md"
        path = base_dir / filename
        try:
            path.write_text(content, encoding="utf-8")
            return True, "saved"
        except OSError:
            short_path = base_dir / f"page-{url_hash}.md"
            try:
                short_path.write_text(content, encoding="utf-8")
                return True, "saved_with_short_name"
            except OSError as exc:
                return False, f"save_markdown_error:{exc.__class__.__name__}"

    @staticmethod
    def _dedupe_keep_order(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def _extract_pdf_links_from_html(self, html: str, base_url: str) -> list[str]:
        """Extract PDF links from HTML/markdown content."""
        try:
            soup = BeautifulSoup(html, "html.parser")
            pdf_links: list[str] = []
            seen: set[str] = set()
            for anchor in soup.find_all("a", href=True):
                href = anchor["href"].strip()
                if not href:
                    continue
                href_lower = href.lower()
                if not (href_lower.endswith(".pdf") or ".pdf?" in href_lower):
                    continue
                absolute_url = urljoin(base_url, href)
                normalized = absolute_url.split("#")[0].split("?")[0]
                if normalized not in seen:
                    seen.add(normalized)
                    pdf_links.append(absolute_url)
            # Also parse markdown links when HTML parser doesn't catch links.
            for match in re.findall(r"\[[^\]]+\]\(([^)]+\.pdf(?:\?[^)]*)?)\)", html, flags=re.IGNORECASE):
                absolute_url = urljoin(base_url, match.strip())
                normalized = absolute_url.split("#")[0].split("?")[0]
                if normalized not in seen:
                    seen.add(normalized)
                    pdf_links.append(absolute_url)
            return pdf_links
        except Exception:
            return []

    @staticmethod
    def _extract_year_candidates(value: str) -> set[int]:
        years: set[int] = set()
        for raw in re.findall(r"(20\d{2})", value or ""):
            try:
                years.add(int(raw))
            except ValueError:
                continue
        return years

    @staticmethod
    def _is_recent_pdf_url(url: str, current_year: int, lookback_years: int) -> bool:
        years = CrawlWorker._extract_year_candidates(url)
        if not years:
            return False
        min_year = current_year - max(0, lookback_years)
        return any(min_year <= y <= current_year for y in years)

    def _write_debug_report(
        self,
        debug_output_dir: str,
        blocked_host_urls: list[str],
        blocked_block_urls: list[str],
        failed_fetch_urls: list[str],
        skipped_invalid_urls: list[str],
        skipped_ingestion_rows: list[str],
        metrics: dict,
    ) -> None:
        debug_dir = Path(debug_output_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)

        (debug_dir / "blocked_by_host_filter.txt").write_text(
            "\n".join(self._dedupe_keep_order(blocked_host_urls)),
            encoding="utf-8",
        )
        (debug_dir / "blocked_by_block_filter.txt").write_text(
            "\n".join(self._dedupe_keep_order(blocked_block_urls)),
            encoding="utf-8",
        )
        (debug_dir / "failed_fetch_or_scrape.txt").write_text(
            "\n".join(self._dedupe_keep_order(failed_fetch_urls)),
            encoding="utf-8",
        )
        (debug_dir / "skipped_invalid_content.tsv").write_text(
            "\n".join(self._dedupe_keep_order(skipped_invalid_urls)),
            encoding="utf-8",
        )
        (debug_dir / "skipped_ingestion.tsv").write_text(
            "\n".join(self._dedupe_keep_order(skipped_ingestion_rows)),
            encoding="utf-8",
        )
        (debug_dir / "summary.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (debug_dir / "finished_reason.txt").write_text(
            str(metrics.get("finished_reason", "unknown")),
            encoding="utf-8",
        )

    async def run_institutional_crawl(
        self,
        start_url: str,
        max_pages: int = 5000,
        concurrency: int = 10,
        max_depth: int = 5,
        persist_to_db: bool = True,
        save_markdown_files: bool = False,
        use_allow_filter: bool = True,
        min_content_words: int = 5,
        count_valid_pages_only: bool = True,
        block_old_years: bool = True,
        use_sitemap_seed: bool = False,
        debug_output_dir: Optional[str] = None,
        progress_hook: Optional[Callable[[dict], None]] = None,
    ):
        parsed_start = urlparse(start_url)
        if not parsed_start.scheme or not parsed_start.netloc:
            raise ValueError("start_url inválida")
        if parsed_start.scheme.lower() != "https":
            raise ValueError("start_url debe usar HTTPS")
        start_host_exact = normalize_host_exact(parsed_start.netloc or parsed_start.hostname or "")

        host_filter = ExactHostFilter(parsed_start.netloc or parsed_start.hostname or "")
        block_filter = TrackingPatternFilter(
            patterns=[
                "*/attachment/*",
                "*attachment_id=*",
                "*/feed/*",
                "*/wp-json/*",
                "*/wp-admin/*",
                "*/wp-login.php*",
                "*/xmlrpc.php*",
                "*/?s=*",
                "*?fluentcrm=*",
                "*/search/*",
                "*/siga*",
                "*/cvm-prop-form/*",
                # Images
                "*.jpg*",
                "*.jpeg*",
                "*.png*",
                "*.gif*",
                "*.webp*",
                "*.svg*",
                # Audio
                "*.mp3*",
                "*.wav*",
                "*.ogg*",
                "*.m4a*",
                "*.aac*",
                # Video
                "*.mp4*",
                "*.webm*",
                "*.avi*",
                "*.mov*",
                "*.mkv*",
                # Binary/documents are blocked from crawler traversal.
                # PDFs are handled asynchronously when extracted from HTML pages.
                "*.pdf*",
                "*.doc*",
                "*.docx*",
                "*.xls*",
                "*.xlsx*",
                "*.ppt*",
                "*.pptx*",
                "*/wp-content/uploads/*",
                # Archives
                "*.zip*",
                "*.rar*",
                "*.7z*",
                # ── NEWS / EVENTS / COMMUNICATIONS — hard blocked ──
                "*/noticia/*",
                "*/noticias/*",
                "*/notimed/*",
                "*/novedad/*",
                "*/novedades/*",
                "*/prensa/*",
                "*/comunicado/*",
                "*/comunicados/*",
                "*/blog/*",
                "*/news/*",
                "*/evento/*",
                "*/eventos/*",
                "*/agenda/*",
                "*/actualidad/*",
                "*/actualidades/*",
                "*/boletin/*",
                "*/newsletter/*",
                "*/gacetilla/*",
                "*/efemeride/*",
                "*/efemerides/*",
            ],
            reverse=True,
        )
        filter_chain_parts = [host_filter]
        filter_chain_parts.append(block_filter)

        # Stage 1: optional sitemap-first seeding. Faster but trades recall
        # (the sitemap may not list every page). Off by default; flip on with
        # use_sitemap_seed=true on POST /api/scrape.
        sitemap_urls: list[str] = []
        if use_sitemap_seed:
            sitemap_urls = await self._fetch_sitemap_urls(start_url, host_filter)
            sitemap_urls = [u for u in sitemap_urls if block_filter.apply(u)]
        use_sitemap_seed = use_sitemap_seed and len(sitemap_urls) > 0
        if use_sitemap_seed:
            logger.info(
                "Sitemap discovery found %d URLs for %s — using sitemap seeding",
                len(sitemap_urls), start_url,
            )

        deep_crawl = None
        if not use_sitemap_seed:
            deep_crawl = BFSDeepCrawlStrategy(
                max_depth=max(1, max_depth),
                include_external=False,
                filter_chain=FilterChain(filter_chain_parts),
            )

        config = self.scraper.config
        config.deep_crawl_strategy = deep_crawl
        crawl_budget_pages = max_pages
        if count_valid_pages_only:
            crawl_budget_pages = min(max(max_pages * 4, max_pages + 500), 50000)
        config.max_pages = crawl_budget_pages
        config.semaphore_count = max(1, concurrency)

        # ── Initialize Metrics & Containers ──
        failed_fetch_urls: list[str] = []
        skipped_invalid_urls: list[str] = []
        skipped_ingestion_rows: list[str] = []
        crawl_started_at = time.monotonic()
        metrics = {
            "total_results": 0,
            "processed_results": 0,
            "finished_reason": "running",
            "target_valid_pages": max_pages,
            "crawl_budget_pages": crawl_budget_pages,
            "seeding_strategy": "sitemap" if use_sitemap_seed else "bfs",
            "sitemap_urls_found": len(sitemap_urls) if use_sitemap_seed else 0,
            "wallclock_seconds": 0.0,
            "accepted_valid_pages": 0,
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
            # Phase 2 (PDF) metrics
            "pdf_links_found": 0,
            "pdf_processed": 0,
            "pdf_saved": 0,
            "pdf_skipped": 0,
            "pdf_errors": 0,
            "pdf_workers": 0,
            "pdf_queue_size": 0,
            "pdf_inflight": 0,
            "pdf_pending_total": 0,
            "last_processed_url": "",
            "ingest_workers": 0,
            "ingest_queue_size": 0,
            "ingest_queue_maxsize": 0,
            "ingest_inflight": 0,
            "ingest_pending_total": 0,
        }
        metrics_lock = asyncio.Lock()
        ingest_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=max(50, max(1, concurrency) * 20)
        )
        pdf_queue: asyncio.Queue[str | None] = asyncio.Queue(
            maxsize=max(30, max(1, concurrency) * 10)
        )
        # Stage 1: scale ingest workers with concurrency (was capped at 8).
        ingest_workers = max(1, min(16, max(1, concurrency)))
        pdf_workers = max(1, min(4, max(1, concurrency // 2 or 1)))
        ingest_inflight = 0
        pdf_inflight = 0
        seen_pdf_urls: set[str] = set()
        metrics["ingest_workers"] = ingest_workers
        metrics["pdf_workers"] = pdf_workers
        metrics["ingest_queue_maxsize"] = int(getattr(ingest_queue, "maxsize", 0) or 0)
        current_year = datetime.now().year
        pdf_lookback_years = int(getattr(settings, "PDF_LOOKBACK_YEARS", 5) or 5)

        async def _sync_queue_metrics() -> None:
            async with metrics_lock:
                qsize = int(ingest_queue.qsize())
                inflight = int(ingest_inflight)
                pdf_qsize = int(pdf_queue.qsize())
                pdf_flight = int(pdf_inflight)
                metrics["ingest_queue_size"] = qsize
                metrics["ingest_inflight"] = inflight
                metrics["pdf_queue_size"] = pdf_qsize
                metrics["pdf_inflight"] = pdf_flight
                metrics["pdf_pending_total"] = pdf_qsize + pdf_flight
                metrics["ingest_pending_total"] = qsize + inflight + pdf_qsize + pdf_flight

        async def _apply_metric_delta(
            *,
            saved_docs: int = 0,
            skipped_ingestion: int = 0,
            skipped_processing_errors: int = 0,
            skipped_db_disabled: int = 0,
        ) -> None:
            async with metrics_lock:
                if saved_docs:
                    metrics["saved_docs"] += int(saved_docs)
                if skipped_ingestion:
                    metrics["skipped_ingestion"] += int(skipped_ingestion)
                if skipped_processing_errors:
                    metrics["skipped_processing_errors"] += int(skipped_processing_errors)
                if skipped_db_disabled:
                    metrics["skipped_db_disabled"] += int(skipped_db_disabled)

        async def _ingest_consumer() -> None:
            nonlocal ingest_inflight
            while True:
                item = await ingest_queue.get()
                if item is None:
                    ingest_queue.task_done()
                    break
                try:
                    if not persist_to_db:
                        await _apply_metric_delta(skipped_db_disabled=1)
                        continue
                    async with metrics_lock:
                        ingest_inflight += 1
                    await _sync_queue_metrics()
                    async with async_session() as session:
                        ingestion_result = await self.ingestor.process_and_save(
                            url=str(item.get("url") or ""),
                            title=str(item.get("title") or ""),
                            content=str(item.get("markdown") or ""),
                            session=session,
                            page_type=str(item.get("page_type") or "institutional_info"),
                            content_type="html",
                            authority_score=float(item.get("authority_score") or 0.5),
                            allowed_host_exact=start_host_exact,
                        )
                    if ingestion_result.get("saved"):
                        await _apply_metric_delta(saved_docs=1)
                    else:
                        await _apply_metric_delta(skipped_ingestion=1)
                        reason = ingestion_result.get("reason", "unknown")
                        skipped_ingestion_rows.append(f"{item.get('url')}\t{reason}")
                except Exception as exc:
                    await _apply_metric_delta(skipped_processing_errors=1)
                    skipped_ingestion_rows.append(
                        f"{item.get('url')}\tprocessing_error:{exc.__class__.__name__}"
                    )
                finally:
                    async with metrics_lock:
                        ingest_inflight = max(0, ingest_inflight - 1)
                    ingest_queue.task_done()
                    await _sync_queue_metrics()
                    if progress_hook:
                        progress_hook(metrics)

        async def _pdf_consumer() -> None:
            nonlocal pdf_inflight
            while True:
                pdf_url = await pdf_queue.get()
                if pdf_url is None:
                    pdf_queue.task_done()
                    break
                try:
                    async with metrics_lock:
                        pdf_inflight += 1
                    await _sync_queue_metrics()

                    result = await self.pdf_service.download_and_convert(pdf_url)
                    async with metrics_lock:
                        metrics["pdf_processed"] += 1

                    if not result or not result.success or not result.markdown_content.strip():
                        async with metrics_lock:
                            metrics["pdf_errors"] += 1
                        continue
                    if not host_filter.is_allowed(result.url):
                        async with metrics_lock:
                            metrics["blocked_by_host_filter"] = int(metrics.get("blocked_by_host_filter", 0)) + 1
                        if len(host_filter.rejected_url_samples) < 50000:
                            host_filter.rejected_url_samples.append(result.url)
                        continue

                    if persist_to_db:
                        async with async_session() as session:
                            saved = await self.ingestor.process_pdf_and_save(
                                url=result.url,
                                title=result.title or result.original_filename or "Documento PDF",
                                markdown_content=result.markdown_content,
                                session=session,
                                page_type="pdf_document",
                                authority_score=0.7,
                                original_filename=result.original_filename or None,
                                pdf_metadata=result.metadata or None,
                                allowed_host_exact=start_host_exact,
                            )
                        if saved.get("saved"):
                            async with metrics_lock:
                                metrics["pdf_saved"] += 1
                        else:
                            async with metrics_lock:
                                metrics["pdf_skipped"] += 1
                    else:
                        async with metrics_lock:
                            metrics["pdf_skipped"] += 1
                except Exception:
                    async with metrics_lock:
                        metrics["pdf_errors"] += 1
                finally:
                    async with metrics_lock:
                        pdf_inflight = max(0, pdf_inflight - 1)
                    pdf_queue.task_done()
                    await _sync_queue_metrics()
                    if progress_hook:
                        progress_hook(metrics)

        consumers = [asyncio.create_task(_ingest_consumer()) for _ in range(ingest_workers)]
        pdf_consumers = [asyncio.create_task(_pdf_consumer()) for _ in range(pdf_workers)]

        def _res_get(res: Any, key: str, default: Any = None) -> Any:
            if isinstance(res, dict):
                return res.get(key, default)
            return getattr(res, key, default)

        def _res_title(res: Any) -> str:
            if isinstance(res, dict):
                metadata = res.get("metadata") or {}
                if isinstance(metadata, dict):
                    return str(metadata.get("title") or "")
                return ""
            metadata = getattr(res, "metadata", None) or {}
            if isinstance(metadata, dict):
                return str(metadata.get("title") or "")
            return ""

        # ── Define Hook for Streaming Processing ──
        async def on_result_hook(res) -> None:
            """Hook called for every page crawled."""
            res_url = str(_res_get(res, "url", "") or "")
            res_success = bool(_res_get(res, "success", False))
            res_markdown = str(_res_get(res, "markdown", "") or "")
            res_title = _res_title(res)
            res_html = str(
                _res_get(res, "html", "")
                or _res_get(res, "cleaned_html", "")
                or _res_get(res, "raw_html", "")
                or ""
            )

            metrics["processed_results"] += 1
            metrics["last_processed_url"] = res_url

            # Update filter stats (approximate)
            metrics["blocked_by_host_filter"] = max(
                int(metrics.get("blocked_by_host_filter", 0)),
                int(host_filter.stats.rejected_urls),
            )
            metrics["blocked_by_block_filter"] = block_filter.stats.rejected_urls

            # Hard guardrail: never ingest out-of-domain URLs.
            if not host_filter.is_allowed(res_url):
                metrics["blocked_by_host_filter"] = int(metrics.get("blocked_by_host_filter", 0)) + 1
                if len(host_filter.rejected_url_samples) < 50000:
                    host_filter.rejected_url_samples.append(res_url)
                if progress_hook:
                    progress_hook(metrics)
                return

            if not res_success:
                failed_fetch_urls.append(res_url)
                if progress_hook:
                    progress_hook(metrics)
                return

            metrics["successful_results"] += 1

            # Validate Content
            invalid_reason = self._invalid_reason(
                res_url,
                res_title,
                res_markdown,
                min_content_words=min_content_words,
            )
            if invalid_reason:
                metrics["skipped_invalid_content"] += 1
                skipped_invalid_urls.append(f"{res_url}\t{invalid_reason}")
                if progress_hook:
                    progress_hook(metrics)
                return

            metrics["accepted_valid_pages"] += 1
            if use_allow_filter and self._matches_allow_priority(
                res_url, res_title
            ):
                metrics["matched_allow_filter"] += 1
            
            if count_valid_pages_only and metrics["accepted_valid_pages"] >= max_pages:
                metrics["finished_reason"] = "target_reached"

            # Classify & Save
            page_title = res_title
            classification = self.classifier.classify_content(
                res_url, page_title, res_markdown
            )

            try:
                if save_markdown_files:
                    saved_file, save_reason = self._save_markdown_to_disk(
                        res_url,
                        page_title,
                        res_markdown,
                    )
                    if saved_file:
                        metrics["saved_markdown_files"] += 1
                    else:
                        metrics["skipped_save_markdown"] += 1
                        skipped_ingestion_rows.append(f"{res_url}\t{save_reason}")

                await ingest_queue.put(
                    {
                        "url": res_url,
                        "title": page_title,
                        "markdown": res_markdown,
                        "page_type": classification.page_type,
                        "authority_score": classification.authority_score,
                    }
                )
                await _sync_queue_metrics()
            except Exception as exc:
                await _apply_metric_delta(skipped_processing_errors=1)
                skipped_ingestion_rows.append(
                    f"{res_url}\tprocessing_error:{exc.__class__.__name__}"
                )

            # Discover and enqueue recent PDF links (current year and previous year only).
            pdf_links = self._extract_pdf_links_from_html(
                res_html or res_markdown,
                res_url,
            )
            if pdf_links:
                for pdf_link in pdf_links:
                    normalized_pdf = pdf_link.split("#")[0].strip()
                    if not normalized_pdf or normalized_pdf in seen_pdf_urls:
                        continue
                    if not host_filter.is_allowed(normalized_pdf):
                        metrics["blocked_by_host_filter"] = int(metrics.get("blocked_by_host_filter", 0)) + 1
                        if len(host_filter.rejected_url_samples) < 50000:
                            host_filter.rejected_url_samples.append(normalized_pdf)
                        continue
                    if not self._is_recent_pdf_url(
                        normalized_pdf,
                        current_year=current_year,
                        lookback_years=pdf_lookback_years,
                    ):
                        metrics["pdf_skipped"] += 1
                        continue
                    seen_pdf_urls.add(normalized_pdf)
                    metrics["pdf_links_found"] += 1
                    await pdf_queue.put(normalized_pdf)
                await _sync_queue_metrics()
            
            if progress_hook:
                progress_hook(metrics)

        # Register hook
        config.hooks = {
            "on_result": on_result_hook
        }

        try:
            async with AsyncWebCrawler() as crawler:
                # SemaphoreDispatcher works for arun() (BFS) but lacks the
                # streaming interface arun_many() needs. MemoryAdaptiveDispatcher
                # supports both — use it on the sitemap path.
                if use_sitemap_seed:
                    crawl_dispatcher = MemoryAdaptiveDispatcher(
                        max_session_permit=max(1, concurrency),
                    )
                else:
                    crawl_dispatcher = SemaphoreDispatcher(
                        semaphore_count=max(1, concurrency),
                        max_session_permit=max(1, concurrency),
                    )
                stream_prev = getattr(config, "stream", False)
                setattr(config, "stream", True)
                try:
                    if use_sitemap_seed:
                        # Sitemap path: feed discovered URLs directly to arun_many.
                        # Cap at the crawl budget to bound work.
                        seed_urls = sitemap_urls[: max(1, crawl_budget_pages)]
                        crawl_result = await crawler.arun_many(
                            seed_urls,
                            config=config,
                            dispatcher=crawl_dispatcher,
                        )
                    else:
                        # BFS path: deep_crawl_strategy wires the traversal off start_url.
                        crawl_result = await crawler.arun(start_url, config=config)

                    if hasattr(crawl_result, "__aiter__"):
                        async for res in crawl_result:
                            if res is None:
                                continue
                            await on_result_hook(res)
                            if (
                                count_valid_pages_only
                                and metrics["accepted_valid_pages"] >= max_pages
                            ):
                                metrics["finished_reason"] = "target_reached"
                                break
                    else:
                        fallback_results = []
                        if isinstance(crawl_result, list):
                            fallback_results = crawl_result
                        elif crawl_result is not None:
                            fallback_results = [crawl_result]
                        for res in fallback_results:
                            if res is None:
                                continue
                            await on_result_hook(res)
                except Exception as primary_exc:
                    logger.exception(
                        "Primary crawl path failed (use_sitemap_seed=%s); falling back to arun(start_url): %s",
                        use_sitemap_seed,
                        primary_exc,
                    )
                    crawl_result = await crawler.arun(start_url, config=config)
                    if crawl_result is not None:
                        if hasattr(crawl_result, "__aiter__"):
                            async for res in crawl_result:
                                if res is None:
                                    continue
                                await on_result_hook(res)
                        else:
                            await on_result_hook(crawl_result)
                finally:
                    setattr(config, "stream", stream_prev)
        finally:
            # Ensure all queued ingestions are flushed/stopped before completion.
            await ingest_queue.join()
            await pdf_queue.join()
            await _sync_queue_metrics()
            for _ in consumers:
                await ingest_queue.put(None)
            for _ in pdf_consumers:
                await pdf_queue.put(None)
            await asyncio.gather(*consumers, return_exceptions=True)
            await asyncio.gather(*pdf_consumers, return_exceptions=True)
            await _sync_queue_metrics()

        # Finalize
        if metrics["finished_reason"] == "running":
            metrics["finished_reason"] = "completed"
        metrics["total_results"] = metrics["processed_results"]
        metrics["wallclock_seconds"] = round(time.monotonic() - crawl_started_at, 2)

        if progress_hook:
            progress_hook(metrics)

        if debug_output_dir:
            self._write_debug_report(
                debug_output_dir=debug_output_dir,
                blocked_host_urls=host_filter.rejected_url_samples,
                blocked_block_urls=block_filter.rejected_url_samples,
                failed_fetch_urls=failed_fetch_urls,
                skipped_invalid_urls=skipped_invalid_urls,
                skipped_ingestion_rows=skipped_ingestion_rows,
                metrics=metrics,
            )
