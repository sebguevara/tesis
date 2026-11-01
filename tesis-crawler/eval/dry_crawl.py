"""
Dry-run crawl: no DB writes, no OpenAI calls. Writes coverage + skip reasons +
per-page markdown to crawl_debug/<host>/ so extraction can be inspected.

    uv run python eval/dry_crawl.py https://www.arq.unne.edu.ar/ --max-pages 3000
"""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--max-pages", type=int, default=3000)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--max-depth", type=int, default=6)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    out = Path("crawl_debug") / urlparse(args.url).netloc
    os.environ["SITE_MD_DIR"] = str(out / "md")
    from app.config import settings
    settings.SITE_MD_DIR = str(out / "md")
    from app.tasks.worker import CrawlWorker

    last = {"n": 0}

    def progress(m: dict) -> None:
        n = m.get("processed_results", 0)
        if n - last["n"] >= 25:
            last["n"] = n
            print(f"processed={n} accepted={m.get('accepted_valid_pages')} "
                  f"skipped={m.get('skipped_invalid_content')} topup={m.get('sitemap_topup_urls')}",
                  flush=True)

    await CrawlWorker().run_institutional_crawl(
        progress_hook=progress,
        start_url=args.url,
        max_pages=args.max_pages,
        concurrency=args.concurrency,
        max_depth=args.max_depth,
        persist_to_db=False,
        save_markdown_files=True,
        debug_output_dir=str(out),
    )
    print(json.dumps(json.loads((out / "summary.json").read_text("utf-8")), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
