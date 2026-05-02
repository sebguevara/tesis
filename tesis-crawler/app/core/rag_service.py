import re
import unicodedata
from typing import Any, Dict, List, TypedDict
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from uuid import UUID

import httpx
from bs4 import BeautifulSoup
from langgraph.graph import END, StateGraph
from langchain_openai import ChatOpenAI
from sqlalchemy import text

from app.config import settings
from app.core.content_filters import is_institutional_news, is_non_academic_noise
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
    session_state: Dict[str, Any] | None


class RAGService:
    NO_INFO_RESPONSE = "No tengo información referente a eso."
    ENABLE_RUNTIME_SCRAPE = bool(settings.RAG_ENABLE_LIVE_FETCH)
    SIMPLE_RETRIEVAL_MODE = bool(getattr(settings, "RAG_SIMPLE_MODE", False))
    USE_PROGRAM_FACTS = True
    VECTOR_K = 42
    LEXICAL_K = 42
    URL_HINT_K = 90
    AUTHORITY_K = 120
    NON_AUTH_CONTEXTS = 16
    AUTHORITY_CONTEXTS = 24
    FALLBACK_DISCOVERY_LIMIT = 20
    FALLBACK_CONTEXT_LIMIT = 12
    MAX_CONTEXT_CHARS_PER_BLOCK = 2200
    MAX_CONTEXT_CHARS_TOTAL = 24000
    MAX_HISTORY_ITEMS = 12
    MAX_HISTORY_CHARS_PER_ITEM = 500
    MAX_HISTORY_CHARS_TOTAL = 5000
    MAX_PROMPT_CHARS = 22000
    MIN_CONFIDENT_PROGRAM = 0.72
    PROFILE_INTENT_KEYS: dict[str, tuple[str, ...]] = {
        "ingresantes": ("ingresante", "ingresantes", "ingreso", "admis", "inscrip"),
        "estudiantes": ("estudiante", "estudiantes", "alumno", "alumnos"),
        "docentes": ("docente", "docentes", "profesor", "profesores", "cátedra", "catedra"),
        "nodocentes": ("nodocente", "nodocentes", "no docente", "no docentes"),
        "directivos": ("directivo", "directivos", "autoridades", "gestion", "gestión"),
    }

    def __init__(self):
        self.llm = ChatOpenAI(
            model=settings.OPENAI_CHAT_MODEL,
            api_key=settings.OPENAI_API_KEY,
            timeout=float(getattr(settings, "RAG_LLM_TIMEOUT_SECONDS", 18)),
            max_retries=int(getattr(settings, "RAG_LLM_MAX_RETRIES", 1)),
        )
        self.scraper = ScrapingService()

    @staticmethod
    async def _resolve_embeddings_relation() -> tuple[str, str, str] | None:
        """
        Resolve embeddings relation and columns across environments:
        - chunks(text, chunk_id)
        - documents_embeddings(chunk, chunk_seq)
        - documents_embedding(chunk, chunk_seq)
        - documents_embedding_store(chunk, chunk_seq)
        - ai.vectorizer_status target/view relations
        """
        async with async_session() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT
                          to_regclass('public.chunks')::text AS chunks_table,
                          to_regclass('public.documents_embeddings')::text AS documents_embeddings_table,
                          to_regclass('public.documents_embedding')::text AS documents_embedding_table,
                          to_regclass('public.documents_embedding_store')::text AS documents_embedding_store_table,
                          COALESCE(
                            (SELECT to_regclass(target_table)::text FROM ai.vectorizer_status ORDER BY id DESC LIMIT 1),
                            ''
                          ) AS vectorizer_target_table,
                          COALESCE(
                            (SELECT to_regclass(view)::text FROM ai.vectorizer_status ORDER BY id DESC LIMIT 1),
                            ''
                          ) AS vectorizer_view_table
                        """
                    )
                )
            ).mappings().first()
            if not row:
                return None

            candidates: list[tuple[str, str, str]] = []
            if row.get("chunks_table"):
                candidates.append(("chunks", "text", "chunk_id"))
            if row.get("documents_embeddings_table"):
                candidates.append(("documents_embeddings", "chunk", "chunk_seq"))
            if row.get("documents_embedding_table"):
                candidates.append(("documents_embedding", "chunk", "chunk_seq"))
            if row.get("documents_embedding_store_table"):
                candidates.append(("documents_embedding_store", "chunk", "chunk_seq"))

            for dynamic_relation in (
                str(row.get("vectorizer_view_table") or "").strip(),
                str(row.get("vectorizer_target_table") or "").strip(),
            ):
                if not dynamic_relation:
                    continue
                relation_name = dynamic_relation.split(".", 1)[-1]
                candidates.append((relation_name, "chunk", "chunk_seq"))

            checked: set[str] = set()
            for relation_name, default_text_col, default_seq_col in candidates:
                if relation_name in checked:
                    continue
                checked.add(relation_name)
                cols = (
                    await session.execute(
                        text(
                            """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema = 'public' AND table_name = :table_name
                            ORDER BY ordinal_position
                            """
                        ),
                        {"table_name": relation_name},
                    )
                ).scalars().all()
                colset = {str(c) for c in cols}
                if not colset:
                    continue
                text_col = (
                    default_text_col
                    if default_text_col in colset
                    else ("chunk" if "chunk" in colset else ("text" if "text" in colset else "content"))
                )
                if text_col not in colset:
                    continue
                if default_seq_col in colset:
                    seq_col = default_seq_col
                elif "chunk_seq" in colset:
                    seq_col = "chunk_seq"
                elif "chunk_id" in colset:
                    seq_col = "chunk_id"
                elif "id" in colset:
                    seq_col = "id"
                else:
                    seq_col = text_col
                return (relation_name, text_col, seq_col)

        return None

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
            # Common program abbreviations that users type
            (r"\bkinesio\b", "kinesiologia"),
            (r"\bkine\b", "kinesiologia"),
            (r"\bkines\b", "kinesiologia"),
            (r"\benfer\b", "enfermeria"),
            (r"\benfermera\b", "enfermeria"),
            (r"\benfermero\b", "enfermeria"),
            (r"\bmedici\b", "medicina"),
            # Typos
            (r"\bcarrea\b", "carrera"),
            (r"\bingrsantes\b", "ingresantes"),
            (r"\bingrersantes\b", "ingresantes"),
            (r"\badminiones\b", "admisiones"),
            (r"\badminicion(?:es)?\b", "admisiones"),
            (r"\badmicion(?:es)?\b", "admisiones"),
            (r"\badmiciones\b", "admisiones"),
            (r"\bduraccion\b", "duracion"),
            (r"\bingenieriaa\b", "ingenieria"),
            (r"\binscirbirme\b", "inscribirme"),
            (r"\binscribirne\b", "inscribirme"),
            (r"\binscirpcion\b", "inscripcion"),
            (r"\bpapeeles\b", "papeles"),
            (r"\bbilbioteca\b", "biblioteca"),
        )
        for pattern, repl in replacements:
            q = re.sub(pattern, repl, q, flags=re.IGNORECASE)
        return q

    @staticmethod
    def _normalize_query_for_intent(query: str) -> str:
        q = RAGService._normalize_query_typos(query)
        q = re.sub(r"\s+", " ", (q or "").strip())
        low = q.lower()

        if re.search(r"\binscrib\w*\s+a\s+la\s+(?:carrera|facultad)\b", low):
            q = f"{q} admision inscripcion ingreso"
        if any(token in low for token in ("anotarme", "matricularme", "matricula", "matriculacion", "matriculación")):
            q = f"{q} inscripcion ingreso admision"
        return q

    @staticmethod
    def _limit_lexical_queries(queries: list[str], query: str) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for item in queries:
            key = re.sub(r"\s+", " ", (item or "").strip().lower())
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(item)

        q = (query or "").lower()
        limit = 4
        if RAGService._is_admissions_query(q) or RAGService._is_tramites_query(q):
            limit = 3
        if RAGService._is_authority_query(q):
            limit = 5
        return unique[:limit]

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
        if any(
            token in q
            for token in (
                "materia",
                "materias",
                "plan de estudios",
                "primer año",
                "primer anio",
                "1er año",
                "1er anio",
            )
        ):
            expanded.append("plan de estudios materias primer año primer anio")
            expanded.append("asignaturas primer año lic en")
            expanded.append("materias segundo año tercer año cuarto año quinto año sexto año")
        if any(token in q for token in ("ingresantes", "ingreso", "admis", "inscrip")):
            expanded.append("ingresantes admisión inscripción requisitos documentación")
        if any(token in q for token in ("estudiantes", "docentes", "nodocentes", "directivos")):
            expanded.append("estudiantes docentes nodocentes directivos autoridades trámites")
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
        value = re.sub(
            r"^\s*lic\s*\.?\s*en\s+",
            "licenciatura en ",
            value,
            flags=re.IGNORECASE,
        )
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
    def _normalize_name_key(value: str) -> str:
        norm = unicodedata.normalize("NFKD", (value or "").strip().lower())
        norm = "".join(ch for ch in norm if not unicodedata.combining(ch))
        norm = re.sub(r"[^a-z0-9]+", " ", norm).strip()
        return norm

    @staticmethod
    def _sanitize_career_name(raw: str) -> str:
        value = re.sub(r"\s+", " ", (raw or "").strip())
        if not value:
            return ""
        # Remove long noisy suffixes frequently extracted from program sheets.
        value = re.sub(
            r"\s+(?:de esta facultad|de esta unidad academica|de esta casa)\b.*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        # Strip "de la Facultad / de la Universidad / de la <sigla>" suffixes.
        value = re.sub(
            r"\s+de\s+(?:la|el|los|las)\s+(?:facultad|universidad|escuela|instituto)\b.*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        value = re.sub(
            r"\s+de\s+la\s+(?:unne|unc|uba|utn|unl|unp|unse|unlp|unr|untref|unsam|uner|unco|unsa|unrc|unsj|unsl|unvm|unpa|unpsjb|uncuyo|unas|un[a-z]{2,4})\b.*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        # Strip conjugated-verb noise: "logró", "viene", "realizó", etc.
        value = re.sub(
            r"\s+(?:logr[oó]|viene|vino|abord[oó]|realiz[oó]|inici[oó]|comenz[oó]|alcanz[oó]|obtuvo|recibi[oó]|celebr[oó]|present[oó])\b.*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        # Strip "con la sociedad / con el / junto a / listado de / noticias"
        value = re.sub(
            r"\s+(?:con\s+la\s+sociedad|junto\s+a|listado\s+de|noticias|novedades|acreditaci[oó]n|acredit[oó])\b.*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        value = re.sub(
            r"\s+(?:programa|programa analitico|programa de examen|patologia|introduccion|resolucion)\b.*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        value = re.sub(
            r"\s+aprobad[oa]\s+por\s+resol.*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        value = re.sub(
            r"\s+para\s+realizar\s+pasant[ií]as?.*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        value = re.sub(
            r"\s+o\s+pr[aá]ctica\s+pre\s+profesional.*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        value = re.sub(
            r"\s+en\s+un\s+[aá]mbito.*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        value = re.sub(r"\s+", " ", value).strip(" .,:;-/")
        # Strip trailing stopwords left by truncated regex matches (e.g. "… de la").
        value = re.sub(r"(?:\s+(?:de|del|la|el|los|las|en|y|a|al|con))+$", "", value, flags=re.IGNORECASE).strip(" .,:;-/")
        return RAGService._clean_program_name(value)

    @staticmethod
    def _is_plausible_career_name(name: str) -> bool:
        value = re.sub(r"\s+", " ", (name or "").strip())
        if not value:
            return False
        if len(value) < 4 or len(value) > 60:
            return False
        # A career name realistically has at most 7 words.
        if len(value.split()) > 7:
            return False
        low = RAGService._normalize_name_key(value)
        if not low:
            return False
        blocked = (
            "programa",
            "analitico",
            "examen",
            "cohorte",
            "posgrado",
            "curso",
            "seminario",
            "departamento",
            "decano",
            "tramite",
            "solicitud",
            "resolucion",
            "aprobada por resol",
            "expedicion",
            "calendario",
            "materia",
            "pasantia",
            "practica pre profesional",
            "acreditacion",
            "logro",
            "listado",
            "noticias",
            "novedades",
            "articulos",
        )
        return not any(tok in low for tok in blocked)

    @staticmethod
    def _program_dedupe_key(value: str) -> str:
        """
        Coarser dedupe key for program listing:
        - collapses degree prefixes ("Licenciatura en", "Carrera de")
        - removes glue words ("de", "la", etc.)
        """
        key = RAGService._normalize_name_key(value)
        if not key:
            return ""
        key = re.sub(r"^(licenciatura|tecnicatura|doctorado|especializacion)\s+en\s+", "", key).strip()
        key = re.sub(r"^carrera\s+de\s+", "", key).strip()
        tokens = [t for t in key.split(" ") if t and t not in {"de", "del", "la", "las", "los", "y", "en"}]
        return " ".join(tokens).strip()

    @staticmethod
    def _career_name_from_url(url: str) -> str:
        low = (url or "").lower()
        # Pattern 1: /carreras/<slug>/...
        if "/carreras/" in low:
            tail = low.split("/carreras/", 1)[1].strip("/")
            if tail:
                slug = tail.split("/", 1)[0].strip("- ")
                if slug and slug not in {"carreras", "category", "tag", "page"}:
                    return RAGService._clean_program_name(slug)
        # Pattern 2: /carrera-de-<slug>/
        m = re.search(r"/carrera-de-([a-z0-9áéíóúñü][a-z0-9áéíóúñü\-]+?)(?:/|$)", low)
        if m:
            slug = m.group(1).strip("- ")
            if slug:
                return RAGService._clean_program_name(slug)
        return ""

    @staticmethod
    def _wants_only_careers(query: str) -> bool:
        q = (query or "").lower()
        has_career = any(t in q for t in ("carrera", "carreras"))
        has_program = any(t in q for t in ("programa", "programas", "curso", "cursos", "oferta"))
        return has_career and not has_program

    @staticmethod
    def _extract_url_from_block(block: str) -> str:
        for line in (block or "").splitlines():
            if line.startswith("URL:"):
                return line.replace("URL:", "").strip()
        return ""

    @staticmethod
    def _extract_fetched_at_from_block(block: str) -> datetime | None:
        for line in (block or "").splitlines():
            if line.startswith("FetchedAt:"):
                raw = line.replace("FetchedAt:", "").strip()
                if not raw:
                    return None
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    return None
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt
        return None

    @staticmethod
    def _is_event_like_old_block(block: str) -> bool:
        low = (block or "").lower()
        if not any(t in low for t in ("inicio", "finalización", "finalizacion", "inscripción", "inscripcion")):
            return False
        years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", low)]
        if not years:
            return False
        return max(years) < (datetime.now().year - 1)

    @staticmethod
    def _authority_url_score(url: str) -> int:
        low = (url or "").lower()
        score = 0
        if "/carreras/" in low:
            score += 35
        elif "/carreras" in low:
            score += 22
        if any(tok in low for tok in ("/oferta-academica", "/ofertas-academicas", "/programas/")):
            score += 8
        if "/ofertas-acad/" in low:
            # These are specific course/seminar pages, not canonical career pages —
            # never use them as source for authority/director info.
            score -= 25
        if any(tok in low for tok in ("/introduccion-a-la-vida-universitaria", "/evento", "/agenda", "/noticia", "/novedad")):
            score -= 18
        return score

    @staticmethod
    def _extract_title_from_block(block: str) -> str:
        for line in (block or "").splitlines():
            if line.startswith("Titulo:"):
                return line.replace("Titulo:", "").strip()
        return ""

    @staticmethod
    def _extract_content_from_block(block: str) -> str:
        lines = (block or "").splitlines()
        for idx, line in enumerate(lines):
            if line.startswith("Contenido:"):
                first = line.replace("Contenido:", "", 1).strip()
                rest = "\n".join(lines[idx + 1 :]).strip()
                return (first + ("\n" + rest if rest else "")).strip()
        return ""

    @staticmethod
    def _is_program_noise(text: str) -> bool:
        t = (text or "").lower()
        return any(
            token in t
            for token in (
                "concurso",
                "curso",
                "simposio",
                "jornada",
                "congreso",
                "publicación",
                "publicacion",
                "viaje",
                "intercambio",
                "ayudante",
                "examen",
                "turno",
                "calendario",
                "cronograma",
                "inscripcion",
                "inscripción",
                "mesa",
                "cargo profesoral",
                "cargos profesorales",
                "concurso docente",
                "plantel docente",
            )
        )

    @staticmethod
    def _extract_program_names_from_text(text: str) -> list[str]:
        raw = (text or "").strip()
        if not raw:
            return []
        out: list[str] = []
        seen: set[str] = set()
        # Each pattern caps at 5 words after the degree keyword to avoid greedy
        # sentence capture (e.g. "Lic. en Enfermería Logró la Acreditación").
        _W = r"[a-záéíóúñü]+"
        _NAME5 = rf"{_W}(?:\s+{_W}){{0,4}}"
        patterns = (
            rf"\blicenciatura\s+en\s+{_NAME5}",
            rf"\btecnicatura\s+en\s+{_NAME5}",
            rf"\bdoctorado\s+en\s+{_NAME5}",
            rf"\bespecializaci[oó]n\s+en\s+{_NAME5}",
            rf"\bcarrera\s+de\s+{_NAME5}",
            r"\bdise[ñn]o\s+gr[aá]fico\b",
            r"\barquitectura\b",
            r"\bmedicina\b",
        )
        for pattern in patterns:
            for match in re.findall(pattern, raw, flags=re.IGNORECASE):
                candidate = RAGService._sanitize_career_name(RAGService._clean_program_name(match))
                if candidate.lower().startswith("carrera de "):
                    candidate = RAGService._sanitize_career_name(
                        candidate[len("Carrera de ") :]
                    )
                key = RAGService._normalize_name_key(candidate)
                if not key or key in seen:
                    continue
                if not RAGService._is_plausible_career_name(candidate):
                    continue
                seen.add(key)
                out.append(candidate)
        return out

    @staticmethod
    def _extract_program_names_from_context(context_blocks: list[str]) -> tuple[list[str], list[str]]:
        names: list[str] = []
        source_urls: list[str] = []
        seen_names: set[str] = set()
        seen_program_keys: set[str] = set()
        seen_urls: set[str] = set()

        _W = r"[a-záéíóúñü]+"
        _NAME5 = rf"{_W}(?:\s+{_W}){{0,4}}"
        name_pattern = re.compile(
            rf"\b("
            rf"medicina|"
            rf"licenciatura\s+en\s+{_NAME5}|"
            rf"tecnicatura\s+en\s+{_NAME5}|"
            rf"doctorado\s+en\s+{_NAME5}|"
            rf"especializaci[oó]n\s+en\s+{_NAME5}"
            rf")\b",
            flags=re.IGNORECASE,
        )

        for block in context_blocks:
            url = RAGService._extract_url_from_block(block)
            title = RAGService._extract_title_from_block(block)
            content = RAGService._extract_content_from_block(block)
            low_url = (url or "").lower()
            low_title = (title or "").lower()

            if not url:
                continue
            if RAGService._is_program_noise(f"{low_url} {low_title}"):
                continue
            is_program_like_url = any(
                token in low_url
                for token in (
                    "/carreras/",
                    "/category/carreras",
                    "/oferta-academica/",
                    "/ofertas-academicas/",
                    "/programas/",
                    "/carrera-de-",
                )
            )
            if not is_program_like_url:
                continue

            if "/carreras/" in low_url:
                after = low_url.split("/carreras/", 1)[1].strip("/")
                slug = after.split("/", 1)[0].strip()
                if slug and slug not in {"carreras", "category", "tag"}:
                    candidate = RAGService._sanitize_career_name(RAGService._clean_program_name(slug))
                    if candidate:
                        key = RAGService._normalize_name_key(candidate)
                        pkey = RAGService._program_dedupe_key(candidate)
                        if key and pkey and key not in seen_names and pkey not in seen_program_keys and RAGService._is_plausible_career_name(candidate):
                            seen_names.add(key)
                            seen_program_keys.add(pkey)
                            names.append(candidate)
                            if url not in seen_urls:
                                seen_urls.add(url)
                                source_urls.append(url)

            for raw in [*name_pattern.findall(title), *name_pattern.findall(content)]:
                candidate = RAGService._sanitize_career_name(RAGService._clean_program_name(raw))
                if not candidate:
                    continue
                key = RAGService._normalize_name_key(candidate)
                pkey = RAGService._program_dedupe_key(candidate)
                if key and pkey and key not in seen_names and pkey not in seen_program_keys and RAGService._is_plausible_career_name(candidate):
                    seen_names.add(key)
                    seen_program_keys.add(pkey)
                    names.append(candidate)
                    if url not in seen_urls:
                        seen_urls.add(url)
                        source_urls.append(url)

            for candidate in RAGService._extract_program_names_from_text(f"{title}\n{content}"):
                key = RAGService._normalize_name_key(candidate)
                pkey = RAGService._program_dedupe_key(candidate)
                if key and pkey and key not in seen_names and pkey not in seen_program_keys and RAGService._is_plausible_career_name(candidate):
                    seen_names.add(key)
                    seen_program_keys.add(pkey)
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
            r"lic\s*\.?\s*en\s+[a-záéíóúñü\s]+|"
            r"licenciatura\s+en\s+[a-záéíóúñü\s]+|"
            r"tecnicatura\s+en\s+[a-záéíóúñü\s]+|"
            r"doctorado\s+en\s+[a-záéíóúñü\s]+|"
            r"especializaci[oó]n\s+en\s+[a-záéíóúñü\s]+|"
            r"enfermer[ií]a|"
            r"kinesio(?:log[ií]a(?:\s+y\s+fisiatr[ií]a)?)?"
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
        return any(
            t in q
            for t in (
                "director",
                "coordinador",
                "coordinacion",
                "coordinación",
                "responsable",
                "jefe",
                "jefa",
                "autoridades",
                "autoridad",
                "dirección",
                "secretario académico",
                "secretario academico",
                "secretaria academica",
                "decano",
                "decana",
                "vicedecano",
                "vicedecana",
            )
        )

    @staticmethod
    def _is_duration_query(query: str) -> bool:
        q = (query or "").lower()
        return any(t in q for t in ("duracion", "duración", "dura", "años", "anios"))

    @staticmethod
    def _is_workload_query(query: str) -> bool:
        q = re.sub(r"\s+", " ", (query or "").lower()).strip()
        return (
            "carga horaria" in q
            or "carga de horas" in q
            or ("horaria" in q and any(t in q for t in ("materia", "materias", "asignatura", "asignaturas")))
            or bool(re.search(r"\bcu[aá]ntas?\s+horas\b", q))
        )

    @staticmethod
    def _is_first_year_subjects_query(query: str) -> bool:
        q = re.sub(r"\s+", " ", (query or "").lower()).strip()
        return (
            any(t in q for t in ("materia", "materias", "asignatura", "asignaturas", "plan de estudios"))
            and any(t in q for t in ("primer año", "primer anio", "1er año", "1er anio", "año 1", "anio 1"))
        )

    @staticmethod
    def _extract_year_from_query(query: str) -> int | None:
        q = re.sub(r"\s+", " ", (query or "").lower()).strip()
        if any(t in q for t in ("primer año", "primer anio", "1er año", "1er anio", "año 1", "anio 1")):
            return 1
        if any(t in q for t in ("segundo año", "segundo anio", "2do año", "2do anio", "año 2", "anio 2")):
            return 2
        if any(t in q for t in ("tercer año", "tercer anio", "3er año", "3er anio", "año 3", "anio 3")):
            return 3
        if any(t in q for t in ("cuarto año", "cuarto anio", "4to año", "4to anio", "año 4", "anio 4")):
            return 4
        if any(t in q for t in ("quinto año", "quinto anio", "5to año", "5to anio", "año 5", "anio 5")):
            return 5
        if any(t in q for t in ("sexto año", "sexto anio", "6to año", "6to anio", "año 6", "anio 6")):
            return 6
        return None

    @staticmethod
    def _is_year_subjects_query(query: str) -> bool:
        q = re.sub(r"\s+", " ", (query or "").lower()).strip()
        return any(t in q for t in ("materia", "materias", "asignatura", "asignaturas", "plan de estudios")) and (
            RAGService._extract_year_from_query(q) is not None
        )

    @staticmethod
    def _looks_like_subjects_followup(query: str) -> bool:
        q = re.sub(r"\s+", " ", (query or "").lower()).strip()
        return any(t in q for t in ("todas", "todas las", "completas", "lista completa", "faltan"))

    @staticmethod
    def _infer_year_from_history(history: list[str], current_query: str) -> int | None:
        q_norm = re.sub(r"\s+", " ", (current_query or "").strip().lower())
        for row in reversed(history or []):
            if not row.upper().startswith("USER:"):
                continue
            text = row.split(":", 1)[1].strip() if ":" in row else ""
            t_norm = re.sub(r"\s+", " ", text.lower())
            if not t_norm or t_norm == q_norm:
                continue
            yr = RAGService._extract_year_from_query(t_norm)
            if yr is not None and any(
                t in t_norm for t in ("materia", "materias", "asignatura", "asignaturas", "plan de estudios")
            ):
                return yr
        return None

    @staticmethod
    def _history_points_to_subjects(history: list[str], current_query: str) -> bool:
        q_norm = re.sub(r"\s+", " ", (current_query or "").strip().lower())
        for row in reversed(history or []):
            if not row.upper().startswith("USER:"):
                continue
            text = row.split(":", 1)[1].strip() if ":" in row else ""
            t_norm = re.sub(r"\s+", " ", text.lower())
            if not t_norm or t_norm == q_norm:
                continue
            if any(t in t_norm for t in ("materia", "materias", "asignatura", "asignaturas", "plan de estudios")):
                return True
            if any(t in t_norm for t in ("todas", "lista completa", "completas", "faltan")) and (
                RAGService._extract_year_from_query(t_norm) is not None
            ):
                return True
        return False

    @staticmethod
    def _extract_profile_intent(query: str) -> str | None:
        q = (query or "").lower()
        for key, hints in RAGService.PROFILE_INTENT_KEYS.items():
            if any(h in q for h in hints):
                return key
        return None

    @staticmethod
    def _normalize_intent_label(intent: str | None) -> str | None:
        raw = (intent or "").strip().lower()
        if not raw:
            return None
        allowed = {
            "authority",
            "duration",
            "workload",
            "subjects",
            "programs_overview",
            "program_count",
            "admissions",
            "tramites",
            "profile_ingresantes",
            "profile_estudiantes",
            "profile_docentes",
            "profile_nodocentes",
            "profile_directivos",
        }
        return raw if raw in allowed else None

    @staticmethod
    def _intent_from_query(query: str) -> str | None:
        if RAGService._is_authority_query(query):
            return "authority"
        if RAGService._is_duration_query(query):
            return "duration"
        if RAGService._is_workload_query(query):
            return "workload"
        if RAGService._is_year_subjects_query(query):
            return "subjects"
        if RAGService._is_program_count_query(query):
            return "program_count"
        if RAGService._is_programs_query(query):
            return "programs_overview"
        if RAGService._is_admissions_query(query):
            return "admissions"
        if RAGService._is_tramites_query(query):
            return "tramites"
        profile = RAGService._extract_profile_intent(query)
        if profile:
            return f"profile_{profile}"
        return None

    def _normalized_session_state(self, raw_state: dict | None) -> dict:
        state = dict(raw_state or {})
        program = state.get("active_program")
        year = state.get("active_year")
        intent = state.get("active_intent")
        confidence_raw = state.get("active_program_confidence")
        source_raw = state.get("active_program_source")

        active_program = ""
        if isinstance(program, str):
            mentions = self._extract_program_mentions_from_text(program)
            active_program = mentions[0] if mentions else self._clean_program_name(program)
        if not active_program:
            active_program = ""

        active_year: int | None = None
        if isinstance(year, int) and 1 <= year <= 6:
            active_year = year
        elif isinstance(year, str) and year.strip().isdigit():
            y = int(year.strip())
            if 1 <= y <= 6:
                active_year = y

        active_intent = self._normalize_intent_label(intent if isinstance(intent, str) else None)
        active_program_confidence = 0.0
        try:
            active_program_confidence = float(confidence_raw)
        except Exception:  # noqa: BLE001
            active_program_confidence = 0.0
        active_program_confidence = min(1.0, max(0.0, active_program_confidence))
        if not active_program:
            active_program_confidence = 0.0
        active_program_source = (
            str(source_raw).strip().lower()
            if isinstance(source_raw, str) and str(source_raw).strip()
            else ""
        )
        if active_program_source not in {"explicit_user", "inferred_history"}:
            active_program_source = ""
        return {
            "active_program": active_program,
            "active_year": active_year,
            "active_intent": active_intent,
            "active_program_confidence": active_program_confidence,
            "active_program_source": active_program_source,
        }

    def _has_confident_program_state(self, session_state: dict | None, min_confidence: float | None = None) -> bool:
        state = self._normalized_session_state(session_state)
        program = (state.get("active_program") or "").strip()
        conf = float(state.get("active_program_confidence") or 0.0)
        threshold = self.MIN_CONFIDENT_PROGRAM if min_confidence is None else float(min_confidence)
        return bool(program) and conf >= threshold

    def _program_for_followup(self, session_state: dict | None, history: list[str], query: str) -> str:
        state = self._normalized_session_state(session_state)
        if self._has_confident_program_state(state):
            return (state.get("active_program") or "").strip()
        inferred = self._infer_program_from_history(history, query)
        return (inferred or "").strip()

    def derive_session_state(
        self,
        *,
        current_state: dict | None,
        query: str,
        history: list[str],
    ) -> dict:
        state = self._normalized_session_state(current_state)

        # Normalize abbreviations BEFORE extracting mentions so that "kinesio",
        # "kine", "enfer", etc. are resolved to the canonical program name.
        normalized_query = self._normalize_query_typos(query)
        mentions = self._extract_program_mentions_from_text(normalized_query)
        if mentions:
            state["active_program"] = mentions[0]
            state["active_program_confidence"] = 1.0
            state["active_program_source"] = "explicit_user"
        elif not state.get("active_program"):
            inferred = self._infer_program_from_history(history, query)
            if inferred:
                state["active_program"] = inferred
                state["active_program_confidence"] = max(
                    float(state.get("active_program_confidence") or 0.0),
                    0.62,
                )
                state["active_program_source"] = "inferred_history"
        else:
            state["active_program_confidence"] = max(
                float(state.get("active_program_confidence") or 0.0),
                0.8 if state.get("active_program_source") == "explicit_user" else 0.62,
            )

        year = self._extract_year_from_query(query)
        if year is not None:
            state["active_year"] = year
        elif state.get("active_intent") == "subjects":
            inferred_year = self._infer_year_from_history(history, query)
            if inferred_year is not None:
                state["active_year"] = inferred_year

        intent = self._intent_from_query(query)
        if intent is not None:
            state["active_intent"] = intent

        return state

    @staticmethod
    def _session_state_summary(session_state: dict | None) -> str:
        state = dict(session_state or {})
        program = (state.get("active_program") or "").strip()
        year = state.get("active_year")
        intent = (state.get("active_intent") or "").strip()
        year_txt = str(year) if isinstance(year, int) else "-"
        conf = float(state.get("active_program_confidence") or 0.0)
        conf_txt = f"{conf:.2f}"
        src = (state.get("active_program_source") or "").strip() or "-"
        return (
            f"Carrera activa: {program or '-'} | "
            f"Año activo: {year_txt} | "
            f"Intento activo: {intent or '-'} | "
            f"Confianza carrera: {conf_txt} | "
            f"Origen carrera: {src}"
        )

    @staticmethod
    def _is_tramites_query(query: str) -> bool:
        q = (query or "").lower()
        return any(
            t in q
            for t in (
                "tramite",
                "trámite",
                "trámites",
                "mesa de entradas",
                "gestión",
                "gestion",
                "diploma",
                "titulo",
                "título",
                "egreso",
                "biblioteca",
                "prestamo",
                "préstamo",
                "devolucion",
                "devolución",
                "pedir un libro",
                "sacar un libro",
            )
        )

    @staticmethod
    def _is_admissions_query(query: str) -> bool:
        q = (query or "").lower()
        if re.search(r"\binscrib\w*\s+a\s+la\s+(?:carrera|facultad)\b", q):
            return True
        if re.search(r"\badmi(?:s|n|c)i", q):
            return True
        return any(
            t in q
            for t in (
                "ingreso",
                "ingresante",
                "ingresantes",
                "admis",
                "inscrip",
                "anotarme",
                "matricularme",
                "matricula",
                "matriculación",
                "requisito",
                "requisitos",
                "papeles",
                "documentacion",
                "documentación",
                "documentos",
            )
        )

    @staticmethod
    def _extract_admissions_key_lines(text: str, target_year: int | None = None) -> list[str]:
        raw = (text or "")
        if not raw:
            return []
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.splitlines()]
        lines = [ln for ln in lines if ln]
        if not lines:
            return []

        date_pattern = re.compile(
            r"\b\d{1,2}\s+de\s+"
            r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
            r"\s+de\s+(20\d{2})\b",
            flags=re.IGNORECASE,
        )
        range_pattern = re.compile(
            r"\b(desde|del)\b.{0,80}\b(al|hasta)\b.{0,80}\b20\d{2}\b",
            flags=re.IGNORECASE,
        )
        key_token_pattern = re.compile(
            r"(inscrip|ingreso|ingresante|admis|requisit|documentaci[oó]n|papeles|ciclo lectivo)",
            flags=re.IGNORECASE,
        )
        requirement_pattern = re.compile(
            r"\b(dni|fotocopia|partida|t[ií]tulo|anal[ií]tico|constancia|certificado|pago|comprobante)\b",
            flags=re.IGNORECASE,
        )

        preferred_years: set[int] = set()
        if isinstance(target_year, int) and 2000 <= target_year <= 2100:
            preferred_years.add(target_year)
            preferred_years.add(target_year - 1)

        scored: list[tuple[int, str]] = []
        seen: set[str] = set()
        for line in lines:
            low = line.lower()
            score = 0
            date_years = [int(y) for _, y in date_pattern.findall(line)]
            has_date = bool(date_years)
            if has_date:
                score += 20
            if range_pattern.search(line):
                score += 24
            if key_token_pattern.search(low):
                score += 14
            if requirement_pattern.search(low):
                score += 8
            if preferred_years and any(y in preferred_years for y in date_years):
                score += 18
            if "preguntas frecuentes" in low:
                score += 4
            if len(line) > 260:
                score -= 6
            if score <= 0:
                continue
            key = low
            if key in seen:
                continue
            seen.add(key)
            scored.append((score, line))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [line for _, line in scored[:8]]

    @staticmethod
    def _extract_target_year_from_query(query: str) -> int | None:
        q = (query or "").lower()
        years = re.findall(r"\b(20\d{2})\b", q)
        if years:
            try:
                return int(years[-1])
            except ValueError:
                return None
        m = re.search(r"ciclo lectivo\s+(\d{4})", q, flags=re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None

    @staticmethod
    def _is_program_count_query(query: str) -> bool:
        q = re.sub(r"\s+", " ", (query or "").strip().lower())
        if RAGService._is_duration_query(q):
            return False
        # Count intent should be explicit and usually plural/global scope.
        if re.search(r"\b(cu[aá]ntas?|cantidad|n[uú]mero|numero)\b", q, flags=re.IGNORECASE) is None:
            return False
        return any(
            p in q
            for p in (
                "cuantas carreras",
                "cuántas carreras",
                "cantidad de carreras",
                "numero de carreras",
                "número de carreras",
                "cuantos programas",
                "cuántos programas",
                "cantidad de programas",
                "oferta academica",
                "oferta académica",
            )
        )

    @staticmethod
    def _wants_secretary(query: str) -> bool:
        q = (query or "").lower()
        return any(t in q for t in ("secretario académico", "secretario academico", "secretaria academica"))

    @staticmethod
    def _wants_director(query: str) -> bool:
        q = (query or "").lower()
        return any(t in q for t in ("director", "coordinador", "responsable", "autoridad", "dirección", "direccion"))

    @staticmethod
    def _wants_dean(query: str) -> bool:
        q = (query or "").lower()
        return any(t in q for t in ("decano", "decana", "vicedecano", "vicedecana"))

    @staticmethod
    def _wants_vice_dean(query: str) -> bool:
        q = (query or "").lower()
        return bool(re.search(r"\b(?:vice\s*decan[oa]|vicedecan[oa])\b", q))

    @staticmethod
    def _wants_dean_only(query: str) -> bool:
        q = (query or "").lower()
        if RAGService._wants_vice_dean(q):
            return False
        return bool(re.search(r"\bdecan[oa]\b", q))

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
    def _infer_intent_query_from_history(history: list[str]) -> str | None:
        for row in reversed(history or []):
            if not row.upper().startswith("USER:"):
                continue
            text = row.split(":", 1)[1].strip() if ":" in row else ""
            if not text:
                continue
            low = text.lower()
            if (
                RAGService._is_authority_query(text)
                or RAGService._is_duration_query(text)
                or RAGService._is_admissions_query(text)
                or RAGService._is_tramites_query(text)
                or RAGService._is_year_subjects_query(text)
                or any(
                    tok in low
                    for tok in (
                        "materia",
                        "materias",
                        "asignatura",
                        "asignaturas",
                        "requisito",
                        "requisitos",
                        "papeles",
                        "documentacion",
                        "documentación",
                    )
                )
            ):
                return text
        return None

    @staticmethod
    def _slugify_program_name(program_name: str) -> str:
        norm = unicodedata.normalize("NFKD", (program_name or "").strip().lower())
        norm = "".join(ch for ch in norm if not unicodedata.combining(ch))
        norm = re.sub(r"^\s*lic\.?\s+en\s+", "licenciatura-en-", norm, flags=re.IGNORECASE)
        norm = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
        return norm

    @staticmethod
    def _slug_candidates_for_program(program_name: str) -> list[str]:
        base_slug = RAGService._slugify_program_name(program_name)
        if not base_slug:
            return []
        variants = [base_slug]
        if base_slug.startswith("lic-en-"):
            variants.append(base_slug.replace("lic-en-", "licenciatura-en-", 1))
        if base_slug.startswith("lic-"):
            variants.append(base_slug.replace("lic-", "licenciatura-", 1))
        if "enfermeria" in base_slug and not base_slug.startswith("licenciatura-en-"):
            variants.append(f"licenciatura-en-{base_slug.split('enfermeria')[0].strip('-')}enfermeria".replace("--", "-"))
            variants.append("licenciatura-en-enfermeria")
        deduped: list[str] = []
        seen: set[str] = set()
        for v in variants:
            key = (v or "").strip("-")
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return deduped

    @staticmethod
    def _url_matches_program_slugs(url: str, slugs: list[str]) -> bool:
        low = (url or "").lower()
        if not low or not slugs:
            return False
        for slug in slugs:
            s = (slug or "").strip().lower().strip("/")
            if not s:
                continue
            if (
                f"/carreras/{s}" in low
                or f"/oferta-academica/{s}" in low
                or f"/ofertas-acad/{s}" in low
                or f"/ofertas-academicas/{s}" in low
                or f"/programas/{s}" in low
            ):
                return True
        return False

    @staticmethod
    def _program_slug_from_url(url: str) -> str:
        low = (url or "").lower()
        if "/carreras/" not in low:
            return ""
        tail = low.split("/carreras/", 1)[1].strip("/")
        if not tail:
            return ""
        return tail.split("/", 1)[0].strip()

    @staticmethod
    def _program_doc_relevance_score(
        *,
        url: str,
        title: str,
        text_value: str,
        slugs: list[str],
        program_variants: list[str],
    ) -> int:
        low_url = (url or "").lower()
        low_title = (title or "").lower()
        snippet = (text_value or "")[:2500].lower()
        score = 0

        if slugs and RAGService._url_matches_program_slugs(url, slugs):
            score += 120

        doc_slug = RAGService._program_slug_from_url(url)
        if doc_slug:
            if any(doc_slug == s or doc_slug.startswith(f"{s}-") or s.startswith(f"{doc_slug}-") for s in slugs):
                score += 40
            else:
                score -= 95

        for variant in [v for v in program_variants if len(v) >= 5][:6]:
            if variant in low_title:
                score += 30
            if variant in snippet:
                score += 18

        if "/carreras/" in low_url:
            score += 18
        if any(tok in low_url for tok in ("/plan-de-estudios", "/distribucion-de-asignaturas")):
            score += 20
        if "/admin-contenidos-are/" in low_url:
            score -= 15
        if "/wp-content/uploads/" in low_url or ".pdf" in low_url:
            score -= 25

        return score

    @staticmethod
    def _fact_source_quality_score(url: str, fact_key: str) -> int:
        low = (url or "").lower()
        key = (fact_key or "").strip().lower()
        score = 0
        if "/carreras/" in low:
            score += 70
        elif any(tok in low for tok in ("/oferta-academica/", "/ofertas-acad/", "/programas/")):
            score += 40
        elif any(tok in low for tok in ("/plan-de-estudios", "/distribucion-de-asignaturas")):
            score += 34
        if "/admin-contenidos-are/" in low:
            score -= 22
        if "/wp-content/uploads/" in low or ".pdf" in low:
            score -= 38 if key.startswith("year_") else 18
        return score

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

    @staticmethod
    def _excerpt_for_query(text: str, query: str, radius: int = 900) -> str:
        lowered = (text or "").lower()
        q_tokens = RAGService._extract_query_tokens(query)
        best_pos = -1
        for tok in q_tokens:
            pos = lowered.find(tok)
            if pos >= 0:
                best_pos = pos
                break
        if best_pos < 0:
            if RAGService._is_authority_query(query):
                for hint in ("director", "coordinador", "responsable", "secretario"):
                    pos = lowered.find(hint)
                    if pos >= 0:
                        best_pos = pos
                        break
            if best_pos < 0 and RAGService._is_duration_query(query):
                for hint in ("duración", "duracion", "años", "anios"):
                    pos = lowered.find(hint)
                    if pos >= 0:
                        best_pos = pos
                        break
        if best_pos < 0:
            return (text or "")[:2200]
        start = max(0, best_pos - radius)
        end = min(len(text), best_pos + radius)
        return (text or "")[start:end]

    @staticmethod
    def _clip_text(value: str, max_chars: int) -> str:
        text = (value or "").strip()
        if max_chars <= 0:
            return ""
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "..."

    @staticmethod
    def _trim_context_block(block: str, max_chars: int) -> str:
        raw = (block or "").strip()
        if not raw:
            return ""
        if len(raw) <= max_chars:
            return raw
        lines = raw.splitlines()
        header: list[str] = []
        content_lines: list[str] = []
        in_content = False
        for line in lines:
            if not in_content and line.startswith("Contenido:"):
                in_content = True
                content_lines.append(line.replace("Contenido:", "", 1).strip())
                continue
            if in_content:
                content_lines.append(line)
            else:
                header.append(line)
        header_text = "\n".join(header).strip()
        content_text = "\n".join(content_lines).strip()
        content_budget = max(120, max_chars - len(header_text) - 20)
        content_text = RAGService._clip_text(content_text, content_budget)
        rebuilt = header_text
        if content_text:
            rebuilt = (rebuilt + "\n" if rebuilt else "") + f"Contenido: {content_text}"
        return rebuilt[:max_chars]

    def _history_for_prompt(self, history_blocks: list[str]) -> list[str]:
        if not history_blocks:
            return []
        out: list[str] = []
        total = 0
        for block in history_blocks[-self.MAX_HISTORY_ITEMS :]:
            clipped = self._clip_text(block, self.MAX_HISTORY_CHARS_PER_ITEM)
            if not clipped:
                continue
            extra = len(clipped) + 1
            if total + extra > self.MAX_HISTORY_CHARS_TOTAL:
                break
            out.append(clipped)
            total += extra
        return out

    def _contexts_for_prompt(self, context_blocks: list[str]) -> list[str]:
        if not context_blocks:
            return []
        out: list[str] = []
        total = 0
        for block in context_blocks:
            clipped = self._trim_context_block(block, self.MAX_CONTEXT_CHARS_PER_BLOCK)
            if not clipped:
                continue
            extra = len(clipped) + 6
            if total + extra > self.MAX_CONTEXT_CHARS_TOTAL:
                break
            out.append(clipped)
            total += extra
        return out

    async def _retrieve_authority_context_from_program(self, source_url: str, program_name: str) -> list[str]:
        if not self.ENABLE_RUNTIME_SCRAPE:
            return []
        parsed = urlparse(source_url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            return []
        base = f"https://{parsed.netloc}/"
        slugs = self._slug_candidates_for_program(program_name)
        if not slugs:
            return []
        candidates: list[str] = []
        for slug in slugs:
            # Authority context must come from canonical career pages only.
            # /ofertas-acad/ are specific courses/seminars — their coordinators
            # are NOT career directors.
            candidates.extend(
                [
                    urljoin(base, f"carreras/{slug}/"),
                    urljoin(base, f"carreras/{slug}"),
                ]
            )
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
                scrape_result = await self.scraper.scrape_page(candidate)
            except Exception:  # noqa: BLE001
                continue
            if not scrape_result.success:
                continue
            title = scrape_result.title
            content = scrape_result.markdown
            content = (content or "").strip()
            if not content:
                continue
            if is_non_academic_noise(candidate, title or "", content):
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
        normalized = RAGService._normalize_query_typos(query)
        return len(RAGService._extract_program_mentions_from_text(normalized)) > 0

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
        asks_program_specific = (
            RAGService._is_authority_query(q)
            or RAGService._is_workload_query(q)
            or RAGService._is_year_subjects_query(q)
            or RAGService._is_admissions_query(q)
            or RAGService._is_tramites_query(q)
        )
        mentions_program_scope = any(
            t in q for t in ("carrera", "programa", "oferta", "esta carrera", "esa carrera")
        )
        return asks_program_specific and mentions_program_scope and not RAGService._query_has_specific_program(query)

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
        if (
            RAGService._is_authority_query(q)
            or RAGService._is_duration_query(q)
            or RAGService._is_year_subjects_query(q)
            or RAGService._is_admissions_query(q)
            or RAGService._is_tramites_query(q)
        ):
            return False
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
        if any(
            t in q
            for t in (
                "director",
                "coordinador",
                "responsable",
                "autoridad",
                "secretario",
                "secretaria",
            )
        ):
            has_authority_token = any(
                token in joined
                for token in (
                    "director de carrera",
                    "directora de carrera",
                    "coordinador",
                    "responsable de carrera",
                    "secretario académico",
                    "secretario academico",
                    "secretaria academica",
                )
            )
            has_canonical_authority_url = any(
                "/carreras/" in RAGService._extract_url_from_block(block).lower()
                for block in contexts
            )
            all_old_event_like = all(RAGService._is_event_like_old_block(block) for block in contexts) if contexts else True
            return (not has_authority_token) or (not has_canonical_authority_url) or all_old_event_like
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

    @staticmethod
    def _normalize_program_for_lookup(value: str) -> str:
        raw = re.sub(r"\s+", " ", (value or "").strip().lower())
        norm = unicodedata.normalize("NFKD", raw)
        return "".join(ch for ch in norm if not unicodedata.combining(ch))

    @staticmethod
    def _program_lookup_variants(program_name: str) -> list[str]:
        base = RAGService._normalize_program_for_lookup(program_name)
        if not base:
            return []
        variants = {base}
        variants.add(re.sub(r"^lic\s*\.?\s*en\s+", "licenciatura en ", base, flags=re.IGNORECASE))
        variants.add(re.sub(r"^licenciatura en\s+", "", base, flags=re.IGNORECASE))
        variants.add(re.sub(r"^lic\s*\.?\s*en\s+", "", base, flags=re.IGNORECASE))
        if "enfermeria" in base or "enfermería" in base:
            variants.add("licenciatura en enfermeria")
            variants.add("enfermeria")
        if any(t in base for t in ("kinesiologia", "kinesiología", "kinesio", "kine")):
            variants.add("licenciatura en kinesiologia y fisiatria")
            variants.add("kinesiologia y fisiatria")
        return sorted([v for v in variants if v], key=len, reverse=True)

    @staticmethod
    def _pick_best_fact_row(rows: list[dict]) -> dict | None:
        if not rows:
            return None
        now = datetime.now(timezone.utc)
        scored: list[tuple[int, dict]] = []
        for row in rows:
            url = (row.get("canonical_url") or "").strip().lower()
            conf = float(row.get("confidence") or 0.0)
            fetched_at = row.get("fetched_at")
            score = int(conf * 100)
            if "/carreras/" in url:
                score += 40
            elif "/oferta-academica/" in url or "/ofertas-acad/" in url:
                score += 12
            if fetched_at is not None:
                try:
                    if fetched_at.tzinfo is None:
                        fetched_dt = fetched_at.replace(tzinfo=timezone.utc)
                    else:
                        fetched_dt = fetched_at.astimezone(timezone.utc)
                    age_days = max(0, int((now - fetched_dt).total_seconds() // 86400))
                    if age_days <= 30:
                        score += 15
                    elif age_days <= 90:
                        score += 10
                    elif age_days <= 180:
                        score += 5
                    elif age_days > 365:
                        score -= 8
                except Exception:  # noqa: BLE001
                    pass
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1] if scored else None

    @staticmethod
    def _is_canonical_program_url(url: str) -> bool:
        low = (url or "").lower()
        return any(
            token in low
            for token in ("/carreras/", "/oferta-academica/", "/ofertas-acad/", "/ofertas-academicas/")
        )

    @staticmethod
    def _is_plausible_duration_fact(value: str) -> bool:
        raw = re.sub(r"\s+", " ", (value or "").lower()).strip()
        m = re.search(r"(\d{1,2})\s*(?:a[nñ]os|años)\b", raw)
        if not m:
            return False
        years = int(m.group(1))
        return 1 <= years <= 12

    @staticmethod
    def _is_plausible_subject_name(value: str) -> bool:
        v = re.sub(r"\s+", " ", (value or "").strip(" .:-\t")).lower()
        if not v:
            return False
        if len(v) < 4 or len(v) > 120:
            return False
        blocked = (
            "periodo",
            "período",
            "matricul",
            "clave",
            "aula virtual",
            "inscrip",
            "resoluci",
            "descargar",
            "hacer clic",
            "link",
            "pdf",
            "www.",
            "http",
            "siu",
            "calendario",
            "cronograma",
            "mesa de examen",
            "lunes",
            "martes",
            "miercoles",
            "miércoles",
            "jueves",
            "viernes",
            "sabado",
            "sábado",
            "domingo",
            "primer año",
            "primer anio",
            "segundo año",
            "segundo anio",
            "tercer año",
            "tercer anio",
            "cuarto año",
            "cuarto anio",
            "quinto año",
            "quinto anio",
            "sexto año",
            "sexto anio",
        )
        if any(tok in v for tok in blocked):
            return False
        alpha_tokens = re.findall(r"[a-záéíóúñü]{2,}", v, flags=re.IGNORECASE)
        if len(alpha_tokens) >= 2:
            return True
        if len(alpha_tokens) == 1 and len(alpha_tokens[0]) >= 8:
            return True
        return False

    @staticmethod
    def _is_plausible_authority_value(value: str) -> bool:
        raw = re.sub(r"\s+", " ", (value or "").strip())
        if not raw:
            return False
        low = raw.lower()
        blocked_tokens = (
            "licenciatura",
            "tecnicatura",
            "doctorado",
            "especializacion",
            "especialización",
            "enfermer",
            "kinesiolog",
            "medicina",
            "carrera",
            "plan de estudios",
            "duración",
            "duracion",
            "oferta",
            "programa",
            "materia",
            "asignatura",
        )
        if any(tok in low for tok in blocked_tokens):
            return False

        cleaned = re.sub(r"^(dr\.?|dra\.?|lic\.?|mg\.?|msc\.?|prof\.?)\s+", "", raw, flags=re.IGNORECASE)
        parts = [p for p in re.split(r"\s+", cleaned) if p]
        if len(parts) < 2:
            return False
        return sum(1 for p in parts if re.fullmatch(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü'`-]{2,}", p)) >= 2

    @staticmethod
    def _extract_titled_authority_candidates(text_value: str, max_scan_lines: int = 0) -> list[str]:
        lines = (text_value or "").splitlines()
        if not lines:
            return []
        out: list[str] = []
        seen: set[str] = set()
        title_token = re.compile(
            r"\b(?:prof\.?|dr\.?|dra\.?|lic\.?|mgter\.?|mgtr\.?|mg\.?|esp\.?|msc\.?)\b",
            flags=re.IGNORECASE,
        )
        scan_lines = lines if max_scan_lines <= 0 else lines[:max_scan_lines]
        for raw in scan_lines:
            line = (raw or "").strip()
            if not line:
                continue
            line = re.sub(r"^\s*[-*>\d.)#\s_`]+", "", line)
            line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
            line = re.sub(r"[*_`]+", "", line)
            line = re.sub(r"\s+", " ", line).strip(" .:-\t")
            if not line or len(line) > 120:
                continue
            low = line.lower()
            if any(tok in low for tok in ("@", "http://", "https://", "cuerpo docente", "plan de estudios")):
                continue
            if not title_token.search(line):
                continue
            if not RAGService._is_plausible_authority_value(line):
                continue
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(line)
        return out

    @staticmethod
    def _best_year_block(text: str, year_num: int, labels: dict[int, tuple[str, ...]]) -> str:
        source = text or ""
        if not source:
            return ""
        low = source.lower()
        starts = labels.get(year_num, ())
        if not starts:
            return ""

        candidate_starts: list[int] = []
        for label in starts:
            for m in re.finditer(re.escape(label), low):
                candidate_starts.append(m.start())
        if not candidate_starts:
            return ""

        next_markers: list[int] = []
        for y, lbls in labels.items():
            if y <= year_num:
                continue
            for label in lbls:
                for m in re.finditer(re.escape(label), low):
                    next_markers.append(m.start())
        next_markers.sort()

        best_block = ""
        best_score = -1
        for start in sorted(set(candidate_starts)):
            end = len(source)
            for marker in next_markers:
                if marker > start:
                    end = marker
                    break
            block = source[start:end]
            if not block:
                continue
            block_low = block.lower()
            score = 0
            score += len(re.findall(r"(?:^|\n)\s*(?:materia|asignatura)\s*:", block_low, flags=re.IGNORECASE)) * 7
            score += len(re.findall(r"\b(?:semestre|cuatrimestre)\b", block_low, flags=re.IGNORECASE)) * 2
            score += len(re.findall(r"(?:^|\n)\s*(?:[-*]\s+|\d{1,2}[.)]\s+)", block, flags=re.IGNORECASE))
            score += min(len(block) // 500, 4)
            if score > best_score or (score == best_score and len(block) > len(best_block)):
                best_score = score
                best_block = block
        return best_block

    @staticmethod
    def _extract_subjects_from_year_block(text: str, year_num: int) -> list[str]:
        q = (text or "")
        labels = {
            1: ("primer año", "primer anio", "1er año", "1er anio", "año 1", "anio 1"),
            2: ("segundo año", "segundo anio", "2do año", "2do anio", "año 2", "anio 2"),
            3: ("tercer año", "tercer anio", "3er año", "3er anio", "año 3", "anio 3"),
            4: ("cuarto año", "cuarto anio", "4to año", "4to anio", "año 4", "anio 4"),
            5: ("quinto año", "quinto anio", "5to año", "5to anio", "año 5", "anio 5"),
            6: ("sexto año", "sexto anio", "6to año", "6to anio", "año 6", "anio 6"),
        }
        block = RAGService._best_year_block(q, year_num, labels)
        if not block:
            return []
        subjects = re.findall(
            r"(?:^|\n)\s*(?:materia|asignatura)\s*:\s*(?:\n\s*)?([^\n]{3,120})",
            block,
            flags=re.IGNORECASE,
        )
        if not subjects:
            subjects = re.findall(
                r"(?:^|\n)\s*(?:[-*]\s+|\d{1,2}[.)]\s+)([A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9\s\-/]{3,120})",
                block,
                flags=re.IGNORECASE,
            )
        if not subjects:
            subjects = re.findall(
                r"(?:^|\n)\s*([A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9\s().,\-/]{3,120})\s*\|",
                block,
                flags=re.IGNORECASE,
            )
        out: list[str] = []
        seen: set[str] = set()
        for s in subjects:
            v = re.sub(r"\s+", " ", (s or "").strip(" .:-\t"))
            if not v:
                continue
            if not RAGService._is_plausible_subject_name(v):
                continue
            key = v.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(v)
        return out

    async def _answer_from_documents(
        self,
        source_id: str,
        query: str,
        history: list[str],
        session_state: dict | None = None,
    ) -> str | None:
        if not source_id:
            return None
        try:
            UUID(source_id)
        except ValueError:
            return None

        normalized_state = self._normalized_session_state(session_state)
        program_mentions = self._extract_program_mentions_from_text(query)
        inferred_program = normalized_state.get("active_program") or self._infer_program_from_history(history, query)
        program_name = program_mentions[0] if program_mentions else inferred_program
        program_variants = self._program_lookup_variants(program_name or "")
        slugs = self._slug_candidates_for_program(program_name or "")
        asked_year = (
            self._extract_year_from_query(query)
            or normalized_state.get("active_year")
            or self._infer_year_from_history(history, query)
        )
        query_params: dict[str, Any] = {"source_id": str(UUID(source_id))}
        slug_filters: list[str] = []
        for idx, slug in enumerate(slugs[:8], start=1):
            key = f"slug_like_{idx}"
            query_params[key] = f"%{slug}%"
            slug_filters.append(
                f"(d.canonical_url ILIKE :{key} OR COALESCE(d.title, '') ILIKE :{key})"
            )
        slug_clause = f" AND ({' OR '.join(slug_filters)})" if slug_filters else ""

        if asked_year is not None:
            doc_scope_clause = "TRUE"
        else:
            doc_scope_clause = """
                    d.page_type IN ('career_canonical', 'offer_canonical', 'curriculum')
                    OR
                    d.canonical_url ILIKE '%/carreras/%'
                    OR d.canonical_url ILIKE '%/oferta-academica/%'
                    OR d.canonical_url ILIKE '%/ofertas-acad/%'
                    OR d.canonical_url ILIKE '%/ofertas-academicas/%'
            """
        relation = await self._resolve_embeddings_relation()
        if relation:
            embeddings_table, text_col, seq_col = relation
            stmt = text(
                f"""
                SELECT d.canonical_url, COALESCE(d.title, '') AS title, d.fetched_at,
                       string_agg(de.{text_col}, E'\n' ORDER BY de.{seq_col}) AS full_text
                FROM documents d
                JOIN {embeddings_table} de ON de.doc_id = d.doc_id
                WHERE d.source_id = CAST(:source_id AS uuid)
                  {slug_clause}
                  AND (
                    {doc_scope_clause}
                  )
                GROUP BY d.doc_id
                ORDER BY
                  CASE WHEN d.canonical_url ILIKE '%/carreras/%' THEN 0 ELSE 1 END,
                  d.fetched_at DESC
                LIMIT 80
                """
            )
        else:
            # Stable fallback when embedding relations are unavailable.
            stmt = text(
                f"""
                SELECT
                  d.canonical_url,
                  COALESCE(d.title, '') AS title,
                  d.fetched_at,
                  COALESCE(d.title, '') AS full_text
                FROM documents d
                WHERE d.source_id = CAST(:source_id AS uuid)
                  {slug_clause}
                  AND (
                    {doc_scope_clause}
                  )
                ORDER BY
                  CASE WHEN d.canonical_url ILIKE '%/carreras/%' THEN 0 ELSE 1 END,
                  d.fetched_at DESC
                LIMIT 80
                """
            )
        async with async_session() as session:
            rows = (await session.execute(stmt, query_params)).mappings().all()
        if not rows:
            return None

        docs: list[dict] = []
        for row in rows:
            url = str(row.get("canonical_url") or "").strip()
            title = str(row.get("title") or "").strip()
            txt = str(row.get("full_text") or "").strip()
            if not url or not txt:
                continue
            relevance = 0
            if slugs:
                relevance = self._program_doc_relevance_score(
                    url=url,
                    title=title,
                    text_value=txt,
                    slugs=slugs,
                    program_variants=program_variants,
                )
                if relevance < 40:
                    continue
            docs.append({"url": url, "title": title, "text": txt, "relevance": relevance})
        if not docs:
            if slugs:
                return None
            docs = [
                {
                    "url": str(r.get("canonical_url") or ""),
                    "title": str(r.get("title") or ""),
                    "text": str(r.get("full_text") or ""),
                    "relevance": 0,
                }
                for r in rows
            ]
        docs.sort(key=lambda d: int(d.get("relevance") or 0), reverse=True)
        strict_program_intent = bool(slugs) and (
            self._is_authority_query(query) or self._is_duration_query(query) or asked_year is not None
        )
        if strict_program_intent:
            scoped_docs = []
            for d in docs:
                if int(d.get("relevance") or 0) >= 70:
                    scoped_docs.append(d)
            if scoped_docs:
                docs = scoped_docs

        q = (query or "").lower()
        target_year = self._extract_target_year_from_query(query)
        history_subjects = self._history_points_to_subjects(history, query)
        if self._is_admissions_query(q):
            best: tuple[int, list[str], str] | None = None
            for d in docs:
                low_url = d["url"].lower()
                if any(t in low_url for t in ("/diploma", "/diplomas", "/egresad", "/graduad")):
                    continue
                if any(t in low_url for t in ("/ingreso", "/ingresantes", "/inscrip", "/admision", "/admis", "/requisitos")):
                    score = 100
                elif "/comunidad/estudiantes/" in low_url and "inscrip" in low_url:
                    score = 85
                else:
                    continue
                lines = self._extract_admissions_key_lines(d["text"], target_year=target_year)
                if not lines:
                    excerpt = self._excerpt_for_query(d["text"], query)
                    if excerpt:
                        lines = [excerpt]
                if not lines:
                    continue
                score += min(18, len(lines) * 3)
                if best is None or score > best[0]:
                    best = (score, lines, d["url"])
            if best:
                bullets = "\n".join(f"- {line}" for line in best[1][:5])
                return (
                    "Para inscribirte, esta es la información encontrada en la página oficial:\n"
                    f"- {best[2]}\n\n"
                    f"Datos clave:\n{bullets}"
                )
        if self._is_duration_query(q):
            best: tuple[int, str, str] | None = None
            for d in docs:
                low_url = d["url"].lower()
                if "/wp-content/uploads/" in low_url or ".pdf" in low_url:
                    continue
                m = re.search(
                    r"(?:duraci[oó]n(?:\s+de\s+la\s+carrera)?\s*[:\-]?\s*)([^\n]{1,80})",
                    d["text"],
                    flags=re.IGNORECASE,
                )
                if not m:
                    continue
                value = re.sub(r"\s+", " ", m.group(1)).strip()
                if not self._is_plausible_duration_fact(value):
                    continue
                score = 100 if "/carreras/" in d["url"].lower() else 70
                if best is None or score > best[0]:
                    best = (score, value, d["url"])
            if best:
                pname = program_name or "la carrera"
                return f"La duración de {pname} es {best[1]}. Fuente: {best[2]}"

        if self._is_authority_query(q):
            candidates: list[tuple[int, str, str]] = []
            wants_vice_dean = self._wants_vice_dean(q)
            wants_dean_only = self._wants_dean_only(q)
            for d in docs:
                low_url = d["url"].lower()
                value = ""
                if "/wp-content/uploads/" in low_url or ".pdf" in low_url:
                    continue
                if wants_vice_dean:
                    patterns = (
                        r"(?:vice\s*decano(?:a)?|vicedecano(?:a)?)\s*[:\-]\s*([^\n|]{3,120})",
                        r"(?:^|\n)\s*#{1,6}\s*(?:vice\s*decano(?:a)?|vicedecano(?:a)?)\s*\n+\s*([^\n|]{2,120})",
                    )
                elif wants_dean_only:
                    patterns = (
                        r"(?:(?<!vice\s)(?<!vice)decano(?:a)?)\s*[:\-]\s*([^\n|]{3,120})",
                        r"(?:^|\n)\s*#{1,6}\s*(?:(?<!vice\s)(?<!vice)decano(?:a)?)\s*\n+\s*([^\n|]{2,120})",
                    )
                else:
                    patterns = (
                        r"(?:director(?:a)?\s+de\s+carrera|direcci[oó]n\s+de\s+carrera|coordinador(?:a)?(?:\s+de\s+carrera)?|responsable\s+de\s+carrera)\s*[:\-]\s*([^\n|]{3,120})",
                        r"(?:director(?:a)?|coordinador(?:a)?|responsable(?:\s+acad[eé]mic[oa])?|jef(?:e|a)\s+de\s+carrera)\s*[:\-]\s*([^\n|]{3,120})",
                        r"(?:direcci[oó]n\s+de\s+(?:la\s+)?carrera)\s*(?:es|:|-)?\s*([A-ZÁÉÍÓÚÑ][^\n|]{2,120})",
                        r"(?:director(?:a)?\s+de\s+(?:la\s+)?carrera(?:\s+de)?[^\n:|]{0,90}?)\s+(?:es\s+)?([A-ZÁÉÍÓÚÑ][^\n|]{2,120})",
                        r"(?:^|\n)\s*#{1,6}\s*(?:director(?:a)?|direcci[oó]n(?:\s+de\s+carrera)?|coordinador(?:a)?(?:\s+de\s+carrera)?|responsable(?:\s+de\s+carrera)?)\s*\n+\s*([^\n|]{2,120})",
                        r"(?:^|\n)\s*#{1,6}\s*(?:director(?:a)?|direcci[oó]n|coordinador(?:a)?|responsable)[^\n]*\n+\s*([^\n|]{2,120})",
                        r"(?:^|\n)\s*(?:director(?:a)?|direcci[oó]n(?:\s+de\s+carrera)?|coordinador(?:a)?(?:\s+de\s+carrera)?|responsable(?:\s+de\s+carrera)?)\s*\n+\s*([^\n|]{2,120})",
                        r"(?:secretario(?:a)?\s+acad[eé]mic[oa])\s*[:\-]\s*([^\n|]{3,120})",
                        r"(?:decano(?:a)?|vicedecano(?:a)?)\s*[:\-]\s*([^\n|]{3,120})",
                    )
                for p in patterns:
                    m = re.search(p, d["text"], flags=re.IGNORECASE)
                    if not m:
                        continue
                    value = re.sub(r"\s+", " ", m.group(1)).strip(" .:-*_`\t")
                    if not self._is_plausible_authority_value(value):
                        continue
                    score = 100 if "/carreras/" in d["url"].lower() else 70
                    candidates.append((score, value, d["url"]))
                    break
                if (
                    not value
                    and re.search(r"/carreras/[^/]+/?$", low_url)
                ):
                    titled = self._extract_titled_authority_candidates(d["text"])
                    if titled:
                        score = 90
                        candidates.append((score, titled[0], d["url"]))
            if not candidates and relation and program_variants:
                embeddings_table, text_col, _seq_col = relation
                slug_candidates = self._slug_candidates_for_program(program_name or "")
                like_params: dict[str, str] = {}
                slug_filters: list[str] = []
                for idx, slug in enumerate(slug_candidates[:6], start=1):
                    key = f"url_like_{idx}"
                    like_params[key] = f"%/carreras/{slug}%"
                    slug_filters.append(f"d.canonical_url ILIKE :{key}")
                if slug_filters:
                    sql = text(
                        f"""
                        SELECT d.canonical_url, COALESCE(d.title, '') AS title, de.{text_col} AS chunk_text, d.fetched_at
                        FROM {embeddings_table} de
                        JOIN documents d ON d.doc_id = de.doc_id
                        WHERE d.source_id = CAST(:source_id AS uuid)
                          AND ({' OR '.join(slug_filters)})
                          AND d.canonical_url ILIKE '%/carreras/%'
                        ORDER BY d.fetched_at DESC
                        LIMIT 600
                        """
                    )
                    async with async_session() as session:
                        chunk_rows = (
                            await session.execute(
                                sql,
                                {"source_id": str(UUID(source_id)), **like_params},
                            )
                        ).mappings().all()
                    for row in chunk_rows:
                        src = str(row.get("canonical_url") or "").strip()
                        text_chunk = str(row.get("chunk_text") or "").strip()
                        if not src or not text_chunk:
                            continue
                        if is_non_academic_noise(src, str(row.get("title") or ""), text_chunk):
                            continue
                        titled = self._extract_titled_authority_candidates(text_chunk)
                        if not titled:
                            continue
                        score = self._authority_url_score(src) + 8
                        candidates.append((score, titled[0], src))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                label = (
                    "vicedecano/a"
                    if self._wants_vice_dean(q)
                    else (
                        "decano/a"
                        if self._wants_dean(q)
                        else ("secretario/a académico/a" if self._wants_secretary(q) else "director/a de carrera")
                    )
                )
                pname = program_name or "la carrera"
                return f"El/la {label} de {pname} es {candidates[0][1]}. Fuente: {candidates[0][2]}"

        if asked_year is not None and (
            any(t in q for t in ("materia", "materias", "asignatura", "asignaturas", "plan de estudios", "todas", "complet"))
            or history_subjects
        ):
            merged: list[str] = []
            partial_merged: list[str] = []
            partial_src = ""
            src = ""
            for d in docs:
                low_url = d["url"].lower()
                if "/wp-content/uploads/" in low_url or ".pdf" in low_url:
                    continue
                is_partial_admin = "/admin-contenidos-are/" in low_url
                if (
                    "/carreras/" not in low_url
                    and "plan-de-estudios" not in low_url
                    and "distribucion-de-asignaturas" not in low_url
                ):
                    continue
                subs = self._extract_subjects_from_year_block(d["text"], asked_year)
                if not subs:
                    continue
                if is_partial_admin:
                    if not partial_src:
                        partial_src = d["url"]
                    partial_merged.extend(subs)
                    continue
                if not src:
                    src = d["url"]
                merged.extend(subs)
            if merged:
                out: list[str] = []
                seen: set[str] = set()
                for s in merged:
                    k = s.lower()
                    if k in seen:
                        continue
                    seen.add(k)
                    out.append(s)
                lines = [f"{i}. {s}" for i, s in enumerate(out[:30], 1)]
                pname = program_name or "la carrera"
                return f"Materias de año {asked_year} de {pname}:\n" + "\n".join(lines) + (f"\n\nFuente: {src}" if src else "")
            if partial_merged:
                out: list[str] = []
                seen: set[str] = set()
                for s in partial_merged:
                    k = s.lower()
                    if k in seen:
                        continue
                    seen.add(k)
                    out.append(s)
                lines = [f"{i}. {s}" for i, s in enumerate(out[:20], 1)]
                pname = program_name or "la carrera"
                return (
                    f"No encuentro el plan anual completo de {pname} para año {asked_year} en páginas canónicas. "
                    "Solo hay un listado parcial (por ejemplo, de cuatrimestre) en esta fuente:\n"
                    + "\n".join(lines)
                    + (f"\n\nFuente: {partial_src}" if partial_src else "")
                )
        return None

    async def _llm_resolve_program(
        self,
        query: str,
        source_id: str,
        history: list[str] | None = None,
    ) -> str | None:
        """
        Ask the LLM to map the user's program reference (abbreviation, informal name,
        partial name) to the canonical program name available in this source.
        This replaces brittle regex-based abbreviation lists and works for any institution.
        """
        if not source_id:
            return None
        try:
            UUID(source_id)
        except ValueError:
            return None
        try:
            async with async_session() as session:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT DISTINCT program_name
                            FROM program_facts
                            WHERE source_id = CAST(:source_id AS uuid)
                              AND program_name IS NOT NULL
                              AND program_name != '__general__'
                            ORDER BY program_name
                            LIMIT 30
                            """
                        ),
                        {"source_id": source_id},
                    )
                ).scalars().all()
        except Exception:
            return None
        available = [str(r).strip() for r in rows if r and str(r).strip()]
        if not available:
            return None
        programs_str = "\n".join(f"- {p}" for p in available)
        recent = "\n".join((history or [])[-4:])
        history_section = f"\nConversación reciente:\n{recent}\n" if recent else ""
        prompt = (
            f"Programas académicos disponibles en esta institución:\n{programs_str}\n"
            f"{history_section}\n"
            f"Mensaje del usuario: \"{query}\"\n\n"
            "Identificá a qué programa se refiere el usuario. Puede usar el nombre completo, "
            "una abreviatura, un nombre informal o parte del nombre. "
            "Respondé ÚNICAMENTE con el nombre exacto del programa tal como aparece en la lista, "
            "o con la palabra 'ninguno' si no hay coincidencia clara."
        )
        try:
            res = await self.llm.ainvoke(prompt)
            resolved = (res.content or "").strip().strip("\"'").strip()
            if not resolved or resolved.lower() in {"ninguno", "none", "no", "n/a", "no sé"}:
                return None
            norm_resolved = self._normalize_program_for_lookup(resolved)
            # Exact match first
            for prog in available:
                if self._normalize_program_for_lookup(prog) == norm_resolved:
                    return prog
            # Substring match as fallback
            for prog in available:
                p_norm = self._normalize_program_for_lookup(prog)
                if norm_resolved and (norm_resolved in p_norm or p_norm in norm_resolved):
                    return prog
            # Return as-is if it looks plausible (LLM might have reformatted slightly)
            if len(resolved) > 4:
                return resolved
            return None
        except Exception:
            return None

    async def _answer_from_program_facts(
        self,
        source_id: str,
        query: str,
        history: list[str],
        session_state: dict | None = None,
    ) -> str | None:
        if not source_id:
            return None
        try:
            UUID(source_id)
        except ValueError:
            return None

        normalized_state = self._normalized_session_state(session_state)
        is_duration = self._is_duration_query(query)
        is_authority = self._is_authority_query(query)
        is_tramites = self._is_tramites_query(query)
        is_admissions = self._is_admissions_query(query)
        asks_subjects_terms = any(
            t in (query or "").lower()
            for t in ("materia", "materias", "asignatura", "asignaturas", "plan de estudios")
        )
        asked_year = self._extract_year_from_query(query)
        if asked_year is None and asks_subjects_terms and isinstance(normalized_state.get("active_year"), int):
            asked_year = int(normalized_state.get("active_year"))
        inferred_year = self._infer_year_from_history(history, query)
        history_subjects = self._history_points_to_subjects(history, query)
        is_subjects_followup = self._looks_like_subjects_followup(query) and inferred_year is not None
        if asked_year is None and (asks_subjects_terms or is_subjects_followup or history_subjects):
            asked_year = inferred_year
        is_year_subjects = (
            not is_admissions
            and not is_tramites
            and asked_year is not None
            and (asks_subjects_terms or is_subjects_followup or history_subjects)
        )
        is_program_count = self._is_program_count_query(query)
        is_programs_overview = self._is_programs_query(query) and not self._query_has_specific_program(query)
        profile_intent = self._extract_profile_intent(query)
        if not (
            is_program_count
            or is_programs_overview
            or is_duration
            or is_authority
            or is_year_subjects
            or profile_intent is not None
            or is_tramites
            or is_admissions
        ):
            return None

        source_uuid = str(UUID(source_id))
        normalized_query = self._normalize_query_typos(query)
        program_mentions = self._extract_program_mentions_from_text(normalized_query)
        inferred_program = normalized_state.get("active_program") or self._infer_program_from_history(history, query)
        program_name = program_mentions[0] if program_mentions else inferred_program
        # If the query clearly needs a specific program but we couldn't extract one via
        # regex (e.g. the user wrote "kinesio", "arq", "infor", or any informal name),
        # ask the LLM to resolve it — this works for any institution without hardcoding.
        if not program_name and (is_authority or is_duration or is_year_subjects):
            program_name = await self._llm_resolve_program(query, source_id, history)
        program_variants = self._program_lookup_variants(program_name or "")
        program_exact = program_variants[0] if program_variants else ""
        like_seed = ""
        for candidate in sorted(program_variants, key=len):
            if len(candidate) >= 5:
                like_seed = candidate
                break
        program_like = f"%{like_seed}%" if like_seed else ""

        async with async_session() as session:
            # Specific intents (duration/authority) must win over global program counting.
            if (is_program_count or is_programs_overview) and not is_duration and not is_authority:
                careers_stmt = text(
                    """
                    SELECT canonical_url, COALESCE(title, '') AS title, COALESCE(content, '') AS content
                    FROM documents
                    WHERE source_id = CAST(:source_id AS uuid)
                      AND (
                        canonical_url ILIKE '%/carreras/%'
                        OR canonical_url ILIKE '%/category/carreras%'
                        OR canonical_url ILIKE '%/carrera-de-%'
                        OR canonical_url ILIKE '%/oferta-academica/%'
                        OR page_type = 'career_canonical'
                      )
                    ORDER BY fetched_at DESC
                    LIMIT 600
                    """
                )
                career_rows = (
                    await session.execute(careers_stmt, {"source_id": source_uuid})
                ).mappings().all()
                career_names: list[str] = []
                seen_careers: set[str] = set()
                seen_program_keys: set[str] = set()
                for row in career_rows:
                    url = str(row.get("canonical_url") or "").strip()
                    title = str(row.get("title") or "").strip()
                    content = str(row.get("content") or "").strip()
                    block = f"URL: {url}\nTitulo: {title}\nContenido: {content[:8000]}"
                    # Prefer the URL slug when the URL contains /carreras/ — it is
                    # the most reliable source and avoids capturing noisy page titles
                    # like "Licenciatura en Enfermería Logró la Acreditación".
                    url_candidate = self._career_name_from_url(url)
                    if url_candidate:
                        candidates = [url_candidate]
                    else:
                        candidates = self._extract_program_names_from_text(f"{title}\n{content}")
                        if not candidates:
                            by_context, _ = self._extract_program_names_from_context([block])
                            candidates = by_context
                    for candidate in candidates:
                        candidate = self._sanitize_career_name(candidate)
                        if not self._is_plausible_career_name(candidate):
                            continue
                        key = self._normalize_name_key(candidate)
                        pkey = self._program_dedupe_key(candidate)
                        if key in seen_careers or (pkey and pkey in seen_program_keys):
                            continue
                        seen_careers.add(key)
                        if pkey:
                            seen_program_keys.add(pkey)
                        career_names.append(candidate)
                if career_names:
                    lines = [f"{i}. {n}" for i, n in enumerate(career_names[:20], 1)]
                    label = "Carreras detectadas" if self._wants_only_careers(query) else "Carreras/programas detectados"
                    return f"{label} en la facultad ({len(career_names)}):\n" + "\n".join(lines)

            if not program_variants and (is_authority or is_duration or is_year_subjects):
                return None

            if is_authority:
                if self._wants_dean(query):
                    return None
                fact_key = "secretary_academic" if self._wants_secretary(query) else "director"
                auth_stmt = text(
                    """
                    SELECT fact_value, canonical_url, confidence, fetched_at, program_name
                    FROM program_facts
                    WHERE source_id = CAST(:source_id AS uuid)
                      AND fact_key = :fact_key
                      AND (
                          translate(lower(program_name), 'áéíóúÁÉÍÓÚ', 'aeiouaeiou') = :program_exact
                          OR (:program_like <> '' AND translate(lower(program_name), 'áéíóúÁÉÍÓÚ', 'aeiouaeiou') LIKE :program_like)
                      )
                    ORDER BY fetched_at DESC
                    LIMIT 20
                    """
                )
                rows = (
                    await session.execute(
                        auth_stmt,
                        {
                            "source_id": source_uuid,
                            "fact_key": fact_key,
                            "program_exact": program_exact,
                            "program_like": program_like,
                        },
                    )
                ).mappings().all()
                canonical_rows = [
                    dict(r)
                    for r in rows
                    if (
                        self._is_canonical_program_url(str((dict(r)).get("canonical_url") or ""))
                        and self._is_plausible_authority_value(str((dict(r)).get("fact_value") or ""))
                    )
                ]
                fallback_rows = [
                    dict(r)
                    for r in rows
                    if (
                        not is_non_academic_noise(str((dict(r)).get("canonical_url") or ""), "", "")
                        and self._is_plausible_authority_value(str((dict(r)).get("fact_value") or ""))
                    )
                ]
                best = self._pick_best_fact_row(canonical_rows or fallback_rows)
                if best:
                    value = (best.get("fact_value") or "").strip()
                    pname = (best.get("program_name") or "").strip() or (program_name or "la carrera")
                    pname_norm = self._normalize_program_for_lookup(pname)
                    if program_variants:
                        strong_variants = [v for v in program_variants if len(v) >= 5]
                        if strong_variants and not any(
                            (v in pname_norm) or (pname_norm in v) for v in strong_variants
                        ):
                            return None
                    src = (best.get("canonical_url") or "").strip()
                    if value:
                        label = "secretario/a académico/a" if fact_key == "secretary_academic" else "director/a de carrera"
                        return f"El/la {label} de {pname} es {value}. Fuente: {src}"

            if is_duration:
                duration_stmt = text(
                    """
                    SELECT fact_value, canonical_url, confidence, fetched_at, program_name
                    FROM program_facts
                    WHERE source_id = CAST(:source_id AS uuid)
                      AND fact_key = 'duration'
                      AND (
                          translate(lower(program_name), 'áéíóúÁÉÍÓÚ', 'aeiouaeiou') = :program_exact
                          OR (:program_like <> '' AND translate(lower(program_name), 'áéíóúÁÉÍÓÚ', 'aeiouaeiou') LIKE :program_like)
                      )
                    ORDER BY fetched_at DESC
                    LIMIT 20
                    """
                )
                rows = (
                    await session.execute(
                        duration_stmt,
                        {
                            "source_id": source_uuid,
                            "program_exact": program_exact,
                            "program_like": program_like,
                        },
                    )
                ).mappings().all()
                filtered_rows: list[dict] = []
                for row in rows:
                    rr = dict(row)
                    src = str(rr.get("canonical_url") or "").strip()
                    val = str(rr.get("fact_value") or "").strip()
                    if is_non_academic_noise(src, "", ""):
                        continue
                    if not self._is_plausible_duration_fact(val):
                        continue
                    filtered_rows.append(rr)
                best = self._pick_best_fact_row(filtered_rows)
                if best:
                    value = (best.get("fact_value") or "").strip()
                    pname = (best.get("program_name") or "").strip() or (program_name or "la carrera")
                    src = (best.get("canonical_url") or "").strip()
                    if value:
                        return f"La duración de {pname} es {value}. Fuente: {src}"

            if is_year_subjects and asked_year is not None:
                year_fact_key = f"year_{asked_year}_subject"
                subjects_stmt = text(
                    """
                    SELECT fact_value, canonical_url, confidence, fetched_at, program_name
                    FROM program_facts
                    WHERE source_id = CAST(:source_id AS uuid)
                      AND fact_key = :year_fact_key
                      AND (
                          translate(lower(program_name), 'áéíóúÁÉÍÓÚ', 'aeiouaeiou') = :program_exact
                          OR (:program_like <> '' AND translate(lower(program_name), 'áéíóúÁÉÍÓÚ', 'aeiouaeiou') LIKE :program_like)
                      )
                    ORDER BY confidence DESC, fetched_at DESC
                    LIMIT 80
                    """
                )
                rows = (
                    await session.execute(
                        subjects_stmt,
                        {
                            "source_id": source_uuid,
                            "year_fact_key": year_fact_key,
                            "program_exact": program_exact,
                            "program_like": program_like,
                        },
                    )
                ).mappings().all()
                if rows:
                    deduped: list[dict] = []
                    seen_values: set[str] = set()
                    best_source_by_score: tuple[int, str] | None = None
                    for row in rows:
                        src = str(row.get("canonical_url") or "").strip()
                        if is_non_academic_noise(src, "", ""):
                            continue
                        src_score = self._fact_source_quality_score(src, year_fact_key)
                        if src_score < -10:
                            continue
                        v = re.sub(r"\s+", " ", str(row.get("fact_value") or "")).strip()
                        if not v:
                            continue
                        if not self._is_plausible_subject_name(v):
                            continue
                        key = v.lower()
                        if key in seen_values:
                            continue
                        seen_values.add(key)
                        deduped.append(dict(row) | {"fact_value": v, "_src_score": src_score})
                        if best_source_by_score is None or src_score > best_source_by_score[0]:
                            best_source_by_score = (src_score, src)
                    if deduped:
                        deduped.sort(
                            key=lambda r: (
                                int(r.get("_src_score") or 0),
                                float(r.get("confidence") or 0.0),
                                str(r.get("fetched_at") or ""),
                            ),
                            reverse=True,
                        )
                        pname = (
                            str(deduped[0].get("program_name") or "").strip()
                            or (program_name or "la carrera")
                        )
                        src = (
                            best_source_by_score[1]
                            if best_source_by_score is not None
                            else str(deduped[0].get("canonical_url") or "").strip()
                        )
                        lines = [f"{idx}. {str(r.get('fact_value') or '').strip()}" for idx, r in enumerate(deduped[:20], 1)]
                        return (
                            f"Materias de año {asked_year} de {pname}:\n"
                            + "\n".join(lines)
                            + f"\n\nFuente: {src}"
                        )

            if profile_intent is not None or is_tramites or is_admissions:
                docs_stmt = text(
                    """
                    SELECT canonical_url, COALESCE(title, '') AS title, COALESCE(content, '') AS content, fetched_at
                    FROM documents
                    WHERE source_id = CAST(:source_id AS uuid)
                      AND COALESCE(content, '') <> ''
                      AND canonical_url NOT ILIKE '%/noticia/%'
                      AND canonical_url NOT ILIKE '%/noticias/%'
                      AND canonical_url NOT ILIKE '%/novedad/%'
                      AND canonical_url NOT ILIKE '%/novedades/%'
                      AND canonical_url NOT ILIKE '%/wp-content/uploads/%'
                      AND canonical_url NOT ILIKE '%.pdf%'
                    ORDER BY fetched_at DESC
                    LIMIT 400
                    """
                )
                doc_rows = (await session.execute(docs_stmt, {"source_id": source_uuid})).mappings().all()
                if doc_rows:
                    query_tokens = self._extract_query_tokens(query)
                    program_tokens = [v for v in program_variants if len(v) >= 5]
                    scored: list[tuple[int, dict]] = []
                    for row in doc_rows:
                        rr = dict(row)
                        url = str(rr.get("canonical_url") or "").strip()
                        title = str(rr.get("title") or "").strip()
                        content = str(rr.get("content") or "").strip()
                        if not url or not content:
                            continue
                        low = f"{url.lower()} {title.lower()} {content[:2500].lower()}"
                        score = 0
                        if is_tramites:
                            if any(t in low for t in ("tramite", "trámite", "diploma", "titulo", "título", "egreso")):
                                score += 32
                        if is_admissions:
                            if any(t in low for t in ("ingreso", "ingresante", "admis", "inscrip", "requisito", "documentación", "documentacion")):
                                score += 28
                        if profile_intent is not None and profile_intent in low:
                            score += 18
                        if "/comunidad/estudiantes/" in url.lower():
                            score += 18
                        if "/carreras/" in url.lower():
                            score += 14
                        if "/admin-contenidos-are/" in url.lower():
                            score -= 18
                        if program_tokens:
                            if any(tok in low for tok in program_tokens):
                                score += 20
                            elif is_admissions:
                                score -= 12
                        score += sum(2 for tok in query_tokens if tok in low)
                        if score > 0:
                            scored.append((score, rr))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    if scored:
                        top = scored[0][1]
                        url = str(top.get("canonical_url") or "").strip()
                        title = str(top.get("title") or "").strip()
                        content = str(top.get("content") or "").strip()
                        excerpt = self._excerpt_for_query(content, query)
                        if is_tramites:
                            return (
                                "Para tramitarlo, revisá esta página oficial y seguí los requisitos indicados ahí:\n"
                                f"- {title or 'Trámite de diplomas'}\n"
                                f"- {url}\n\n"
                                f"Resumen útil: {excerpt}"
                            )
                        if is_admissions:
                            target_year = self._extract_target_year_from_query(query)
                            key_lines = self._extract_admissions_key_lines(
                                content,
                                target_year=target_year,
                            )
                            if key_lines:
                                bullets = "\n".join(f"- {ln}" for ln in key_lines[:5])
                                return (
                                    "Para inscribirte, esto figura en la web oficial:\n"
                                    f"- {title or 'Ingreso/Inscripción'}\n"
                                    f"- {url}\n\n"
                                    f"Datos clave:\n{bullets}"
                                )
                            return (
                                "Para inscribirte, esta es la fuente más relevante que encontré:\n"
                                f"- {title or 'Ingreso/Inscripción'}\n"
                                f"- {url}\n\n"
                                f"Resumen útil: {excerpt}"
                            )
                        if profile_intent is not None:
                            lines: list[str] = []
                            for idx, (_, row) in enumerate(scored[:5], 1):
                                rtitle = str((row.get("title") or "")).strip() or "Página informativa"
                                rurl = str((row.get("canonical_url") or "")).strip()
                                lines.append(f"{idx}. {rtitle}: {rurl}")
                            return f"Páginas relevantes para {profile_intent}:\n" + "\n".join(lines)
        return None

    async def _discover_candidate_urls(self, source_url: str, query: str, limit: int = 10) -> list[str]:
        parsed_source = urlparse(source_url)
        base_host = parsed_source.netloc.lower()
        tokens = self._extract_query_tokens(query)
        seeded_urls = self._seed_candidate_urls(source_url, query)

        scored_links: list[tuple[int, str]] = []
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                for seed in seeded_urls[:4]:
                    try:
                        resp = await client.get(seed)
                    except Exception:  # noqa: BLE001
                        continue
                    if resp.status_code >= 400:
                        continue
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for anchor in soup.select("a[href]"):
                        href = (anchor.get("href") or "").strip()
                        if not href:
                            continue
                        abs_url = urljoin(seed, href)
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
                                "duracion",
                                "duración",
                                "director",
                                "coordinador",
                                "secretario",
                            )
                        ):
                            score += 3
                        if score > 0:
                            scored_links.append((score, abs_url))
        except Exception:  # noqa: BLE001
            return seeded_urls[: max(1, limit)]

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

    async def _retrieve_from_source(self, source_url: str, query: str, *, force: bool = False) -> list[str]:
        if not self.ENABLE_RUNTIME_SCRAPE and not force:
            return []
        if not self._is_valid_https_source(source_url):
            return []
        candidate_urls = await self._discover_candidate_urls(
            source_url,
            query,
            limit=self.FALLBACK_DISCOVERY_LIMIT,
        )
        contexts: list[str] = []
        for url in candidate_urls:
            try:
                scrape_result = await self.scraper.scrape_page(url)
            except Exception:  # noqa: BLE001
                continue
            if not scrape_result.success:
                continue
            title = scrape_result.title
            content = scrape_result.markdown
            title = (title or "").strip()
            content = (content or "").strip()
            if len(content.split()) < 8:
                continue
            if is_institutional_news(url, title, content):
                continue
            if is_non_academic_noise(url, title, content):
                continue
            excerpt = self._excerpt_for_query(content, query)
            contexts.append(f"URL: {url}\nTitulo: {title}\nContenido: {excerpt}")
            if len(contexts) >= self.FALLBACK_CONTEXT_LIMIT:
                break
        return contexts

    async def _retrieve_program_page_context(self, source_url: str, query: str, *, force: bool = False, authority_only: bool = False) -> list[str]:
        if not self.ENABLE_RUNTIME_SCRAPE and not force:
            return []
        if not self._is_valid_https_source(source_url):
            return []
        programs = self._extract_program_mentions_from_text(query)
        if not programs:
            return []
        parsed = urlparse(source_url)
        base = f"https://{parsed.netloc}/"
        slugs = self._slug_candidates_for_program(programs[0])
        if not slugs:
            return []
        candidates: list[str] = []
        for slug in slugs:
            career_paths = [
                urljoin(base, f"carreras/{slug}/"),
                urljoin(base, f"carreras/{slug}"),
            ]
            # For authority/director queries, only try canonical /carreras/ paths.
            # /ofertas-acad/ pages are specific courses/seminars — their coordinators
            # are NOT career directors.
            extra_paths = [] if authority_only else [
                urljoin(base, f"ofertas-acad/{slug}"),
                urljoin(base, f"oferta-academica/{slug}"),
            ]
            candidates.extend(career_paths + extra_paths)
        out: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                scrape_result = await self.scraper.scrape_page(candidate)
            except Exception:  # noqa: BLE001
                continue
            if not scrape_result.success:
                continue
            title = scrape_result.title
            content = scrape_result.markdown
            content = (content or "").strip()
            if len(content.split()) < 8:
                continue
            if is_institutional_news(candidate, title or "", content):
                continue
            if is_non_academic_noise(candidate, title or "", content):
                continue
            excerpt = self._excerpt_for_query(content, query)
            out.append(f"URL: {candidate}\nTitulo: {(title or '').strip()}\nContenido: {excerpt}")
            if len(out) >= 3:
                break
        return out

    async def _fallback_answer_from_live_source(
        self,
        *,
        source_id: str,
        query: str,
        history: list[str],
        session_state: dict | None,
    ) -> str | None:
        # Keep response latency predictable for admin/tramite flows:
        # these should come from indexed documents, not on-demand crawling.
        if self._is_admissions_query(query) or self._is_tramites_query(query):
            return None
        if not source_id:
            return None
        try:
            UUID(source_id)
        except ValueError:
            return None

        scope = await self._resolve_source_scope(source_id)
        if not scope:
            return None
        _, _, source_url = scope
        if not self._is_valid_https_source(source_url):
            return None

        active_program = self._program_for_followup(
            self._normalized_session_state(session_state),
            history,
            query,
        )
        resolved_query = (query or "").strip()
        if active_program and not self._query_has_specific_program(resolved_query):
            resolved_query = f"{resolved_query} {active_program}".strip()

        is_authority = self._is_authority_query(resolved_query)
        contexts: list[str] = []
        program_contexts = await self._retrieve_program_page_context(
            source_url, resolved_query, force=True, authority_only=is_authority
        )
        # For authority/director queries skip broad site discovery: it finds course/seminar
        # pages (/ofertas-acad/) whose coordinators are NOT career directors.
        source_contexts: list[str] = []
        if not is_authority:
            source_contexts = await self._retrieve_from_source(
                source_url, resolved_query, force=True
            )
        if program_contexts:
            contexts.extend(program_contexts)
        if source_contexts:
            contexts.extend(source_contexts)
        if not contexts:
            return None

        ranked_contexts = self._rank_context_blocks(contexts, resolved_query)[:10]
        extracted = self._extract_answer_from_context(resolved_query, ranked_contexts)
        if extracted:
            return extracted

        context = "\n\n---\n\n".join(ranked_contexts[:6])
        history_text = "\n".join(self._history_for_prompt(history)).strip()
        prompt = (
            f"{SYSTEM_RAG}\n\n"
            "Usa SOLO el contexto recuperado y no inventes datos.\n"
            "Si no hay evidencia exacta en el contexto, responde EXACTAMENTE: "
            "\"No tengo información referente a eso.\".\n\n"
            f"Historial reciente:\n{history_text}\n\n"
            f"Contexto recuperado:\n{context}\n\n"
            f"Pregunta del usuario:\n{resolved_query}"
        )
        try:
            res = await self.llm.ainvoke(prompt)
            text_value = str(getattr(res, "content", "") or "").strip()
            return text_value or self.NO_INFO_RESPONSE
        except Exception:  # noqa: BLE001
            return None

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
        authority_query = RAGService._is_authority_query(query)
        scored: list[tuple[int, str]] = []
        for block in contexts:
            haystack = (block or "").lower()
            score = sum(2 for tok in tokens if tok in haystack)
            if programs_query:
                if any(
                    token in haystack
                    for token in (
                        "/carreras/",
                        "/category/carreras",
                        "/oferta-academica",
                        "/programas/",
                        "oferta académica",
                    )
                ):
                    score += 8
                if any(t in haystack for t in ("/cargos-profesorales", "cargos profesorales")):
                    score -= 30
                if "/wp-content/uploads/" in haystack:
                    score -= 14
                if any(t in haystack for t in ("programa curso", "simposio", "jornada", "congreso")):
                    score -= 18
                if RAGService._is_program_noise(haystack):
                    score -= 10
            if "director" in query.lower() and "director" in haystack:
                score += 5
            if any(t in query.lower() for t in ("duracion", "duración", "años", "anios")) and (
                "duración" in haystack or "duracion" in haystack or "años" in haystack
            ):
                score += 5
            if authority_query:
                src = RAGService._extract_url_from_block(block)
                score += RAGService._authority_url_score(src)
                fetched = RAGService._extract_fetched_at_from_block(block)
                if fetched is not None:
                    age_days = max(0, int((datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)).total_seconds() // 86400))
                    if age_days <= 30:
                        score += 16
                    elif age_days <= 90:
                        score += 10
                    elif age_days <= 180:
                        score += 6
                    elif age_days > 365:
                        score -= 8
                if RAGService._is_event_like_old_block(block):
                    score -= 22
            scored.append((score, block))
        ranked = sorted(scored, key=lambda item: item[0], reverse=True)
        return [block for _, block in ranked]

    @staticmethod
    def _extract_answer_from_context(query: str, context_blocks: list[str]) -> str | None:
        q = (query or "").lower()
        if not context_blocks:
            return None

        if (
            RAGService._is_programs_query(q)
            and not RAGService._is_authority_query(q)
            and not RAGService._is_duration_query(q)
            and not RAGService._is_year_subjects_query(q)
            and not RAGService._is_admissions_query(q)
            and not RAGService._is_tramites_query(q)
        ):
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

        if RAGService._is_first_year_subjects_query(q):
            for block in context_blocks:
                low = (block or "").lower()
                if "primer año" not in low and "primer anio" not in low:
                    continue
                section_match = re.search(
                    r"(primer\s+a[nñ]o[\s\S]{0,8000}?)(?:\n\s*(?:segundo|2do|2º|2°)\s+a[nñ]o\b|$)",
                    block,
                    flags=re.IGNORECASE,
                )
                section = section_match.group(1) if section_match else block
                subjects = re.findall(
                    r"(?:^|\n)\s*materia\s*:\s*([^\n]{3,120})",
                    section,
                    flags=re.IGNORECASE,
                )
                seen: set[str] = set()
                cleaned: list[str] = []
                for s in subjects:
                    v = re.sub(r"\s+", " ", (s or "").strip(" .:-\t"))
                    if not v:
                        continue
                    if not RAGService._is_plausible_subject_name(v):
                        continue
                    key = v.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    cleaned.append(v)
                if cleaned:
                    src = RAGService._extract_url_from_block(block)
                    lines = [f"{idx}. {name}" for idx, name in enumerate(cleaned[:20], 1)]
                    return (
                        "Materias de primer año:\n"
                        + "\n".join(lines)
                        + f"\n\nFuente: {src or 'contexto recuperado'}"
                    )

        if RAGService._is_admissions_query(q):
            target_year = RAGService._extract_target_year_from_query(q)
            best: tuple[int, str, list[str]] | None = None
            for block in context_blocks:
                src = RAGService._extract_url_from_block(block)
                if not src:
                    continue
                low_src = src.lower()
                if any(t in low_src for t in ("/diploma", "/diplomas", "/egresad", "/graduad")):
                    continue
                score = 0
                if any(t in low_src for t in ("/inscrip", "/ingreso", "/ingresantes", "/admision", "/admis")):
                    score += 35
                if "/comunidad/estudiantes/" in low_src:
                    score += 20
                lines = RAGService._extract_admissions_key_lines(block, target_year=target_year)
                if not lines:
                    continue
                score += min(20, len(lines) * 4)
                if best is None or score > best[0]:
                    best = (score, src, lines)
            if best:
                bullets = "\n".join(f"- {ln}" for ln in best[2][:5])
                return (
                    "Para inscripciones, esto figura en la fuente oficial:\n"
                    f"- {best[1]}\n\n"
                    f"Datos clave:\n{bullets}"
                )

        if RAGService._is_authority_query(q):
            candidates: list[tuple[int, str, str]] = []
            wants_vice_dean = RAGService._wants_vice_dean(q)
            wants_dean_only = RAGService._wants_dean_only(q)
            for block in context_blocks:
                src = RAGService._extract_url_from_block(block)
                low_src = (src or "").lower()
                if wants_vice_dean:
                    direct_patterns = (
                        r"(?:vice\s*decano(?:a)?|vicedecano(?:a)?)\s*[:\-]\s*([^\n|]+)",
                        r"(?:^|\n)\s*#{1,4}\s*(?:vice\s*decano(?:a)?|vicedecano(?:a)?)\s*\n+\s*([^\n]+)",
                    )
                elif wants_dean_only:
                    direct_patterns = (
                        r"(?:(?<!vice\s)(?<!vice)decano(?:a)?)\s*[:\-]\s*([^\n|]+)",
                        r"(?:^|\n)\s*#{1,4}\s*(?:(?<!vice\s)(?<!vice)decano(?:a)?)\s*\n+\s*([^\n]+)",
                    )
                else:
                    direct_patterns = (
                        r"(?:director(?:a)?\s+de\s+carrera|direcci[oó]n\s+de\s+carrera|coordinador(?:a)?(?:\s+de\s+carrera)?|responsable\s+de\s+carrera)\s*[:\-]\s*([^\n|]+)",
                        r"(?:director(?:a)?|coordinador(?:a)?|responsable(?:\s+acad[eé]mic[oa])?|jef(?:e|a)\s+de\s+carrera)\s*[:\-]\s*([^\n|]+)",
                        r"(?:director(?:a)?\s+de\s+(?:la\s+)?carrera(?:\s+de)?[^\n:|]{0,90}?)\s+(?:es\s+)?([A-ZÁÉÍÓÚÑ][^\n|]{2,120})",
                        r"(?:secretario(?:a)?\s+acad[eé]mic[oa])\s*[:\-]\s*([^\n|]+)",
                        r"(?:decano(?:a)?|vicedecano(?:a)?)\s*[:\-]\s*([^\n|]+)",
                        r"\|\s*(?:director(?:a)?\s+de\s+carrera|direcci[oó]n\s+de\s+carrera|coordinador(?:a)?|responsable)\s*\|\s*([^\|\n]+)\|",
                        r"\|\s*(?:secretario(?:a)?\s+acad[eé]mic[oa])\s*\|\s*([^\|\n]+)\|",
                        r"(?:^|\n)\s*#{1,4}\s*(?:director(?:a)?|coordinador(?:a)?|direcci[oó]n)\s*(?:de\s+carrera)?\s*\n+\s*([^\n]+)",
                        r"(?:^|\n)\s*#{1,6}\s*(?:director(?:a)?|direcci[oó]n|coordinador(?:a)?|responsable)[^\n]*\n+\s*([^\n]+)",
                        r"(?:^|\n)\s*#{1,4}\s*(?:secretario(?:a)?\s+acad[eé]mic[oa])\s*\n+\s*([^\n]+)",
                        r"(?:^|\n)\s*#{1,4}\s*(?:decano(?:a)?|vicedecano(?:a)?)\s*\n+\s*([^\n]+)",
                    )
                value = ""
                for pattern in direct_patterns:
                    match = re.search(pattern, block, flags=re.IGNORECASE)
                    if match:
                        value = (match.group(1) or "").strip(" .:-*_`")
                        if value:
                            break
                if value:
                    if not RAGService._is_plausible_authority_value(value):
                        value = ""
                if value:
                    score = RAGService._authority_url_score(src)
                    fetched = RAGService._extract_fetched_at_from_block(block)
                    if fetched is not None:
                        age_days = max(0, int((datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)).total_seconds() // 86400))
                        if age_days <= 30:
                            score += 12
                        elif age_days <= 90:
                            score += 8
                        elif age_days > 365:
                            score -= 6
                    if RAGService._is_event_like_old_block(block):
                        score -= 20
                    candidates.append((score, value, src))
                match = re.search(
                    r"director\s+de\s+carrera[^\n]*\n+\s*#{1,4}\s*([^\n]+)",
                    block,
                    flags=re.IGNORECASE,
                )
                if match:
                    value = match.group(1).strip()
                    if not RAGService._is_plausible_authority_value(value):
                        value = ""
                    if value:
                        src = RAGService._extract_url_from_block(block)
                        score = RAGService._authority_url_score(src)
                        candidates.append((score, value, src))
                if not value:
                    src_low = (src or "").lower()
                    if re.search(r"/carreras/[^/]+/?$", src_low):
                        titled = RAGService._extract_titled_authority_candidates(block)
                        if titled:
                            score = RAGService._authority_url_score(src) + 4
                            candidates.append((score, titled[0], src))
            if candidates:
                aggregated: dict[str, dict] = {}
                for score, value, src in candidates:
                    key = re.sub(r"\s+", " ", (value or "").strip().lower())
                    if not key:
                        continue
                    if key not in aggregated:
                        aggregated[key] = {
                            "score_sum": 0,
                            "count": 0,
                            "best_score": -10**9,
                            "value": value,
                            "src": src,
                        }
                    bucket = aggregated[key]
                    bucket["score_sum"] += int(score)
                    bucket["count"] += 1
                    if int(score) > int(bucket["best_score"]):
                        bucket["best_score"] = int(score)
                        bucket["value"] = value
                        bucket["src"] = src
                ranked = sorted(
                    aggregated.values(),
                    key=lambda item: (item["score_sum"] + item["count"] * 10, item["best_score"]),
                    reverse=True,
                )
                if not ranked:
                    return None
                best = ranked[0]
                label = (
                    "vicedecano/a"
                    if RAGService._wants_vice_dean(q)
                    else (
                        "decano/a"
                        if RAGService._wants_dean(q)
                        else ("secretario/a académico/a" if RAGService._wants_secretary(q) else "director/a de carrera")
                    )
                )
                return (
                    f"El/la {label} es {best['value']}. "
                    f"Fuente: {best['src'] or 'contexto recuperado'}"
                )
        return None

    async def retrieve(self, state: AgentState):
        query = (state.get("query") or "").strip()
        query = self._normalize_query_for_intent(query)
        history = state.get("history") or []
        session_state = self._normalized_session_state(state.get("session_state"))
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
        active_program = self._program_for_followup(session_state, history, query)
        active_year = session_state.get("active_year")
        active_intent = (session_state.get("active_intent") or "").strip()
        if self._looks_like_program_reply(query):
            prior_intent_query = self._infer_intent_query_from_history(history)
            current_programs = self._extract_program_mentions_from_text(query)
            if prior_intent_query and current_programs:
                resolved_query = f"{prior_intent_query} {current_programs[0]}"
        if self._needs_program_clarification(query):
            inferred_program = self._infer_program_from_history(history, query)
            if inferred_program:
                resolved_query = f"{query} {inferred_program}"
        if not self._query_has_specific_program(query):
            inferred_program = self._infer_program_from_history(history, query)
            followup_like = (
                self._extract_year_from_query(query) is not None
                or self._looks_like_subjects_followup(query)
                or self._is_authority_query(query)
                or self._is_duration_query(query)
                or bool(re.search(r"\by\s+de(?:l|la)?\b", (query or "").lower()))
            )
            state_program = active_program if active_program else inferred_program
            if state_program and followup_like:
                resolved_query = f"{resolved_query} {state_program}".strip()
        if active_year is not None and self._extract_year_from_query(query) is None:
            if (
                not self._is_admissions_query(query)
                and not self._is_tramites_query(query)
                and not self._is_authority_query(query)
                and (
                    active_intent == "subjects"
                    or self._looks_like_subjects_followup(query)
                    or bool(re.search(r"\by\s+de(?:l|la)?\b", (query or "").lower()))
                )
            ):
                resolved_query = f"{resolved_query} año {active_year}".strip()

        structured_intent = self._intent_from_query(query)
        if structured_intent in {
            "authority",
            "duration",
            "workload",
            "subjects",
            "admissions",
            "tramites",
            "program_count",
            "programs_overview",
        }:
            requires_program = structured_intent in {"authority", "duration", "workload", "subjects"}
            has_program_ref = bool(active_program) or self._query_has_specific_program(query)
            if (not requires_program) or has_program_ref:
                # Fast path: these intents are resolved from structured facts or direct DB lookups in generate().
                return {"context": []}

        relation = await self._resolve_embeddings_relation()
        lexical_queries = self._limit_lexical_queries(
            self._expand_lexical_queries(resolved_query),
            resolved_query,
        )
        vector_stmt = None
        lexical_stmt = None
        url_hint_stmt = None
        authority_stmt = None
        if relation:
            embeddings_table, text_col, seq_col = relation
            vector_stmt = text(
                f"""
                SELECT d.canonical_url AS url, COALESCE(d.title, '') AS title, de.{text_col} AS chunk_text, d.fetched_at AS fetched_at
                FROM {embeddings_table} de
                JOIN documents d ON d.doc_id = de.doc_id
                JOIN sources s ON s.source_id = d.source_id
                WHERE de.embedding IS NOT NULL
                  AND (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
                  AND COALESCE(d.page_type, 'institutional_info') <> 'news_blocked'
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
                ORDER BY de.embedding <=> ai.openai_embed('text-embedding-3-large', :resolved_query, dimensions => 1536)
                LIMIT :k
                """
            )
            lexical_stmt = text(
                f"""
                SELECT d.canonical_url AS url, COALESCE(d.title, '') AS title, de.{text_col} AS chunk_text, d.fetched_at AS fetched_at
                FROM {embeddings_table} de
                JOIN documents d ON d.doc_id = de.doc_id
                JOIN sources s ON s.source_id = d.source_id
                WHERE to_tsvector('spanish', COALESCE(d.title, '') || ' ' || de.{text_col})
                      @@ websearch_to_tsquery('spanish', :q)
                  AND (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
                  AND COALESCE(d.page_type, 'institutional_info') <> 'news_blocked'
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
                    to_tsvector('spanish', COALESCE(d.title, '') || ' ' || de.{text_col}),
                    websearch_to_tsquery('spanish', :q)
                ) DESC
                LIMIT :k
                """
            )
            url_hint_stmt = text(
                f"""
                SELECT d.canonical_url AS url, COALESCE(d.title, '') AS title, de.{text_col} AS chunk_text, d.fetched_at AS fetched_at
                FROM {embeddings_table} de
                JOIN documents d ON d.doc_id = de.doc_id
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
                    OR d.title ILIKE '%requisito%'
                )
                  AND (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
                  AND COALESCE(d.page_type, 'institutional_info') <> 'news_blocked'
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
            authority_stmt = text(
                f"""
                SELECT d.canonical_url AS url, COALESCE(d.title, '') AS title, de.{text_col} AS chunk_text, d.fetched_at AS fetched_at
                FROM {embeddings_table} de
                JOIN documents d ON d.doc_id = de.doc_id
                JOIN sources s ON s.source_id = d.source_id
                WHERE (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
                  AND COALESCE(d.page_type, 'institutional_info') <> 'news_blocked'
                  AND (
                      d.canonical_url ILIKE '%/carreras/%'
                      OR d.title ILIKE '%director%'
                      OR d.title ILIKE '%coordinador%'
                      OR d.title ILIKE '%secretari%'
                      OR de.{text_col} ILIKE '%director de carrera%'
                      OR de.{text_col} ILIKE '%coordinador%'
                      OR de.{text_col} ILIKE '%responsable de carrera%'
                      OR de.{text_col} ILIKE '%secretario academico%'
                      OR de.{text_col} ILIKE '%secretario académico%'
                      OR de.{text_col} ILIKE '%secretaria academica%'
                      OR to_tsvector('spanish', COALESCE(d.title, '') || ' ' || de.{text_col})
                         @@ websearch_to_tsquery('spanish', :q)
                  )
                ORDER BY
                  CASE
                    WHEN d.canonical_url ILIKE '%/carreras/%' THEN 0
                    WHEN d.canonical_url ILIKE '%/oferta-academica/%' THEN 1
                    ELSE 2
                  END,
                  d.fetched_at DESC
                LIMIT :k
                """
            )

        if relation:
            docs_lexical_stmt = text(
                f"""
                SELECT
                  d.canonical_url AS url,
                  COALESCE(d.title, '') AS title,
                  string_agg(de.{text_col}, E'\n' ORDER BY de.{seq_col}) AS full_text,
                  d.fetched_at AS fetched_at
                FROM {embeddings_table} de
                JOIN documents d ON d.doc_id = de.doc_id
                JOIN sources s ON s.source_id = d.source_id
                WHERE to_tsvector('spanish', COALESCE(d.title, '') || ' ' || de.{text_col})
                    @@ websearch_to_tsquery('spanish', :q)
                  AND (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
                  AND COALESCE(d.page_type, 'institutional_info') <> 'news_blocked'
                  AND d.canonical_url NOT ILIKE '%/noticia/%'
                  AND d.canonical_url NOT ILIKE '%/noticias/%'
                  AND d.canonical_url NOT ILIKE '%/novedad/%'
                  AND d.canonical_url NOT ILIKE '%/novedades/%'
                  AND d.canonical_url NOT ILIKE '%/actualidad/%'
                  AND d.canonical_url NOT ILIKE '%/prensa/%'
                  AND d.canonical_url NOT ILIKE '%/comunicado/%'
                  AND d.canonical_url NOT ILIKE '%/evento/%'
                  AND d.canonical_url NOT ILIKE '%/agenda/%'
                  AND d.canonical_url NOT ILIKE '%/wp-content/uploads/%'
                  AND d.canonical_url NOT ILIKE '%.pdf%'
                GROUP BY d.doc_id
                ORDER BY d.fetched_at DESC
                LIMIT :k
                """
            )
        else:
            docs_lexical_stmt = text(
                """
                SELECT
                  d.canonical_url AS url,
                  COALESCE(d.title, '') AS title,
                  COALESCE(d.title, '') AS full_text,
                  d.fetched_at AS fetched_at
                FROM documents d
                JOIN sources s ON s.source_id = d.source_id
                WHERE to_tsvector('spanish', COALESCE(d.title, ''))
                    @@ websearch_to_tsquery('spanish', :q)
                  AND (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
                  AND COALESCE(d.page_type, 'institutional_info') <> 'news_blocked'
                  AND d.canonical_url NOT ILIKE '%/noticia/%'
                  AND d.canonical_url NOT ILIKE '%/noticias/%'
                  AND d.canonical_url NOT ILIKE '%/novedad/%'
                  AND d.canonical_url NOT ILIKE '%/novedades/%'
                  AND d.canonical_url NOT ILIKE '%/actualidad/%'
                  AND d.canonical_url NOT ILIKE '%/prensa/%'
                  AND d.canonical_url NOT ILIKE '%/comunicado/%'
                  AND d.canonical_url NOT ILIKE '%/evento/%'
                  AND d.canonical_url NOT ILIKE '%/agenda/%'
                  AND d.canonical_url NOT ILIKE '%/wp-content/uploads/%'
                  AND d.canonical_url NOT ILIKE '%.pdf%'
                ORDER BY d.fetched_at DESC
                LIMIT :k
                """
            )

        contexts: list[str] = []
        seen: set[tuple[str, str]] = set()
        authority_query = self._is_authority_query(resolved_query)

        async with async_session() as session:
            vector_rows = []
            if vector_stmt is not None:
                vector_rows = (
                    await session.execute(
                        vector_stmt,
                        {
                            "resolved_query": resolved_query,
                            "k": self.VECTOR_K,
                            "domain_1": domain_1,
                            "domain_2": domain_2,
                        },
                    )
                ).mappings().all()

            lexical_rows = []
            if lexical_stmt is not None:
                for lq in lexical_queries:
                    rows = (
                        await session.execute(
                            lexical_stmt,
                            {
                                "q": lq,
                                "k": self.LEXICAL_K,
                                "domain_1": domain_1,
                                "domain_2": domain_2,
                            },
                        )
                    ).mappings().all()
                    lexical_rows.extend(rows)

            hinted_rows = []
            if self._needs_url_hints(resolved_query) and url_hint_stmt is not None:
                hinted_rows = (
                    await session.execute(
                        url_hint_stmt,
                        {"k": self.URL_HINT_K, "domain_1": domain_1, "domain_2": domain_2},
                    )
                ).mappings().all()

            retry_rows = []
            if not lexical_rows and not hinted_rows and lexical_stmt is not None:
                for rq in self._build_retry_queries(resolved_query, history):
                    rows = (
                        await session.execute(
                            lexical_stmt,
                            {
                                "q": rq,
                                "k": self.LEXICAL_K,
                                "domain_1": domain_1,
                                "domain_2": domain_2,
                            },
                        )
                    ).mappings().all()
                    retry_rows.extend(rows)

            authority_rows = []
            if authority_query and authority_stmt is not None:
                authority_rows = (
                    await session.execute(
                        authority_stmt,
                        {
                            "q": resolved_query,
                            "k": self.AUTHORITY_K,
                            "domain_1": domain_1,
                            "domain_2": domain_2,
                        },
                    )
                ).mappings().all()

            docs_rows = []
            for lq in lexical_queries[:2]:
                rows = (
                    await session.execute(
                        docs_lexical_stmt,
                        {
                            "q": lq,
                            "k": max(10, self.LEXICAL_K // 2),
                            "domain_1": domain_1,
                            "domain_2": domain_2,
                        },
                    )
                ).mappings().all()
                docs_rows.extend(rows)

        max_contexts = self.AUTHORITY_CONTEXTS if authority_query else self.NON_AUTH_CONTEXTS
        for row in [*authority_rows, *lexical_rows, *hinted_rows, *vector_rows, *retry_rows]:
            url = (row.get("url") or "").strip()
            title = (row.get("title") or "").strip()
            chunk_text = (row.get("chunk_text") or "").strip()
            fetched_at = row.get("fetched_at")
            if not url or not chunk_text:
                continue
            low_url = url.lower()
            if "/wp-content/uploads/" in low_url or ".pdf" in low_url:
                continue
            if is_non_academic_noise(url, title, chunk_text):
                continue
            key = (url, chunk_text[:180])
            if key in seen:
                continue
            seen.add(key)
            fetched_str = ""
            if fetched_at is not None:
                fetched_str = str(fetched_at)
            contexts.append(
                f"URL: {url}\nTitulo: {title}\nFetchedAt: {fetched_str}\nContenido: {chunk_text}"
            )
            if len(contexts) >= max_contexts:
                break

        for row in docs_rows:
            url = (row.get("url") or "").strip()
            title = (row.get("title") or "").strip()
            full_text = (row.get("full_text") or "").strip()
            fetched_at = row.get("fetched_at")
            if not url or not full_text:
                continue
            low_url = url.lower()
            if "/wp-content/uploads/" in low_url or ".pdf" in low_url:
                continue
            if is_non_academic_noise(url, title, full_text):
                continue
            excerpt = self._excerpt_for_query(full_text, resolved_query)
            if not excerpt:
                continue
            key = (url, excerpt[:180])
            if key in seen:
                continue
            seen.add(key)
            fetched_str = str(fetched_at) if fetched_at is not None else ""
            contexts.append(
                f"URL: {url}\nTitulo: {title}\nFetchedAt: {fetched_str}\nContenido: {excerpt}"
            )
            if len(contexts) >= max_contexts:
                break

        contexts = self._rank_context_blocks(contexts, resolved_query)[:max_contexts]

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
                        )[:max_contexts]
                else:
                    authority_contexts = await self._retrieve_authority_context_from_program(
                        source_url, programs[0]
                    )
                    if authority_contexts:
                        contexts = self._rank_context_blocks(
                            [*authority_contexts, *contexts], resolved_query
                        )[:max_contexts]

        allow_live_fetch = (
            self.ENABLE_RUNTIME_SCRAPE
            and not self._is_admissions_query(resolved_query)
            and not self._is_tramites_query(resolved_query)
        )
        # For authority/director queries, broad site discovery (_retrieve_from_source)
        # finds course/seminar pages (/ofertas-acad/) whose coordinators are NOT career
        # directors. Skip it entirely — authority data must come from /carreras/ pages only.
        if allow_live_fetch and not self._is_authority_query(resolved_query) and source_url and (
            not contexts or self._needs_source_fallback(contexts, resolved_query)
        ):
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
                contexts = merged[:max_contexts]

        if allow_live_fetch and source_url and self._query_has_specific_program(
            resolved_query
        ):
            _authority_only = self._is_authority_query(resolved_query)
            program_contexts = await self._retrieve_program_page_context(source_url, resolved_query, authority_only=_authority_only)
            if program_contexts:
                contexts = self._rank_context_blocks([*program_contexts, *contexts], resolved_query)[
                    :max_contexts
                ]

        return {"context": contexts}

    async def generate(self, state: AgentState):
        raw_history = state.get("history") or []
        context_blocks = self._contexts_for_prompt(state.get("context") or [])
        history_blocks = self._history_for_prompt(raw_history)
        session_state = self._normalized_session_state(state.get("session_state"))
        query = (state.get("query") or "").strip()
        source_id = (state.get("source_id") or "").strip()
        structured_intent = self._intent_from_query(query)

        if self.SIMPLE_RETRIEVAL_MODE:
            if not context_blocks:
                return {"response": self.NO_INFO_RESPONSE}
            extracted = self._extract_answer_from_context(query, context_blocks)
            if extracted:
                return {"response": extracted}
            history = "\n".join(history_blocks).strip()
            context = "\n\n---\n\n".join(context_blocks)
            prompt = (
                f"{SYSTEM_RAG}\n\n"
                "Usa SOLO el contexto recuperado. Si faltan datos, dilo explícitamente. "
                "Responde breve y cita al menos una URL fuente usada.\n\n"
                f"Historial reciente:\n{history}\n\n"
                f"Contexto recuperado:\n{context}\n\n"
                f"Pregunta del usuario:\n{state['query']}"
            )
            res = await self.llm.ainvoke(prompt)
            return {"response": (res.content or "").strip()}

        state_program = self._program_for_followup(session_state, raw_history, query)
        asks_program_specific = (
            self._is_authority_query(query)
            or self._is_duration_query(query)
            or self._is_workload_query(query)
            or self._is_year_subjects_query(query)
            or self._extract_year_from_query(query) is not None
            or self._looks_like_subjects_followup(query)
            or self._is_admissions_query(query)
            or self._is_tramites_query(query)
        )
        if asks_program_specific and not self._query_has_specific_program(query):
            if not state_program:
                return {"response": "Para responder bien, decime primero la carrera exacta."}
            if not self._has_confident_program_state(session_state):
                return {
                    "response": (
                        f"Para confirmar: ¿te referís a {state_program}? "
                        "Si es otra carrera, decime cuál."
                    )
                }

        # For authority questions, demand explicit program unless it can be inferred safely.
        if self._is_authority_query(query):
            inferred_program = state_program
            if not self._query_has_specific_program(query) and not inferred_program:
                return {
                    "response": "Para darte el dato correcto necesito la carrera exacta (por ejemplo, Medicina o Licenciatura en Enfermería)."
                }

        facts_context_block: str | None = None
        if self.USE_PROGRAM_FACTS:
            facts_answer = await self._answer_from_program_facts(
                source_id,
                query,
                raw_history,
                session_state=session_state,
            )
            if facts_answer:
                # Program/career listings are returned directly — they're already well-formatted
                # and adding LLM overhead doesn't add value.
                if re.match(r"^Carrera", facts_answer):
                    return {"response": facts_answer}
                # For factual data (director, duration, subjects, etc.), inject as structured
                # context so the LLM generates a natural conversational response instead of
                # a hardcoded template string.
                facts_context_block = f"[DATOS ESTRUCTURADOS]\n{facts_answer}"

        doc_answer = await self._answer_from_documents(
            source_id,
            query,
            raw_history,
            session_state=session_state,
        )
        if doc_answer:
            return {"response": doc_answer}
        if structured_intent in {
            "authority",
            "duration",
            "workload",
            "subjects",
            "admissions",
            "tramites",
            "program_count",
            "programs_overview",
        } and not facts_context_block:
            fallback_answer = await self._fallback_answer_from_live_source(
                source_id=source_id,
                query=query,
                history=raw_history,
                session_state=session_state,
            )
            if fallback_answer:
                return {"response": fallback_answer}
            if not context_blocks:
                return {"response": self.NO_INFO_RESPONSE}
        if self._is_duration_query(query):
            inferred_program = state_program
            if not self._query_has_specific_program(query) and not inferred_program:
                return {
                    "response": "Decime la carrera exacta y te paso la duración con fuente."
                }
        if self._needs_program_clarification(query):
            inferred_program = state_program
            if not inferred_program:
                return {
                    "response": "¿A qué carrera te referís exactamente?"
                }

        # Build effective context: structured facts (if any) + retrieved documents
        effective_contexts = (
            [facts_context_block, *context_blocks] if facts_context_block else context_blocks
        )
        if not effective_contexts:
            return {"response": self.NO_INFO_RESPONSE}

        extracted = self._extract_answer_from_context(query, context_blocks)
        if extracted and not facts_context_block:
            return {"response": extracted}

        history = "\n".join(history_blocks).strip()
        prompt_contexts = list(effective_contexts)
        prompt = ""
        while True:
            context = "\n\n---\n\n".join(prompt_contexts)
            prompt = (
                f"{SYSTEM_RAG}\n\n"
                "Regla crítica: si hay conflicto entre fuentes, prioriza la evidencia más reciente "
                "(campo FetchedAt y páginas canónicas de carrera como /carreras/ sobre noticias/eventos).\n\n"
                f"Estado de conversación:\n{self._session_state_summary(session_state)}\n\n"
                f"Historial reciente:\n{history}\n\n"
                f"Contexto recuperado:\n{context}\n\n"
                f"Pregunta del usuario:\n{state['query']}"
            )
            if len(prompt) <= self.MAX_PROMPT_CHARS or len(prompt_contexts) <= 1:
                break
            prompt_contexts = prompt_contexts[:-1]
        if len(prompt) > self.MAX_PROMPT_CHARS and history:
            compact_history = "\n".join(history_blocks[-4:]).strip()
            context = "\n\n---\n\n".join(prompt_contexts[:6])
            prompt = (
                f"{SYSTEM_RAG}\n\n"
                "Regla crítica: si hay conflicto entre fuentes, prioriza la evidencia más reciente "
                "(campo FetchedAt y páginas canónicas de carrera como /carreras/ sobre noticias/eventos).\n\n"
                f"Estado de conversación:\n{self._session_state_summary(session_state)}\n\n"
                f"Historial reciente:\n{compact_history}\n\n"
                f"Contexto recuperado:\n{context}\n\n"
                f"Pregunta del usuario:\n{state['query']}"
            )
        res = await self.llm.ainvoke(prompt)
        return {"response": (res.content or "").strip()}
