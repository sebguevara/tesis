"""
Scraping Service — HTML page extraction with PDF link collection.

Primary: Crawl4AI with pruning content filter and markdown generation.
Fallback: httpx + BeautifulSoup for pages where Crawl4AI fails.
Collects PDF links from anchors during scraping for later processing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

logger = logging.getLogger(__name__)


@dataclass
class ScrapeResult:
    """Result of scraping a single page."""

    title: str = ""
    markdown: str = ""
    pdf_links: list[str] = field(default_factory=list)
    success: bool = False


class ScrapingService:
    """Scrapes HTML pages and collects PDF links discovered during crawling."""

    # Stage 1 speed-up: cache writes so re-crawls of the same URL within
    # a session reuse fetches. page_timeout was tried at 20_000 first but
    # cut too many slow UNNE pages (correctness dropped 10pts), so we
    # settled on 45_000 — still ~25% faster than the 60_000 default.
    PAGE_TIMEOUT_MS = 45_000
    DEFAULT_CACHE_MODE = CacheMode.WRITE_ONLY

    def __init__(self):
        self.prune = PruningContentFilter(threshold=0.25, min_word_threshold=8)
        self.md_gen = DefaultMarkdownGenerator(content_filter=self.prune)
        self.config = CrawlerRunConfig(
            target_elements=[
                "main",
                ".site-main",
                ".content-area",
                ".post-content",
                ".entry-content",
                ".elementor-widget-theme-post-content",
                ".elementor-tab-content",
                ".e-n-tabs-content",
                "article",
                "body",
            ],
            excluded_tags=["header", "footer", "nav", "aside", "form"],
            excluded_selector=".post-navigation, .sharedaddy, .comments-area",
            exclude_external_links=True,
            markdown_generator=self.md_gen,
            cache_mode=self.DEFAULT_CACHE_MODE,
            page_timeout=self.PAGE_TIMEOUT_MS,
        )

    def _config_for_url(self, url: str) -> CrawlerRunConfig:
        low = (url or "").lower()
        if any(token in low for token in ("/carreras/", "/plan-de-estudios", "/distribucion-de-asignaturas")):
            # Career pages often contain short curriculum rows (e.g., "Materia: ...")
            # that get dropped by aggressive pruning.
            relaxed_prune = PruningContentFilter(threshold=0.08, min_word_threshold=1)
            relaxed_md = DefaultMarkdownGenerator(content_filter=relaxed_prune)
            return CrawlerRunConfig(
                target_elements=[
                    "main",
                    ".site-main",
                    ".content-area",
                    ".post-content",
                    ".entry-content",
                    ".elementor-widget-theme-post-content",
                    ".elementor-tab-content",
                    ".e-n-tabs-content",
                    "article",
                    "body",
                ],
                excluded_tags=["header", "footer", "nav", "aside", "form"],
                excluded_selector=".post-navigation, .sharedaddy, .comments-area",
                exclude_external_links=True,
                markdown_generator=relaxed_md,
                cache_mode=self.DEFAULT_CACHE_MODE,
                page_timeout=self.PAGE_TIMEOUT_MS,
            )
        return self.config

    async def scrape_page(self, url: str) -> ScrapeResult:
        """
        Scrape a page and return its title, markdown content, and discovered PDF links.

        Tries Crawl4AI first, falls back to httpx + BeautifulSoup.
        """
        result = ScrapeResult()

        try:
            async with AsyncWebCrawler() as crawler:
                crawl_result = await crawler.arun(url, config=self._config_for_url(url))

                if crawl_result.success and crawl_result.markdown:
                    result.title = crawl_result.metadata.get("title", "")
                    result.markdown = crawl_result.markdown
                    result.success = True

                    # Extract PDF links from the raw HTML
                    if crawl_result.html:
                        result.pdf_links = self._extract_pdf_links(
                            crawl_result.html, url
                        )

                    return result
        except Exception as exc:
            logger.debug("Crawl4AI failed for %s: %s, trying fallback", url, exc)

        # ── Fallback: httpx + BeautifulSoup ──────────────────────────
        return await self._fallback_scrape(url)

    async def _fallback_scrape(self, url: str) -> ScrapeResult:
        """Fallback scraper using httpx and BeautifulSoup."""
        result = ScrapeResult()
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(30.0),
                verify=False,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text

            soup = BeautifulSoup(html, "html.parser")

            # Extract PDF links before cleaning
            result.pdf_links = self._extract_pdf_links_from_soup(soup, url)

            # Clean navigation / noise
            for selector in [
                "header", "footer", "nav", "aside", "form",
                ".post-navigation", ".sharedaddy", ".comments-area",
            ]:
                for node in soup.select(selector):
                    node.decompose()

            # Extract main content
            content_el = (
                soup.select_one("main")
                or soup.select_one(".entry-content")
                or soup.select_one(".elementor-widget-theme-post-content")
                or soup.select_one(".elementor-tab-content")
                or soup.select_one(".e-n-tabs-content")
                or soup.select_one("article")
                or soup.select_one("body")
            )

            result.title = soup.title.string.strip() if soup.title and soup.title.string else ""

            if content_el:
                # Convert to simple markdown-like text
                result.markdown = self._html_to_markdown(content_el)
                result.success = bool(result.markdown.strip())

        except Exception as exc:
            logger.warning("Fallback scrape failed for %s: %s", url, exc)

        return result

    def _extract_pdf_links(self, html: str, base_url: str) -> list[str]:
        """Extract PDF links from raw HTML string."""
        soup = BeautifulSoup(html, "html.parser")
        return self._extract_pdf_links_from_soup(soup, base_url)

    @staticmethod
    def _extract_pdf_links_from_soup(soup: BeautifulSoup, base_url: str) -> list[str]:
        """Extract unique, absolute PDF URLs from anchor tags."""
        pdf_links: list[str] = []
        seen: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href:
                continue

            # Check if it links to a PDF
            href_lower = href.lower()
            if not (href_lower.endswith(".pdf") or ".pdf?" in href_lower):
                continue

            # Build absolute URL
            absolute_url = urljoin(base_url, href)

            # Normalize for dedup
            normalized = absolute_url.split("#")[0].split("?")[0]
            if normalized not in seen:
                seen.add(normalized)
                pdf_links.append(absolute_url)

        return pdf_links

    @staticmethod
    def _html_to_markdown(element) -> str:
        """Convert a BeautifulSoup element to basic markdown text."""
        lines: list[str] = []

        for tag in element.find_all(True):
            text = tag.get_text(strip=True)
            if not text:
                continue
            tag_name = tag.name

            if tag_name in ("h1",):
                lines.append(f"# {text}")
            elif tag_name in ("h2",):
                lines.append(f"## {text}")
            elif tag_name in ("h3",):
                lines.append(f"### {text}")
            elif tag_name in ("h4", "h5", "h6"):
                lines.append(f"#### {text}")
            elif tag_name in ("p", "div", "section", "td", "th"):
                lines.append(text)
            elif tag_name in ("li",):
                lines.append(f"- {text}")

        # Deduplicate consecutive identical lines
        cleaned: list[str] = []
        for line in lines:
            if not cleaned or line != cleaned[-1]:
                cleaned.append(line)

        return "\n\n".join(cleaned)
