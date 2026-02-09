import asyncio
import hashlib
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import delete, select
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from app.config import settings
from app.core.content_filters import is_institutional_news, is_outdated_content
from app.core.domain_utils import domain_variants, normalize_domain
from app.embedding.models import Chunk, Document, Source, utc_now_naive


logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(self):
        self.md_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[("#", "H1"), ("##", "H2"), ("###", "H3")]
        )
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1500, chunk_overlap=300
        )
        self.embeddings = OpenAIEmbeddings(
            model=settings.OPENAI_EMBEDDING_MODEL,
            dimensions=settings.EMBEDDING_DIM,
        )

    @staticmethod
    def _canonicalize_url(raw_url: str) -> str:
        parsed = urlparse((raw_url or "").strip())
        scheme = (parsed.scheme or "https").lower()
        host = normalize_domain(parsed.netloc)
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/" and path.endswith("/"):
            path = path[:-1]

        # Drop common tracking query params but keep functional params.
        kept_params: list[tuple[str, str]] = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            lk = key.lower()
            if (
                lk.startswith("utm_")
                or lk in {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}
            ):
                continue
            kept_params.append((key, value))
        kept_params.sort(key=lambda item: (item[0].lower(), item[1]))
        query = urlencode(kept_params, doseq=True)

        return urlunparse((scheme, host, path or "/", "", query, ""))

    @staticmethod
    def _clean_markdown(content: str) -> str:
        lines = [ln.strip() for ln in content.splitlines()]
        cleaned: list[str] = []
        line_counts: dict[str, int] = {}

        for line in lines:
            if not line:
                continue
            # Cards/teasers with linked image only.
            if re.match(r"^\[!\[[^\]]*\]\([^)]+\)\]\([^)]+\)$", line):
                continue
            # Standalone image markdown.
            if re.match(r"^!\[[^\]]*\]\([^)]+\)$", line):
                continue

            normalized = line.lower()
            max_repetitions = 2
            if "área de prensa" in normalized:
                max_repetitions = 1
            count = line_counts.get(normalized, 0)
            if count >= max_repetitions:
                continue

            line_counts[normalized] = count + 1
            cleaned.append(line)

        return "\n".join(cleaned).strip()

    async def process_and_save(self, url: str, title: str, content: str, session):
        canonical_url = self._canonicalize_url(url)
        clean_content = self._clean_markdown(content or "")
        if not clean_content:
            return {"saved": False, "reason": "empty_content"}
        normalized = clean_content.lower()
        if "página no encontrada" in normalized or "pagina no encontrada" in normalized:
            return {"saved": False, "reason": "not_found_content"}
        if is_institutional_news(url, title, clean_content):
            return {"saved": False, "reason": "institutional_news"}
        if is_outdated_content(url, title, clean_content):
            return {"saved": False, "reason": "outdated_content"}

        doc_hash = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()

        parsed_url = urlparse(canonical_url)
        domain = normalize_domain(parsed_url.netloc)
        source_result = await session.execute(
            select(Source).where(Source.domain.in_(list(domain_variants(domain))))
        )
        source_rows = source_result.scalars().all()
        source = source_rows[0] if source_rows else None
        if source is None:
            source = Source(domain=domain)
            session.add(source)
            await session.flush()

        existing_by_url = await session.execute(
            select(Document).where(Document.canonical_url == canonical_url)
        )
        existing_doc = existing_by_url.scalar_one_or_none()
        if existing_doc is not None and existing_doc.content_hash == doc_hash:
            return {"saved": False, "reason": "duplicate_content"}

        existing_same_hash = await session.execute(
            select(Document)
            .where(Document.source_id == source.source_id)
            .where(Document.content_hash == doc_hash)
            .where(Document.canonical_url != canonical_url)
            .limit(1)
        )
        same_hash_doc = existing_same_hash.scalar_one_or_none()
        if same_hash_doc is not None:
            return {
                "saved": False,
                "reason": "duplicate_content_other_url",
                "canonical_url": same_hash_doc.canonical_url,
            }

        if existing_doc is None:
            doc = Document(
                source_id=source.source_id,
                url=canonical_url,
                canonical_url=canonical_url,
                title=title,
                content_hash=doc_hash,
                page_type="academic",
            )
            session.add(doc)
            await session.flush()
        else:
            doc = existing_doc
            doc.source_id = source.source_id
            doc.url = canonical_url
            doc.title = title
            doc.content_hash = doc_hash
            doc.page_type = "academic"
            doc.fetched_at = utc_now_naive()
            await session.execute(delete(Chunk).where(Chunk.doc_id == doc.doc_id))

        segments = self.md_splitter.split_text(clean_content)
        chunks = self.text_splitter.split_documents(segments)
        if not chunks:
            await session.rollback()
            return {"saved": False, "reason": "no_chunks"}

        chunk_texts = [c.page_content for c in chunks]
        embeddings: list[list[float]]
        try:
            embeddings = await asyncio.to_thread(self.embeddings.embed_documents, chunk_texts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding generation failed for %s: %s", url, exc)
            embeddings = [[0.0] * settings.EMBEDDING_DIM for _ in chunk_texts]

        for c, emb in zip(chunks, embeddings):
            heading_path = [
                c.metadata.get("H1"),
                c.metadata.get("H2"),
                c.metadata.get("H3"),
            ]
            heading_path = [h for h in heading_path if h]

            chunk = Chunk(
                doc_id=doc.doc_id,
                text=c.page_content,
                heading_path=heading_path,
                embedding=emb,
            )
            session.add(chunk)
        await session.commit()
        return {"saved": True, "reason": "saved" if existing_doc is None else "updated"}
