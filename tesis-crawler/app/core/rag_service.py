import asyncio
import re
from typing import List, TypedDict
from urllib.parse import urljoin, urlparse
from uuid import UUID

import httpx
from bs4 import BeautifulSoup
from langgraph.graph import END, StateGraph
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlalchemy import text

from app.config import settings
from app.core.content_filters import is_institutional_news, is_outdated_content
from app.core.domain_utils import domain_variants, normalize_domain
from app.core.scraping_service import ScrapingService
from app.llm.prompts import SYSTEM_RAG
from app.storage.db_client import async_session


class AgentState(TypedDict):
    query: str
    context: List[str]
    response: str
    history: List[str]
    source_id: str | None


class RAGService:
    def __init__(self):
        self.llm = ChatOpenAI(model=settings.OPENAI_CHAT_MODEL, api_key=settings.OPENAI_API_KEY)
        self.embeddings = OpenAIEmbeddings(
            model=settings.OPENAI_EMBEDDING_MODEL, dimensions=settings.EMBEDDING_DIM
        )
        self.scraper = ScrapingService()

    def build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("retrieve", self.retrieve)
        workflow.add_node("generate", self.generate)
        workflow.set_entry_point("retrieve")
        workflow.add_edge("retrieve", "generate")
        workflow.add_edge("generate", END)
        return workflow.compile()

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^\wáéíóúñü]+", " ", value, flags=re.IGNORECASE)).strip()

    @staticmethod
    def _normalize_query_typos(query: str) -> str:
        q = query or ""
        replacements = (
            (r"\bcarrea\b", "carrera"),
            (r"\bingrsantes\b", "ingresantes"),
            (r"\bingrersantes\b", "ingresantes"),
            (r"\badminiones\b", "admisiones"),
            (r"\bduraccion\b", "duracion"),
            (r"\bingenieriaa\b", "ingenieria"),
        )
        for pattern, repl in replacements:
            q = re.sub(pattern, repl, q, flags=re.IGNORECASE)
        return q

    @staticmethod
    def _expand_lexical_queries(query: str) -> list[str]:
        fixed_query = RAGService._normalize_query_typos(query)
        q = fixed_query.lower()
        expanded = [fixed_query]
        if any(
            token in q
            for token in (
                "carrera",
                "carreras",
                "programa",
                "programas",
                "curso",
                "cursos",
                "oferta",
                "grado",
                "pregrado",
                "posgrado",
            )
        ):
            expanded.append(
                "oferta académica carreras programas cursos tecnicaturas licenciaturas"
            )
        if "cuanta" in q or "cuánta" in q or "numero" in q or "número" in q:
            expanded.append("cantidad total cuántas cuantas número numero")
        if any(
            token in q
            for token in ("inscripcion", "inscripción", "admisión", "admision", "requisito")
        ):
            expanded.append("inscripción admisión requisitos documentación pasos")
        if any(token in q for token in ("duracion", "duración", "años", "anios", "dura", "cuantos años")):
            expanded.append("duración duracion años anios tiempo cursado")
        if any(token in q for token in ("director", "coordinador", "responsable", "autoridad")):
            expanded.append("director coordinador responsable de carrera contacto correo")
        return [RAGService._normalize_text(x) for x in expanded if RAGService._normalize_text(x)]

    @staticmethod
    def _needs_url_hints(query: str) -> bool:
        q = query.lower()
        return any(
            token in q
            for token in (
                "carrera",
                "programa",
                "curso",
                "oferta",
                "inscripcion",
                "inscripción",
                "admisión",
                "admision",
                "requisito",
                "tramite",
                "trámite",
                "arancel",
                "duracion",
                "duración",
                "modalidad",
                "contacto",
            )
        )

    @staticmethod
    def _extract_query_tokens(query: str) -> list[str]:
        tokens = re.findall(r"[a-záéíóúñü0-9]{4,}", query.lower())
        stopwords = {
            "donde",
            "como",
            "cuando",
            "sobre",
            "para",
            "desde",
            "hasta",
            "tengo",
            "quiero",
            "necesito",
            "informacion",
            "información",
        }
        return [t for t in tokens if t not in stopwords]

    @staticmethod
    def _is_valid_https_source(source_url: str | None) -> bool:
        if not source_url:
            return False
        parsed = urlparse(source_url)
        return parsed.scheme.lower() == "https" and bool(parsed.netloc)

    @staticmethod
    def _needs_source_fallback(contexts: list[str], query: str) -> bool:
        if len(contexts) < 2:
            return True
        q = (query or "").lower()
        joined = "\n".join(contexts).lower()
        if any(t in q for t in ("director", "coordinador", "responsable", "autoridad")):
            return "director" not in joined and "coordinador" not in joined
        if any(t in q for t in ("duracion", "duración", "años", "anios", "dura")):
            return (
                "duración" not in joined
                and "duracion" not in joined
                and "años" not in joined
                and "anios" not in joined
            )
        return False

    async def _discover_candidate_urls(self, source_url: str, query: str, limit: int = 5) -> list[str]:
        parsed_source = urlparse(source_url)
        base_host = parsed_source.netloc.lower()
        tokens = self._extract_query_tokens(query)

        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(source_url)
                if resp.status_code >= 400:
                    return [source_url]
                soup = BeautifulSoup(resp.text, "html.parser")
        except Exception:  # noqa: BLE001
            return [source_url]

        scored_links: list[tuple[int, str]] = []
        for anchor in soup.select("a[href]"):
            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            abs_url = urljoin(source_url, href)
            parsed = urlparse(abs_url)
            if parsed.scheme.lower() != "https":
                continue
            if parsed.netloc.lower() != base_host:
                continue
            link_text = (anchor.get_text(" ", strip=True) or "").lower()
            haystack = f"{abs_url.lower()} {link_text}"
            score = 0
            for tok in tokens:
                if tok in haystack:
                    score += 2
            if any(
                p in haystack
                for p in (
                    "admision",
                    "admisión",
                    "ingreso",
                    "ingresante",
                    "inscripcion",
                    "inscripción",
                    "carreras",
                    "programas",
                    "oferta",
                    "plan de estudios",
                    "requisitos",
                    "tramites",
                    "trámites",
                )
            ):
                score += 3
            if score > 0:
                scored_links.append((score, abs_url))

        ranked = sorted(scored_links, key=lambda x: x[0], reverse=True)
        unique: list[str] = [source_url]
        seen = {source_url}
        for _, link in ranked:
            if link in seen:
                continue
            seen.add(link)
            unique.append(link)
            if len(unique) >= max(1, limit):
                break
        return unique

    async def _retrieve_from_source(self, source_url: str, query: str) -> list[str]:
        if not self._is_valid_https_source(source_url):
            return []
        candidate_urls = await self._discover_candidate_urls(source_url, query, limit=5)
        contexts: list[str] = []
        for url in candidate_urls:
            try:
                title, content = await self.scraper.scrape_page(url)
            except Exception:  # noqa: BLE001
                continue
            title = (title or "").strip()
            content = (content or "").strip()
            if len(content.split()) < 20:
                continue
            if is_institutional_news(url, title, content):
                continue
            if is_outdated_content(url, title, content):
                continue
            excerpt = content[:3500]
            contexts.append(f"URL: {url}\nTitulo: {title}\nContenido: {excerpt}")
            if len(contexts) >= 4:
                break
        return contexts

    async def _resolve_source_scope(self, source_id: str) -> tuple[str, str, str] | None:
        stmt = text(
            """
            SELECT s.domain AS domain
            FROM sources s
            WHERE s.source_id = CAST(:source_id AS uuid)
            LIMIT 1
            """
        )
        async with async_session() as session:
            row = (await session.execute(stmt, {"source_id": source_id})).mappings().first()
        if not row:
            return None
        domain = normalize_domain((row.get("domain") or "").strip().lower())
        if not domain:
            return None
        variants = sorted(domain_variants(domain))
        return variants[0], variants[1], f"https://{domain}/"

    @staticmethod
    def _rank_context_blocks(contexts: list[str], query: str) -> list[str]:
        tokens = RAGService._extract_query_tokens(query)
        scored: list[tuple[int, str]] = []
        for block in contexts:
            haystack = (block or "").lower()
            score = sum(2 for tok in tokens if tok in haystack)
            if "director" in query.lower() and "director" in haystack:
                score += 5
            if any(t in query.lower() for t in ("duracion", "duración", "años", "anios")) and (
                "duración" in haystack or "duracion" in haystack or "años" in haystack
            ):
                score += 5
            scored.append((score, block))
        ranked = sorted(scored, key=lambda item: item[0], reverse=True)
        return [block for _, block in ranked]

    @staticmethod
    def _extract_answer_from_context(query: str, context_blocks: list[str]) -> str | None:
        q = (query or "").lower()
        if not context_blocks:
            return None

        def _extract_url(block: str) -> str:
            for line in block.splitlines():
                if line.startswith("URL:"):
                    return line.replace("URL:", "").strip()
            return ""

        if any(t in q for t in ("duracion", "duración", "años", "anios", "dura")):
            for block in context_blocks:
                match = re.search(
                    r"(?:^|\n)\s*#{0,3}\s*duraci[oó]n\s*:\s*([^\n]+)",
                    block,
                    flags=re.IGNORECASE,
                )
                if match:
                    value = match.group(1).strip()
                    if value:
                        src = _extract_url(block)
                        return f"La duración es {value}. Fuente: {src or 'contexto recuperado'}"

        if any(t in q for t in ("director", "coordinador", "responsable", "autoridad")):
            for block in context_blocks:
                match = re.search(
                    r"director\s+de\s+carrera[^\n]*\n+\s*#{1,4}\s*([^\n]+)",
                    block,
                    flags=re.IGNORECASE,
                )
                if match:
                    value = match.group(1).strip()
                    if value:
                        src = _extract_url(block)
                        return (
                            f"El director de carrera indicado en el sitio es {value}. "
                            f"Fuente: {src or 'contexto recuperado'}"
                        )
        return None

    async def retrieve(self, state: AgentState):
        query = (state.get("query") or "").strip()
        query = self._normalize_query_typos(query)
        source_id = (state.get("source_id") or "").strip()
        if not query:
            return {"context": []}
        if not source_id:
            return {"context": []}
        try:
            UUID(source_id)
        except ValueError:
            return {"context": []}
        source_scope = await self._resolve_source_scope(source_id)
        if not source_scope:
            return {"context": []}
        domain_1, domain_2, source_url = source_scope

        query_vector = await asyncio.to_thread(self.embeddings.embed_query, query)
        vector_literal = "[" + ",".join(f"{value:.8f}" for value in query_vector) + "]"
        lexical_queries = self._expand_lexical_queries(query)

        vector_stmt = text(
            """
            SELECT d.canonical_url AS url, COALESCE(d.title, '') AS title, c.text AS chunk_text
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            JOIN sources s ON s.source_id = d.source_id
            WHERE c.embedding IS NOT NULL
              AND (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
              AND COALESCE(d.page_type, 'academic') IN ('academic', 'evergreen')
              AND d.canonical_url NOT ILIKE '%/noticia/%'
              AND d.canonical_url NOT ILIKE '%/noticias/%'
              AND d.canonical_url NOT ILIKE '%/novedad/%'
              AND d.canonical_url NOT ILIKE '%/novedades/%'
              AND d.canonical_url NOT ILIKE '%/actualidad/%'
              AND d.canonical_url NOT ILIKE '%/prensa/%'
              AND d.canonical_url NOT ILIKE '%/comunicado/%'
              AND d.canonical_url NOT ILIKE '%/evento/%'
              AND d.canonical_url NOT ILIKE '%/agenda/%'
              AND COALESCE(d.title, '') NOT ILIKE '%noticia%'
              AND COALESCE(d.title, '') NOT ILIKE '%novedad%'
              AND COALESCE(d.title, '') NOT ILIKE '%prensa%'
              AND COALESCE(d.title, '') NOT ILIKE '%comunicado%'
              AND COALESCE(d.title, '') NOT ILIKE '%agenda%'
            ORDER BY c.embedding <=> CAST(:query_embedding AS vector)
            LIMIT :k
            """
        )
        lexical_stmt = text(
            """
            SELECT d.canonical_url AS url, COALESCE(d.title, '') AS title, c.text AS chunk_text
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            JOIN sources s ON s.source_id = d.source_id
            WHERE to_tsvector('spanish', COALESCE(d.title, '') || ' ' || c.text)
                  @@ websearch_to_tsquery('spanish', :q)
              AND (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
              AND COALESCE(d.page_type, 'academic') IN ('academic', 'evergreen')
              AND d.canonical_url NOT ILIKE '%/noticia/%'
              AND d.canonical_url NOT ILIKE '%/noticias/%'
              AND d.canonical_url NOT ILIKE '%/novedad/%'
              AND d.canonical_url NOT ILIKE '%/novedades/%'
              AND d.canonical_url NOT ILIKE '%/actualidad/%'
              AND d.canonical_url NOT ILIKE '%/prensa/%'
              AND d.canonical_url NOT ILIKE '%/comunicado/%'
              AND d.canonical_url NOT ILIKE '%/evento/%'
              AND d.canonical_url NOT ILIKE '%/agenda/%'
              AND COALESCE(d.title, '') NOT ILIKE '%noticia%'
              AND COALESCE(d.title, '') NOT ILIKE '%novedad%'
              AND COALESCE(d.title, '') NOT ILIKE '%prensa%'
              AND COALESCE(d.title, '') NOT ILIKE '%comunicado%'
              AND COALESCE(d.title, '') NOT ILIKE '%agenda%'
            ORDER BY ts_rank(
                to_tsvector('spanish', COALESCE(d.title, '') || ' ' || c.text),
                websearch_to_tsquery('spanish', :q)
            ) DESC
            LIMIT :k
            """
        )
        url_hint_stmt = text(
            """
            SELECT d.canonical_url AS url, COALESCE(d.title, '') AS title, c.text AS chunk_text
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            JOIN sources s ON s.source_id = d.source_id
            WHERE (
                d.canonical_url ILIKE '%/carreras/%'
                OR d.canonical_url ILIKE '%/oferta-academica/%'
                OR d.canonical_url ILIKE '%/ofertas-academicas/%'
                OR d.canonical_url ILIKE '%/programas/%'
                OR d.canonical_url ILIKE '%/inscripcion/%'
                OR d.canonical_url ILIKE '%/admision/%'
                OR d.canonical_url ILIKE '%/requisitos/%'
                OR d.canonical_url ILIKE '%/tramites/%'
                OR d.canonical_url ILIKE '%/ingenieria-civil/%'
                OR d.canonical_url ILIKE '%ingenieria%civil%'
                OR d.title ILIKE '%carrera%'
                OR d.title ILIKE '%programa%'
                OR d.title ILIKE '%requisito%'
            )
              AND (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
              AND COALESCE(d.page_type, 'academic') IN ('academic', 'evergreen')
              AND d.canonical_url NOT ILIKE '%/noticia/%'
              AND d.canonical_url NOT ILIKE '%/noticias/%'
              AND d.canonical_url NOT ILIKE '%/novedad/%'
              AND d.canonical_url NOT ILIKE '%/novedades/%'
              AND d.canonical_url NOT ILIKE '%/actualidad/%'
              AND d.canonical_url NOT ILIKE '%/prensa/%'
              AND d.canonical_url NOT ILIKE '%/comunicado/%'
              AND d.canonical_url NOT ILIKE '%/evento/%'
              AND d.canonical_url NOT ILIKE '%/agenda/%'
              AND COALESCE(d.title, '') NOT ILIKE '%noticia%'
              AND COALESCE(d.title, '') NOT ILIKE '%novedad%'
              AND COALESCE(d.title, '') NOT ILIKE '%prensa%'
              AND COALESCE(d.title, '') NOT ILIKE '%comunicado%'
              AND COALESCE(d.title, '') NOT ILIKE '%agenda%'
            ORDER BY d.fetched_at DESC
            LIMIT :k
            """
        )

        contexts: list[str] = []
        seen: set[tuple[str, str]] = set()

        async with async_session() as session:
            vector_rows = (
                await session.execute(
                    vector_stmt,
                    {
                        "query_embedding": vector_literal,
                        "k": 20,
                        "domain_1": domain_1,
                        "domain_2": domain_2,
                    },
                )
            ).mappings().all()
            lexical_rows = []
            for lq in lexical_queries:
                rows = (
                    await session.execute(
                        lexical_stmt,
                        {
                            "q": lq,
                            "k": 20,
                            "domain_1": domain_1,
                            "domain_2": domain_2,
                        },
                    )
                ).mappings().all()
                lexical_rows.extend(rows)
            hinted_rows = []
            if self._needs_url_hints(query):
                hinted_rows = (
                    await session.execute(
                        url_hint_stmt,
                        {"k": 60, "domain_1": domain_1, "domain_2": domain_2},
                    )
                ).mappings().all()

        for row in [*lexical_rows, *hinted_rows, *vector_rows]:
            url = (row.get("url") or "").strip()
            title = (row.get("title") or "").strip()
            chunk_text = (row.get("chunk_text") or "").strip()
            if not url or not chunk_text:
                continue
            key = (url, chunk_text[:180])
            if key in seen:
                continue
            seen.add(key)
            contexts.append(f"URL: {url}\nTitulo: {title}\nContenido: {chunk_text}")
            if len(contexts) >= 10:
                break

        contexts = self._rank_context_blocks(contexts, query)[:10]

        if source_url and (not contexts or self._needs_source_fallback(contexts, query)):
            fallback_contexts = await self._retrieve_from_source(source_url, query)
            if fallback_contexts:
                merged: list[str] = []
                seen_urls: set[str] = set()
                for block in [*contexts, *fallback_contexts]:
                    first_line = block.splitlines()[0] if block else ""
                    url = first_line.replace("URL:", "").strip() if first_line.startswith("URL:") else ""
                    if url and url in seen_urls:
                        continue
                    if url:
                        seen_urls.add(url)
                    merged.append(block)
                merged = self._rank_context_blocks(merged, query)
                return {"context": merged[:10]}

        return {"context": contexts}

    async def generate(self, state: AgentState):
        context_blocks = state.get("context") or []
        history_blocks = state.get("history") or []
        if not context_blocks:
            return {
                "response": "No tengo evidencia suficiente en la base para responder con precisión. Fuente: sin coincidencias recuperadas."
            }
        extracted = self._extract_answer_from_context(state.get("query", ""), context_blocks)
        if extracted:
            return {"response": extracted}

        context = "\n\n---\n\n".join(context_blocks)
        history = "\n".join(history_blocks[-8:]).strip()
        prompt = (
            f"{SYSTEM_RAG}\n\n"
            f"Historial reciente:\n{history}\n\n"
            f"Contexto recuperado:\n{context}\n\n"
            f"Pregunta del usuario:\n{state['query']}"
        )
        res = await self.llm.ainvoke(prompt)
        return {"response": (res.content or "").strip()}
