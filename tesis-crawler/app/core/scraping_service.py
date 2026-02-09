import httpx
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator


class ScrapingService:
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
            cache_mode=CacheMode.BYPASS,
        )

    async def scrape_page(self, url: str):
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url, config=self.config)
            if result.success and result.markdown:
                return result.metadata.get("title"), result.markdown

            async with httpx.AsyncClient() as client:
                resp = await client.get(url)
                soup = BeautifulSoup(resp.text, "html.parser")
                for selector in [
                    "header",
                    "footer",
                    "nav",
                    "aside",
                    "form",
                    ".post-navigation",
                    ".sharedaddy",
                    ".comments-area",
                ]:
                    for node in soup.select(selector):
                        node.decompose()

                content = (
                    soup.select_one(".entry-content")
                    or soup.select_one(".elementor-widget-theme-post-content")
                    or soup.select_one(".elementor-tab-content")
                    or soup.select_one(".e-n-tabs-content")
                    or soup.select_one("article")
                )
                return (
                    soup.title.string if soup.title else "",
                    content.get_text() if content else "",
                )
