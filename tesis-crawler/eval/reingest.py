"""
Re-scrape + re-ingest a list of URLs (one per line), e.g. pages a filter
blocked by mistake. Same path as POST /api/scrape/refresh-page, without the API.

    python eval/reingest.py crawl_debug/reingest_urls.txt --concurrency 3
"""
import argparse
import asyncio
import collections
from pathlib import Path

from app.core.ingestion_service import IngestionService
from app.core.page_classifier import PageClassifier
from app.core.scraping_service import ScrapingService
from app.storage.db_client import async_session


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("urls_file")
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()
    urls = [u.strip() for u in Path(args.urls_file).read_text("utf-8").splitlines() if u.strip()]

    scraper, ingestor, classifier = ScrapingService(), IngestionService(), PageClassifier()
    sem = asyncio.Semaphore(args.concurrency)
    outcomes: collections.Counter = collections.Counter()

    async def one(url: str) -> None:
        async with sem:
            try:
                res = await scraper.scrape_page(url)
                if not res.success or not (res.markdown or "").strip():
                    outcomes["scrape_failed"] += 1
                    return
                cls = classifier.classify_content(url, res.title or "", res.markdown[:8000])
                async with async_session() as session:
                    saved = await ingestor.process_and_save(
                        url=url, title=res.title or "", content=res.markdown, session=session,
                        page_type=cls.page_type, content_type="html",
                        authority_score=float(cls.authority_score),
                    )
                    await session.commit()
                outcomes[saved.get("reason", "?")] += 1
            except Exception as exc:
                outcomes[f"error:{exc.__class__.__name__}"] += 1
            done = sum(outcomes.values())
            if done % 20 == 0:
                print(done, dict(outcomes), flush=True)

    await asyncio.gather(*(one(u) for u in urls))
    print("FINAL", dict(outcomes))


if __name__ == "__main__":
    asyncio.run(main())
