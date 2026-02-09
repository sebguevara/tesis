import asyncio
import re
import unicodedata
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
                "ofrecen",
                "ofrece",
                "estudiar",
                "estudia",
                "estudio",
                "opciones",
                "grado",
                "pregrado",
                "posgrado",
                "formacion",
                "formación",
            )
        ):
            expanded.append(
                "oferta académica carreras programas cursos tecnicaturas licenciaturas"
            )
            expanded.append(
                "que puedo estudiar opciones de estudio oferta educativa carreras disponibles"
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
                "ofrecen",
                "ofrece",
                "estudiar",
                "estudio",
                "opciones",
                "oferta educativa",
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
    def _clean_program_name(raw: str) -> str:
        value = (raw or "").strip()
        if not value:
            return ""
        value = re.sub(r"\s+", " ", value.replace("-", " ")).strip()
        words = []
        for w in value.split(" "):
            lw = w.lower()
            if lw in {"de", "del", "la", "las", "los", "y", "en"}:
                words.append(lw)
            else:
                words.append(w.capitalize())
        return " ".join(words).strip()

    @staticmethod
    def _extract_url_from_block(block: str) -> str:
        for line in (block or "").splitlines():
            if line.startswith("URL:"):
                return line.replace("URL:", "").strip()
        return ""

    @staticmethod
    def _extract_title_from_block(block: str) -> str:
        for line in (block or "").splitlines():
            if line.startswith("Titulo:"):
                return line.replace("Titulo:", "").strip()
        return ""

    @staticmethod
    def _is_program_noise(text: str) -> bool:
        t = (text or "").lower()
        return any(
            token in t
            for token in (
                "concurso",
                "ayudante",
                "examen",
                "turno",
                "calendario",
                "cronograma",
                "inscripcion",
                "inscripción",
                "mesa",
            )
        )

    @staticmethod
    def _extract_program_names_from_context(context_blocks: list[str]) -> tuple[list[str], list[str]]:
        names: list[str] = []
        source_urls: list[str] = []
        seen_names: set[str] = set()
        seen_urls: set[str] = set()

        name_pattern = re.compile(
            r"\b("
            r"medicina|"
            r"licenciatura\s+en\s+[a-záéíóúñü\s]+|"
            r"tecnicatura\s+en\s+[a-záéíóúñü\s]+|"
            r"doctorado\s+en\s+[a-záéíóúñü\s]+|"
            r"especializaci[oó]n\s+en\s+[a-záéíóúñü\s]+"
            r")\b",
            flags=re.IGNORECASE,
        )

        for block in context_blocks:
            url = RAGService._extract_url_from_block(block)
            title = RAGService._extract_title_from_block(block)
            low_url = (url or "").lower()
            low_title = (title or "").lower()

            if not url:
                continue
            if RAGService._is_program_noise(f"{low_url} {low_title}"):
                continue

            if "/carreras/" in low_url:
                after = low_url.split("/carreras/", 1)[1].strip("/")
                slug = after.split("/", 1)[0].strip()
                if slug and slug not in {"carreras", "category", "tag"}:
                    candidate = RAGService._clean_program_name(slug)
                    if candidate:
                        key = candidate.lower()
                        if key not in seen_names:
                            seen_names.add(key)
                            names.append(candidate)
                            if url not in seen_urls:
                                seen_urls.add(url)
                                source_urls.append(url)

            for raw in name_pattern.findall(title):
                candidate = RAGService._clean_program_name(raw)
                if not candidate:
                    continue
                key = candidate.lower()
                if key not in seen_names:
                    seen_names.add(key)
                    names.append(candidate)
                    if url not in seen_urls:
                        seen_urls.add(url)
                        source_urls.append(url)

        return names, source_urls

    @staticmethod
    def _extract_program_mentions_from_text(text: str) -> list[str]:
        value = (text or "").strip()
        if not value:
            return []
        pattern = re.compile(
            r"\b("
            r"medicina|"
            r"licenciatura\s+en\s+[a-záéíóúñü\s]+|"
            r"tecnicatura\s+en\s+[a-záéíóúñü\s]+|"
            r"doctorado\s+en\s+[a-záéíóúñü\s]+|"
            r"especializaci[oó]n\s+en\s+[a-záéíóúñü\s]+"
            r")\b",
            flags=re.IGNORECASE,
        )
        found: list[str] = []
        seen: set[str] = set()
        for raw in pattern.findall(value):
            candidate = RAGService._clean_program_name(raw)
            if not candidate:
                continue
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(candidate)
        return found

    @staticmethod
    def _is_authority_query(query: str) -> bool:
        q = (query or "").lower()
        return any(t in q for t in ("director", "coordinador", "responsable", "autoridad", "dirección"))

    @staticmethod
    def _looks_like_program_reply(query: str) -> bool:
        q = (query or "").strip().lower()
        if not q:
            return False
        if not RAGService._query_has_specific_program(q):
            return False
        if RAGService._is_authority_query(q):
            return False
        soft_prefixes = (
            "me refiero",
            "de ",
            "de la ",
            "es ",
            "la carrera es",
            "hablo de",
        )
        token_count = len(re.findall(r"[a-záéíóúñü0-9]+", q))
        return token_count <= 8 or any(q.startswith(p) for p in soft_prefixes)

    @staticmethod
    def _infer_authority_query_from_history(history: list[str]) -> str | None:
        for row in reversed(history or []):
            if not row.upper().startswith("USER:"):
                continue
            text = row.split(":", 1)[1].strip() if ":" in row else ""
            if RAGService._is_authority_query(text):
                return text
        return None

    @staticmethod
    def _slugify_program_name(program_name: str) -> str:
        norm = unicodedata.normalize("NFKD", (program_name or "").strip().lower())
        norm = "".join(ch for ch in norm if not unicodedata.combining(ch))
        norm = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
        return norm

    @staticmethod
    def _excerpt_around_keyword(text: str, keywords: tuple[str, ...], radius: int = 700) -> str:
        lowered = (text or "").lower()
        pos = -1
        for kw in keywords:
            idx = lowered.find(kw)
            if idx >= 0:
                pos = idx
                break
        if pos < 0:
            return (text or "")[:1400]
        start = max(0, pos - radius)
        end = min(len(text), pos + radius)
        return (text or "")[start:end]

    async def _retrieve_authority_context_from_program(self, source_url: str, program_name: str) -> list[str]:
        parsed = urlparse(source_url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            return []
        base = f"https://{parsed.netloc}/"
        slug = self._slugify_program_name(program_name)
        if not slug:
            return []
        candidates = [
            urljoin(base, f"carreras/{slug}/"),
            urljoin(base, f"carreras/{slug}"),
            urljoin(base, f"ofertas-acad/{slug}"),
            urljoin(base, f"oferta-academica/{slug}"),
        ]
        out: list[str] = []
        seen: set[str] = set()
        authority_terms = (
            "director de carrera",
            "directora de carrera",
            "dirección de carrera",
            "direccion de carrera",
            "coordinador",
            "coordinadora",
            "responsable de carrera",
        )
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                title, content = await self.scraper.scrape_page(candidate)
            except Exception:  # noqa: BLE001
                continue
            content = (content or "").strip()
            if not content:
                continue
            haystack = f"{(title or '').lower()} {content.lower()}"
            if not any(term in haystack for term in authority_terms):
                continue
            excerpt = self._excerpt_around_keyword(content, authority_terms)
            out.append(f"URL: {candidate}\nTitulo: {(title or '').strip()}\nContenido: {excerpt}")
            if len(out) >= 2:
                break
        return out

    @staticmethod
    def _query_has_specific_program(query: str) -> bool:
        return len(RAGService._extract_program_mentions_from_text(query)) > 0

    @staticmethod
    def _infer_program_from_history(history: list[str], current_query: str) -> str | None:
        q_norm = re.sub(r"\s+", " ", (current_query or "").strip().lower())
        for row in reversed(history or []):
            if not row.upper().startswith("USER:"):
                continue
            text = row.split(":", 1)[1].strip() if ":" in row else ""
            t_norm = re.sub(r"\s+", " ", text.lower())
            if t_norm == q_norm:
                continue
            mentions = RAGService._extract_program_mentions_from_text(text)
            if mentions:
                return mentions[0]
        return None

    @staticmethod
    def _needs_program_clarification(query: str) -> bool:
        q = (query or "").lower()
        asks_authority = RAGService._is_authority_query(q)
        mentions_program_scope = any(
            t in q for t in ("carrera", "programa", "oferta")
        )
        return asks_authority and mentions_program_scope and not RAGService._query_has_specific_program(query)

    @staticmethod
    def _build_retry_queries(query: str, history: list[str]) -> list[str]:
        base = (query or "").strip()
        if not base:
            return []
        retries: list[str] = []
        if RAGService._is_programs_query(base):
            retries.extend(
                [
                    f"{base} oferta académica carreras programas",
                    f"{base} listado de carreras disponibles",
                    "carreras oferta académica programas de estudio",
                ]
            )
        if RAGService._is_authority_query(base):
            inferred = RAGService._infer_program_from_history(history, base)
            if inferred:
                retries.extend(
                    [
                        f"director de carrera {inferred}",
                        f"coordinador de carrera {inferred}",
                        f"dirección de carrera {inferred}",
                    ]
                )
        unique: list[str] = []
        seen: set[str] = set()
        for item in retries:
            key = re.sub(r"\s+", " ", item.lower()).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique[:3]

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
            "puedo",
            "tienen",
            "ofrece",
            "ofrecen",
            "aca",
            "aqui",
            "aquí",
        }
        return [t for t in tokens if t not in stopwords]

    @staticmethod
    def _is_valid_https_source(source_url: str | None) -> bool:
        if not source_url:
            return False
        parsed = urlparse(source_url)
        return parsed.scheme.lower() == "https" and bool(parsed.netloc)

    @staticmethod
    def _is_programs_query(query: str) -> bool:
        q = (query or "").lower()
        compact_q = re.sub(r"\s+", " ", q).strip()
        return any(
            token in q
            for token in (
                "carrera",
                "carreras",
                "oferta",
                "ofertas",
                "programa",
                "programas",
                "tecnicatura",
                "tecnicaturas",
                "licenciatura",
                "licenciaturas",
                "grado",
                "pregrado",
                "posgrado",
                "oferta educativa",
                "oferta académica",
                "oferta academica",
                "puedo estudiar",
                "se puede estudiar",
                "que estudiar",
                "qué estudiar",
                "que se estudia",
                "qué se estudia",
                "opciones de estudio",
                "que ofrecen",
                "qué ofrecen",
                "que ofrece",
                "qué ofrece",
            )
        ) or bool(
            re.search(
                r"\b(que|qué)\b.*\b(hay|ofrecen|ofrece|estudia|estudiar)\b",
                compact_q,
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _contexts_look_like_program_catalog(contexts: list[str]) -> bool:
        if not contexts:
            return False
        joined = "\n".join(contexts).lower()
        url_hits = 0
        for block in contexts:
            head = (block.splitlines()[0] if block else "").lower()
            if any(
                token in head
                for token in (
                    "/carreras",
                    "/oferta-academica",
                    "/ofertas-academicas",
                    "/programas",
                    "/carrera-",
                )
            ):
                url_hits += 1
        keyword_hits = sum(
            1
            for kw in ("oferta académica", "oferta academica", "carreras", "programas")
            if kw in joined
        )
        return url_hits >= 1 or keyword_hits >= 2

    @staticmethod
    def _needs_source_fallback(contexts: list[str], query: str) -> bool:
        if len(contexts) < 2:
            return True
        q = (query or "").lower()
        joined = "\n".join(contexts).lower()
        if RAGService._is_programs_query(q) and not RAGService._contexts_look_like_program_catalog(contexts):
            return True
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

    @staticmethod
    def _seed_candidate_urls(source_url: str, query: str) -> list[str]:
        parsed = urlparse(source_url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            return [source_url]
        root = f"https://{parsed.netloc}/"
        seeds = [source_url, root]
        if RAGService._is_programs_query(query):
            seeds.extend(
                [
                    urljoin(root, "carreras"),
                    urljoin(root, "carreras/"),
                    urljoin(root, "oferta-academica"),
                    urljoin(root, "oferta-academica/"),
                    urljoin(root, "ofertas-academicas"),
                    urljoin(root, "ofertas-academicas/"),
                    urljoin(root, "programas"),
                    urljoin(root, "programas/"),
                    urljoin(root, "academica"),
                    urljoin(root, "academica/"),
                ]
            )
        unique: list[str] = []
        seen: set[str] = set()
        for item in seeds:
            if not item or item in seen:
                continue
            seen.add(item)
            unique.append(item)
        return unique

    async def _discover_candidate_urls(self, source_url: str, query: str, limit: int = 5) -> list[str]:
        parsed_source = urlparse(source_url)
        base_host = parsed_source.netloc.lower()
        tokens = self._extract_query_tokens(query)
        seeded_urls = self._seed_candidate_urls(source_url, query)

        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(seeded_urls[0])
                if resp.status_code >= 400:
                    return seeded_urls[: max(1, limit)]
                soup = BeautifulSoup(resp.text, "html.parser")
        except Exception:  # noqa: BLE001
            return seeded_urls[: max(1, limit)]

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
        unique: list[str] = []
        seen: set[str] = set()
        for seeded in seeded_urls:
            if seeded in seen:
                continue
            seen.add(seeded)
            unique.append(seeded)
            if len(unique) >= max(1, limit):
                return unique
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
        programs_query = RAGService._is_programs_query(query)
        scored: list[tuple[int, str]] = []
        for block in contexts:
            haystack = (block or "").lower()
            score = sum(2 for tok in tokens if tok in haystack)
            if programs_query:
                if any(
                    token in haystack
                    for token in ("/carreras/", "/oferta-academica", "/programas/", "oferta académica")
                ):
                    score += 8
                if RAGService._is_program_noise(haystack):
                    score -= 10
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

        if RAGService._is_programs_query(q):
            names, urls = RAGService._extract_program_names_from_context(context_blocks)
            if len(names) >= 2:
                lines = [f"{idx}. {name}" for idx, name in enumerate(sorted(names), 1)]
                srcs = "\n".join(f"- {u}" for u in urls[:3])
                return (
                    f"Se identifican {len(names)} carreras/programas en el sitio:\n"
                    + "\n".join(lines)
                    + "\n\nFuente:\n"
                    + srcs
                )

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
                        src = RAGService._extract_url_from_block(block)
                        return f"La duración es {value}. Fuente: {src or 'contexto recuperado'}"

        if RAGService._is_authority_query(q):
            for block in context_blocks:
                direct_patterns = (
                    r"(?:director(?:a)?\s+de\s+carrera|direcci[oó]n\s+de\s+carrera|coordinador(?:a)?(?:\s+de\s+carrera)?|responsable\s+de\s+carrera)\s*[:\-]\s*([^\n|]+)",
                    r"\|\s*(?:director(?:a)?\s+de\s+carrera|direcci[oó]n\s+de\s+carrera|coordinador(?:a)?|responsable)\s*\|\s*([^\|\n]+)\|",
                    r"(?:^|\n)\s*#{1,4}\s*(?:director(?:a)?|coordinador(?:a)?|direcci[oó]n)\s*(?:de\s+carrera)?\s*\n+\s*([^\n]+)",
                )
                value = ""
                for pattern in direct_patterns:
                    match = re.search(pattern, block, flags=re.IGNORECASE)
                    if match:
                        value = (match.group(1) or "").strip(" .:-")
                        if value:
                            break
                if value:
                    src = RAGService._extract_url_from_block(block)
                    return (
                        f"La autoridad de carrera indicada en el sitio es {value}. "
                        f"Fuente: {src or 'contexto recuperado'}"
                    )
                match = re.search(
                    r"director\s+de\s+carrera[^\n]*\n+\s*#{1,4}\s*([^\n]+)",
                    block,
                    flags=re.IGNORECASE,
                )
                if match:
                    value = match.group(1).strip()
                    if value:
                        src = RAGService._extract_url_from_block(block)
                        return (
                            f"El director de carrera indicado en el sitio es {value}. "
                            f"Fuente: {src or 'contexto recuperado'}"
                        )
        return None

    async def retrieve(self, state: AgentState):
        query = (state.get("query") or "").strip()
        query = self._normalize_query_typos(query)
        history = state.get("history") or []
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

        resolved_query = query
        if self._looks_like_program_reply(query):
            prior_authority_query = self._infer_authority_query_from_history(history)
            current_programs = self._extract_program_mentions_from_text(query)
            if prior_authority_query and current_programs:
                resolved_query = f"{prior_authority_query} {current_programs[0]}"
        if self._needs_program_clarification(query):
            inferred_program = self._infer_program_from_history(history, query)
            if inferred_program:
                resolved_query = f"{query} {inferred_program}"

        query_vector = await asyncio.to_thread(self.embeddings.embed_query, resolved_query)
        vector_literal = "[" + ",".join(f"{value:.8f}" for value in query_vector) + "]"
        lexical_queries = self._expand_lexical_queries(resolved_query)

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
            if self._needs_url_hints(resolved_query):
                hinted_rows = (
                    await session.execute(
                        url_hint_stmt,
                        {"k": 60, "domain_1": domain_1, "domain_2": domain_2},
                    )
                ).mappings().all()

            retry_rows = []
            if not lexical_rows and not hinted_rows:
                for rq in self._build_retry_queries(resolved_query, history):
                    rows = (
                        await session.execute(
                            lexical_stmt,
                            {
                                "q": rq,
                                "k": 20,
                                "domain_1": domain_1,
                                "domain_2": domain_2,
                            },
                        )
                    ).mappings().all()
                    retry_rows.extend(rows)

        for row in [*lexical_rows, *hinted_rows, *vector_rows, *retry_rows]:
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

        contexts = self._rank_context_blocks(contexts, resolved_query)[:10]

        if self._is_authority_query(resolved_query):
            programs = self._extract_program_mentions_from_text(resolved_query)
            if programs:
                has_authority_signal = any(
                    any(
                        token in (block or "").lower()
                        for token in (
                            "director de carrera",
                            "directora de carrera",
                            "dirección de carrera",
                            "direccion de carrera",
                            "coordinador",
                            "coordinadora",
                            "responsable de carrera",
                        )
                    )
                    for block in contexts
                )
                if not has_authority_signal:
                    authority_contexts = await self._retrieve_authority_context_from_program(
                        source_url, programs[0]
                    )
                    if authority_contexts:
                        contexts = self._rank_context_blocks(
                            [*authority_contexts, *contexts], resolved_query
                        )[:10]

        if source_url and (not contexts or self._needs_source_fallback(contexts, resolved_query)):
            fallback_contexts = await self._retrieve_from_source(source_url, resolved_query)
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
                merged = self._rank_context_blocks(merged, resolved_query)
                return {"context": merged[:10]}

        return {"context": contexts}

    async def generate(self, state: AgentState):
        context_blocks = state.get("context") or []
        history_blocks = state.get("history") or []
        query = (state.get("query") or "").strip()
        if self._needs_program_clarification(query):
            inferred_program = self._infer_program_from_history(history_blocks, query)
            if not inferred_program:
                return {
                    "response": "¿A qué carrera te referís? Si me decís el nombre exacto, te doy el dato puntual."
                }
        if not context_blocks:
            return {
                "response": "No encontré evidencia suficiente con esa formulación. ¿Querés que lo busque por carrera específica, sede o nivel (grado/posgrado)?"
            }
        extracted = self._extract_answer_from_context(query, context_blocks)
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
