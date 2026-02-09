from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from typing import Callable, Optional

from crawl4ai import AsyncWebCrawler
from crawl4ai.async_dispatcher import SemaphoreDispatcher
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from crawl4ai.deep_crawling.filters import FilterChain, URLFilter, URLPatternFilter
from app.config import settings
from app.core.content_filters import is_institutional_news, is_outdated_content
from app.core.domain_utils import domain_variants, normalize_domain
from app.core.scraping_service import ScrapingService
from app.core.ingestion_service import IngestionService
from app.storage.db_client import async_session


class ExactHostFilter(URLFilter):
    def __init__(self, host: str, sample_limit: int = 50000):
        super().__init__()
        self._allowed_hosts = domain_variants(host)
        self._sample_limit = sample_limit
        self.rejected_url_samples: list[str] = []

    def apply(self, url: str) -> bool:
        parsed = urlparse(url)
        candidate_host = normalize_domain(parsed.netloc)
        result = parsed.scheme.lower() == "https" and candidate_host in self._allowed_hosts
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
        # Normaliza para que patrones como *.jpg* también bloqueen .JPG/.Jpg/etc.
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

    def __init__(self):
        self.scraper = ScrapingService()
        self.ingestor = IngestionService()

    @classmethod
    def _matches_allow_priority(cls, url: str, title: str = "") -> bool:
        haystack = f"{(url or '').lower()} {(title or '').lower()}".strip()
        return any(token in haystack for token in cls.ALLOW_PRIORITY_TOKENS)

    @staticmethod
    def _invalid_reason(
        url: str, title: str, content: str, min_content_words: int = 5
    ) -> Optional[str]:
        url_lc = (url or "").lower()
        parsed = urlparse(url_lc)
        if not content:
            return "empty_content"
        normalized = content.strip().lower()
        if not normalized:
            return "blank_content"
        # Evita guardar home/listados genéricos que suelen ser solo cards y enlaces.
        if parsed.path in ("", "/"):
            return "root_path"
        if "página no encontrada" in normalized or "pagina no encontrada" in normalized:
            return "not_found_page"
        if len(normalized.split()) < max(1, min_content_words):
            return "too_short"
        if is_institutional_news(url, title, normalized):
            return "institutional_news"
        if is_outdated_content(url, title, normalized):
            return "outdated_content"
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
        # Evita rutas largas en Windows, que pueden disparar FileNotFoundError/OSError.
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
        debug_output_dir: Optional[str] = None,
        progress_hook: Optional[Callable[[dict], None]] = None,
    ):
        parsed_start = urlparse(start_url)
        if not parsed_start.scheme or not parsed_start.netloc:
            raise ValueError("start_url inválida")
        if parsed_start.scheme.lower() != "https":
            raise ValueError("start_url debe usar HTTPS")

        host_filter = ExactHostFilter(parsed_start.netloc)
        current_year = datetime.now().year
        stale_year_patterns = [f"*/{year}/*" for year in range(2000, current_year - 2)]
        year_patterns: list[str] = []
        if block_old_years:
            year_patterns = ["*/201*/*", *stale_year_patterns]
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
                "*/notimed/*",
                "*/licitaciones-y-compr/*",
                "*/licitaciones/*",
                "*/compras/*",
                "*/cvm-prop-form/*",
                "*/siga*",
                "*/boletin/*",
                "*/newsletter/*",
                "*/noticia/*",
                "*/noticias/*",
                "*/novedad/*",
                "*/novedades/*",
                "*/actualidad/*",
                "*/actualidades/*",
                "*/prensa/*",
                "*/comunicado/*",
                "*/comunicados/*",
                "*/blog/*",
                "*/news/*",
                "*/eventos/*",
                "*/evento/*",
                "*/agenda/*",
                "*.pdf*",
                "*.jpg*",
                "*.jpeg*",
                "*.png*",
                "*.gif*",
                "*.webp*",
                "*.svg*",
                "*.mp3*",
                "*.wav*",
                "*.ogg*",
                "*.m4a*",
                "*.aac*",
                "*.mp4*",
                "*.webm*",
                "*.avi*",
                "*.mov*",
                "*.mkv*",
                "*.doc*",
                "*.docx*",
                "*.xls*",
                "*.xlsx*",
                "*.ppt*",
                "*.pptx*",
                "*.zip*",
                "*.rar*",
                "*.7z*",
                "*/tag/*",
                "*/author/*",
                "*/category/*",
                *year_patterns,
            ],
            reverse=True,
        )
        filter_chain_parts = [host_filter]
        filter_chain_parts.append(block_filter)
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

        async with AsyncWebCrawler() as crawler:
            crawl_dispatcher = SemaphoreDispatcher(
                semaphore_count=max(1, concurrency),
                max_session_permit=max(1, concurrency),
            )
            original_arun_many = crawler.arun_many

            async def arun_many_with_dispatcher(
                urls, config=None, dispatcher=None, **kwargs
            ):
                return await original_arun_many(
                    urls=urls,
                    config=config,
                    dispatcher=dispatcher or crawl_dispatcher,
                    **kwargs,
                )

            crawler.arun_many = arun_many_with_dispatcher
            result = await crawler.arun(start_url, config=config)

            if isinstance(result, list):
                failed_fetch_urls: list[str] = []
                skipped_invalid_urls: list[str] = []
                skipped_ingestion_rows: list[str] = []
                metrics = {
                    "total_results": len(result),
                    "finished_reason": "running",
                    "target_valid_pages": max_pages,
                    "crawl_budget_pages": crawl_budget_pages,
                    "accepted_valid_pages": 0,
                    "successful_results": 0,
                    "saved_docs": 0,
                    "saved_markdown_files": 0,
                    "skipped_invalid_content": 0,
                    "skipped_ingestion": 0,
                    "skipped_save_markdown": 0,
                    "skipped_processing_errors": 0,
                    "skipped_db_disabled": 0,
                    "blocked_by_host_filter": host_filter.stats.rejected_urls,
                    "blocked_by_allow_filter": 0,
                    "matched_allow_filter": 0,
                    "blocked_by_block_filter": block_filter.stats.rejected_urls,
                }

                for res in result:
                    if not res.success:
                        failed_fetch_urls.append(res.url or "")
                        if progress_hook:
                            progress_hook(metrics)
                        continue

                    metrics["successful_results"] += 1

                    invalid_reason = self._invalid_reason(
                        res.url,
                        res.metadata.get("title", ""),
                        res.markdown,
                        min_content_words=min_content_words,
                    )
                    if invalid_reason:
                        metrics["skipped_invalid_content"] += 1
                        skipped_invalid_urls.append(f"{res.url}\t{invalid_reason}")
                        if progress_hook:
                            progress_hook(metrics)
                        continue
                    metrics["accepted_valid_pages"] += 1
                    if use_allow_filter and self._matches_allow_priority(
                        res.url, res.metadata.get("title", "")
                    ):
                        # Modo no excluyente: solo marca páginas de alta prioridad.
                        metrics["matched_allow_filter"] += 1

                    try:
                        if save_markdown_files:
                            saved_file, save_reason = self._save_markdown_to_disk(
                                res.url,
                                res.metadata.get("title", ""),
                                res.markdown,
                            )
                            if saved_file:
                                metrics["saved_markdown_files"] += 1
                            else:
                                metrics["skipped_save_markdown"] += 1
                                skipped_ingestion_rows.append(f"{res.url}\t{save_reason}")

                        if persist_to_db:
                            async with async_session() as session:
                                ingestion_result = await self.ingestor.process_and_save(
                                    url=res.url,
                                    title=res.metadata.get("title", ""),
                                    content=res.markdown,
                                    session=session,
                                )
                            if ingestion_result.get("saved"):
                                metrics["saved_docs"] += 1
                            else:
                                metrics["skipped_ingestion"] += 1
                                reason = ingestion_result.get("reason", "unknown")
                                skipped_ingestion_rows.append(f"{res.url}\t{reason}")
                        else:
                            metrics["skipped_db_disabled"] += 1
                    except Exception as exc:  # noqa: BLE001
                        metrics["skipped_processing_errors"] += 1
                        skipped_ingestion_rows.append(
                            f"{res.url}\tprocessing_error:{exc.__class__.__name__}"
                        )
                        if progress_hook:
                            progress_hook(metrics)
                        continue

                    if progress_hook:
                        progress_hook(metrics)

                    if (
                        count_valid_pages_only
                        and metrics["accepted_valid_pages"] >= max_pages
                    ):
                        metrics["finished_reason"] = "target_reached"
                        break

                if metrics["finished_reason"] == "running":
                    metrics["finished_reason"] = "frontier_exhausted"
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
