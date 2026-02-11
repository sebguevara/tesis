import asyncio
from datetime import datetime

from app.tasks.worker import CrawlWorker

START_URL = "https://arq.unne.edu.ar"
MAX_PAGES = 5000
CONCURRENCY = 10
MAX_DEPTH = 5


def fmt_metrics(m: dict) -> str:
    return (
        f"processed={int(m.get('processed_results', 0))} "
        f"accepted={int(m.get('accepted_valid_pages', 0))} "
        f"saved_docs={int(m.get('saved_docs', 0))} "
        f"skipped_invalid={int(m.get('skipped_invalid_content', 0))} "
        f"skipped_ingestion={int(m.get('skipped_ingestion', 0))} "
        f"pdf_found={int(m.get('pdf_links_found', 0))} "
        f"pdf_processed={int(m.get('pdf_processed', 0))} "
        f"pdf_saved={int(m.get('pdf_saved', 0))} "
        f"pdf_errors={int(m.get('pdf_errors', 0))} "
        f"finished_reason={m.get('finished_reason', 'running')}"
    )


async def main() -> None:
    worker = CrawlWorker()
    started = datetime.now()
    last_print = {"processed": -1, "pdf_processed": -1}

    def on_progress(metrics: dict):
        processed = int(metrics.get("processed_results", 0))
        pdf_processed = int(metrics.get("pdf_processed", 0))
        should_print = False
        if processed >= 0 and (processed % 100 == 0) and processed != last_print["processed"]:
            should_print = True
            last_print["processed"] = processed
        if pdf_processed >= 0 and (pdf_processed % 50 == 0) and pdf_processed != last_print["pdf_processed"] and pdf_processed > 0:
            should_print = True
            last_print["pdf_processed"] = pdf_processed
        if metrics.get("finished_reason") in {"phase1_done", "completed", "target_reached"}:
            should_print = True
        if should_print:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"[{now}] {fmt_metrics(metrics)}", flush=True)

    print("Starting clean re-scrape...", flush=True)
    await worker.run_institutional_crawl(
        start_url=START_URL,
        max_pages=MAX_PAGES,
        concurrency=CONCURRENCY,
        max_depth=MAX_DEPTH,
        persist_to_db=True,
        save_markdown_files=False,
        use_allow_filter=True,
        min_content_words=5,
        count_valid_pages_only=True,
        block_old_years=True,
        debug_output_dir="./crawl_debug/rescrape-clean-run",
        progress_hook=on_progress,
    )
    elapsed = (datetime.now() - started).total_seconds()
    print(f"Re-scrape finished in {elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
