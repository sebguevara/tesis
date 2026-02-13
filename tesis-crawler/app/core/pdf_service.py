"""
PDF Processing Service — Downloads PDFs and converts them to clean Markdown.

Uses PyMuPDF (pymupdf) to extract text with structure preservation.
Processes multiple PDFs in parallel with semaphore-based concurrency control.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import httpx
import pymupdf

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class PDFResult:
    """Result of downloading and converting a single PDF."""

    url: str
    title: str = ""
    markdown_content: str = ""
    page_count: int = 0
    file_size_bytes: int = 0
    original_filename: str = ""
    content_hash: str = ""
    metadata: dict = field(default_factory=dict)
    success: bool = False
    error: Optional[str] = None


class PDFService:
    """Downloads institutional PDFs and converts them to Markdown for RAG ingestion."""

    MAX_PDF_SIZE: int = (settings.MAX_PDF_SIZE_MB if hasattr(settings, "MAX_PDF_SIZE_MB") else 50) * 1024 * 1024
    MAX_DOC_SIZE: int = (settings.PDF_DOC_MAX_SIZE_MB if hasattr(settings, "PDF_DOC_MAX_SIZE_MB") else 15) * 1024 * 1024
    MAX_DOC_PAGES: int = int(getattr(settings, "PDF_DOC_MAX_PAGES", 120) or 120)
    DOWNLOAD_TIMEOUT: int = 90
    HEADING_FONT_THRESHOLD: float = 14.0  # Points; text larger than this is treated as heading
    MIN_TEXT_LENGTH: int = 50  # Minimum chars for a PDF to be considered valid

    # ── Public API ───────────────────────────────────────────────────────

    async def download_and_convert(self, url: str) -> Optional[PDFResult]:
        """Download a single PDF and convert it to Markdown."""
        result = PDFResult(url=url, original_filename=self._filename_from_url(url))
        try:
            pdf_bytes = await self._download_pdf(url)
            if pdf_bytes is None:
                result.error = "download_failed"
                return result
            result.file_size_bytes = len(pdf_bytes)
            result.content_hash = hashlib.sha256(pdf_bytes).hexdigest()
            return self._pdf_to_markdown(pdf_bytes, result)
        except httpx.TimeoutException:
            result.error = "download_timeout"
            logger.warning("PDF download timeout: %s", url)
            return result
        except Exception as exc:  # noqa: BLE001
            result.error = f"processing_error:{exc.__class__.__name__}"
            logger.warning("PDF processing failed for %s: %s", url, exc)
            return result

    async def process_batch(
        self,
        urls: list[str],
        max_concurrent: int | None = None,
    ) -> list[PDFResult]:
        """Process multiple PDFs in parallel with controlled concurrency."""
        concurrency = max_concurrent or getattr(settings, "PDF_CONCURRENCY", 5)
        semaphore = asyncio.Semaphore(concurrency)

        async def _limited(url: str) -> PDFResult:
            async with semaphore:
                result = await self.download_and_convert(url)
                if result is None:
                    return PDFResult(url=url, error="null_result")
                return result

        tasks = [_limited(u) for u in urls]
        return list(await asyncio.gather(*tasks, return_exceptions=False))

    # ── Private helpers ──────────────────────────────────────────────────

    async def _download_pdf(self, url: str) -> Optional[bytes]:
        """Download PDF bytes, respecting size limits."""
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(self.DOWNLOAD_TIMEOUT),
                verify=False,  # Institutional sites sometimes have bad SSL
            ) as client:
                # HEAD request to check size first
                try:
                    head = await client.head(url)
                    content_length = int(head.headers.get("content-length", 0))
                    if content_length > self.MAX_PDF_SIZE:
                        logger.warning("PDF too large (%d bytes): %s", content_length, url)
                        return None
                except (httpx.HTTPError, ValueError):
                    pass  # Proceed anyway; some servers don't support HEAD

                response = await client.get(url)
                response.raise_for_status()

                content_type = (response.headers.get("content-type") or "").lower()
                if "pdf" not in content_type and not url.lower().endswith(".pdf"):
                    logger.warning("URL does not return PDF content-type: %s (%s)", url, content_type)
                    return None

                if len(response.content) > self.MAX_PDF_SIZE:
                    logger.warning("PDF exceeds size limit after download: %s", url)
                    return None
                if len(response.content) > self.MAX_DOC_SIZE:
                    logger.info("PDF skipped by doc-size limit (%d bytes): %s", len(response.content), url)
                    return None

                return response.content
        except httpx.HTTPStatusError as exc:
            logger.warning("HTTP %d for PDF: %s", exc.response.status_code, url)
            return None
        except httpx.HTTPError as exc:
            logger.warning("HTTP error downloading PDF %s: %s", url, exc)
            return None

    def _pdf_to_markdown(self, pdf_bytes: bytes, result: PDFResult) -> PDFResult:
        """Convert raw PDF bytes to structured Markdown."""
        try:
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        except Exception as exc:  # noqa: BLE001
            result.error = f"pdf_open_error:{exc.__class__.__name__}"
            logger.warning("Cannot open PDF %s: %s", result.url, exc)
            return result

        result.page_count = len(doc)
        if result.page_count > self.MAX_DOC_PAGES:
            result.error = "too_many_pages"
            doc.close()
            return result

        # Extract metadata
        meta = doc.metadata or {}
        result.metadata = {
            "author": (meta.get("author") or "").strip(),
            "subject": (meta.get("subject") or "").strip(),
            "creator": (meta.get("creator") or "").strip(),
            "producer": (meta.get("producer") or "").strip(),
            "creation_date": (meta.get("creationDate") or "").strip(),
            "modification_date": (meta.get("modDate") or "").strip(),
            "keywords": (meta.get("keywords") or "").strip(),
        }

        # Title: prefer metadata, fallback to filename
        pdf_title = (meta.get("title") or "").strip()
        if not pdf_title or pdf_title.lower() in {"untitled", "microsoft word", ""}:
            pdf_title = self._title_from_filename(result.original_filename)
        result.title = pdf_title

        # Extract text page by page with structure
        markdown_parts: list[str] = []
        for page_num in range(len(doc)):
            page = doc[page_num]
            page_md = self._extract_page_markdown(page, page_num + 1)
            if page_md.strip():
                markdown_parts.append(page_md)

        doc.close()

        full_markdown = "\n\n".join(markdown_parts).strip()

        # Clean up the markdown
        full_markdown = self._clean_pdf_markdown(full_markdown)

        if len(full_markdown) < self.MIN_TEXT_LENGTH:
            result.error = "insufficient_text"
            result.markdown_content = full_markdown
            return result

        result.markdown_content = full_markdown
        result.success = True
        return result

    def _extract_page_markdown(self, page: pymupdf.Page, page_number: int) -> str:
        """Extract text from a single PDF page, preserving some structure."""
        blocks = page.get_text("dict", sort=True).get("blocks", [])
        lines: list[str] = []
        prev_font_size = 0.0

        for block in blocks:
            if block.get("type") != 0:  # Only text blocks
                continue

            for line_data in block.get("lines", []):
                spans = line_data.get("spans", [])
                if not spans:
                    continue

                line_text_parts: list[str] = []
                max_font_size = 0.0
                is_bold = False

                for span in spans:
                    text = (span.get("text") or "").strip()
                    if not text:
                        continue
                    font_size = span.get("size", 12.0)
                    max_font_size = max(max_font_size, font_size)
                    flags = span.get("flags", 0)
                    if flags & 2**4:  # Bold flag
                        is_bold = True
                    line_text_parts.append(text)

                line_text = " ".join(line_text_parts).strip()
                if not line_text:
                    continue

                # Detect headings by font size
                if max_font_size >= self.HEADING_FONT_THRESHOLD + 4:
                    line_text = f"## {line_text}"
                elif max_font_size >= self.HEADING_FONT_THRESHOLD:
                    line_text = f"### {line_text}"
                elif is_bold and len(line_text) < 120 and not line_text.endswith("."):
                    line_text = f"**{line_text}**"

                lines.append(line_text)
                prev_font_size = max_font_size

        return "\n".join(lines)

    def _clean_pdf_markdown(self, content: str) -> str:
        """Clean up PDF-extracted markdown: remove noise, fix spacing."""
        lines = content.splitlines()
        cleaned: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                # Keep empty lines for paragraph separation but not too many
                if cleaned and cleaned[-1] != "":
                    cleaned.append("")
                continue

            # Skip page headers/footers that are just numbers
            if re.match(r"^\d{1,3}$", stripped):
                continue
            # Skip lines that are only dots, dashes, underscores (decorative)
            if re.match(r"^[.\-_=]{3,}$", stripped):
                continue
            # Skip lines like "Página X de Y"
            if re.match(r"^[Pp]ágina\s+\d+\s+de\s+\d+$", stripped):
                continue
            if re.match(r"^[Pp]age\s+\d+\s+of\s+\d+$", stripped):
                continue

            cleaned.append(stripped)

        result = "\n".join(cleaned).strip()
        # Collapse multiple blank lines
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result

    @staticmethod
    def _filename_from_url(url: str) -> str:
        """Extract the filename from a URL."""
        parsed = urlparse(url)
        path = unquote(parsed.path or "")
        parts = path.rsplit("/", 1)
        if len(parts) == 2 and parts[1]:
            return parts[1]
        return ""

    @staticmethod
    def _title_from_filename(filename: str) -> str:
        """Convert a filename like 'plan-de-estudios-medicina.pdf' to a readable title."""
        name = filename.rsplit(".", 1)[0] if "." in filename else filename
        name = name.replace("-", " ").replace("_", " ")
        name = re.sub(r"\s+", " ", name).strip()
        if name:
            return name.title()
        return ""

    def save_pdf_to_disk(self, pdf_bytes: bytes, url: str) -> Optional[Path]:
        """Save raw PDF to the configured storage directory for auditing."""
        pdf_dir = Path(getattr(settings, "PDF_STORAGE_DIR", "./data/pdf"))
        pdf_dir.mkdir(parents=True, exist_ok=True)
        url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        filename = self._filename_from_url(url) or "document.pdf"
        # Sanitize and shorten filename for Windows compatibility
        safe_name = re.sub(r"[^\w.\-]", "_", filename)[:80]
        dest = pdf_dir / f"{url_hash}_{safe_name}"
        try:
            dest.write_bytes(pdf_bytes)
            return dest
        except OSError as exc:
            logger.warning("Cannot save PDF to disk: %s (%s)", dest, exc)
            return None
