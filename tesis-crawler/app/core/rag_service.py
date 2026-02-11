import asyncio
import re
import unicodedata
from typing import List, TypedDict
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from uuid import UUID

import httpx
from bs4 import BeautifulSoup
from langgraph.graph import END, StateGraph
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
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


class RAGService:
    ENABLE_RUNTIME_SCRAPE = bool(settings.RAG_ENABLE_LIVE_FETCH)
    USE_PROGRAM_FACTS = True
    VECTOR_K = 90
    LEXICAL_K = 90
    URL_HINT_K = 180
    AUTHORITY_K = 260
    NON_AUTH_CONTEXTS = 26
    AUTHORITY_CONTEXTS = 48
    FALLBACK_DISCOVERY_LIMIT = 20
    FALLBACK_CONTEXT_LIMIT = 12
    PROFILE_INTENT_KEYS: dict[str, tuple[str, ...]] = {
        "ingresantes": ("ingresante", "ingresantes", "ingreso", "admis", "inscrip"),
        "estudiantes": ("estudiante", "estudiantes", "alumno", "alumnos"),
        "docentes": ("docente", "docentes", "profesor", "profesores", "cátedra", "catedra"),
        "nodocentes": ("nodocente", "nodocentes", "no docente", "no docentes"),
        "directivos": ("directivo", "directivos", "autoridades", "gestion", "gestión"),
    }

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
            r"^\s*lic\.?\s+en\s+",
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
        value = re.sub(
            r"\s+(?:programa|programa analitico|programa de examen|patologia|introduccion|resolucion)\b.*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        value = re.sub(r"\s+", " ", value).strip(" .,:;-/")
        return RAGService._clean_program_name(value)

    @staticmethod
    def _is_plausible_career_name(name: str) -> bool:
        value = re.sub(r"\s+", " ", (name or "").strip())
        if not value:
            return False
        if len(value) < 4 or len(value) > 80:
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
            "expedicion",
            "calendario",
            "materia",
        )
        return not any(tok in low for tok in blocked)

    @staticmethod
    def _career_name_from_url(url: str) -> str:
        low = (url or "").lower()
        if "/carreras/" not in low:
            return ""
        tail = low.split("/carreras/", 1)[1].strip("/")
        if not tail:
            return ""
        slug = tail.split("/", 1)[0].strip("- ")
        if not slug:
            return ""
        return RAGService._clean_program_name(slug)

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
        if any(tok in low for tok in ("/oferta-academica", "/ofertas-acad", "/ofertas-academicas", "/programas/")):
            score += 8
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
            if "/carreras/" not in low_url:
                continue

            after = low_url.split("/carreras/", 1)[1].strip("/")
            slug = after.split("/", 1)[0].strip()
            if slug and slug not in {"carreras", "category", "tag"}:
                candidate = RAGService._sanitize_career_name(RAGService._clean_program_name(slug))
                if candidate:
                    key = RAGService._normalize_name_key(candidate)
                    if key and key not in seen_names and RAGService._is_plausible_career_name(candidate):
                        seen_names.add(key)
                        names.append(candidate)
                        if url not in seen_urls:
                            seen_urls.add(url)
                            source_urls.append(url)

            for raw in name_pattern.findall(title):
                candidate = RAGService._sanitize_career_name(RAGService._clean_program_name(raw))
                if not candidate:
                    continue
                key = RAGService._normalize_name_key(candidate)
                if key and key not in seen_names and RAGService._is_plausible_career_name(candidate):
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
            r"lic\.?\s+en\s+[a-záéíóúñü\s]+|"
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
        return any(
            t in q
            for t in (
                "director",
                "coordinador",
                "responsable",
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
    def _extract_profile_intent(query: str) -> str | None:
        q = (query or "").lower()
        for key, hints in RAGService.PROFILE_INTENT_KEYS.items():
            if any(h in q for h in hints):
                return key
        return None

    @staticmethod
    def _is_tramites_query(query: str) -> bool:
        q = (query or "").lower()
        return any(t in q for t in ("tramite", "trámite", "trámites", "mesa de entradas", "gestión", "gestion"))

    @staticmethod
    def _is_admissions_query(query: str) -> bool:
        q = (query or "").lower()
        return any(t in q for t in ("ingreso", "ingresante", "ingresantes", "admis", "inscrip"))

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
            candidates.extend(
                [
                    urljoin(base, f"carreras/{slug}/"),
                    urljoin(base, f"carreras/{slug}"),
                    urljoin(base, f"ofertas-acad/{slug}"),
                    urljoin(base, f"oferta-academica/{slug}"),
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
        return re.sub(r"\s+", " ", (value or "").strip().lower())

    @staticmethod
    def _program_lookup_variants(program_name: str) -> list[str]:
        base = RAGService._normalize_program_for_lookup(program_name)
        if not base:
            return []
        variants = {base}
        variants.add(re.sub(r"^lic\.?\s+en\s+", "licenciatura en ", base, flags=re.IGNORECASE))
        variants.add(re.sub(r"^licenciatura en\s+", "", base, flags=re.IGNORECASE))
        variants.add(re.sub(r"^lic\.?\s+en\s+", "", base, flags=re.IGNORECASE))
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
    def _extract_subjects_from_year_block(text: str, year_num: int) -> list[str]:
        q = (text or "")
        low = q.lower()
        labels = {
            1: ("primer año", "primer anio", "1er año", "1er anio", "año 1", "anio 1"),
            2: ("segundo año", "segundo anio", "2do año", "2do anio", "año 2", "anio 2"),
            3: ("tercer año", "tercer anio", "3er año", "3er anio", "año 3", "anio 3"),
            4: ("cuarto año", "cuarto anio", "4to año", "4to anio", "año 4", "anio 4"),
            5: ("quinto año", "quinto anio", "5to año", "5to anio", "año 5", "anio 5"),
            6: ("sexto año", "sexto anio", "6to año", "6to anio", "año 6", "anio 6"),
        }
        starts = labels.get(year_num, ())
        if not starts:
            return []
        start = -1
        for s in starts:
            idx = low.find(s)
            if idx >= 0:
                start = idx
                break
        if start < 0:
            return []
        end = len(q)
        for y, lbls in labels.items():
            if y <= year_num:
                continue
            for s in lbls:
                idx = low.find(s, start + 1)
                if idx >= 0:
                    end = min(end, idx)
        block = q[start:end]
        subjects = re.findall(
            r"(?:^|\n)\s*(?:materia|asignatura)\s*:\s*(?:\n\s*)?([^\n]{3,120})",
            block,
            flags=re.IGNORECASE,
        )
        out: list[str] = []
        seen: set[str] = set()
        for s in subjects:
            v = re.sub(r"\s+", " ", (s or "").strip(" .:-\t"))
            if not v:
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
    ) -> str | None:
        if not source_id:
            return None
        try:
            UUID(source_id)
        except ValueError:
            return None

        program_mentions = self._extract_program_mentions_from_text(query)
        inferred_program = self._infer_program_from_history(history, query)
        program_name = program_mentions[0] if program_mentions else inferred_program
        slugs = self._slug_candidates_for_program(program_name or "")
        asked_year = self._extract_year_from_query(query) or self._infer_year_from_history(history, query)

        stmt = text(
            """
            SELECT d.canonical_url, COALESCE(d.title, '') AS title, d.fetched_at,
                   string_agg(c.text, E'\n' ORDER BY c.chunk_id) AS full_text
            FROM documents d
            JOIN chunks c ON c.doc_id = d.doc_id
            WHERE d.source_id = CAST(:source_id AS uuid)
              AND (
                d.page_type IN ('career_canonical', 'offer_canonical', 'curriculum')
                OR
                d.canonical_url ILIKE '%/carreras/%'
                OR d.canonical_url ILIKE '%/oferta-academica/%'
                OR d.canonical_url ILIKE '%/ofertas-acad/%'
                OR d.canonical_url ILIKE '%/ofertas-academicas/%'
              )
            GROUP BY d.doc_id
            ORDER BY
              CASE WHEN d.canonical_url ILIKE '%/carreras/%' THEN 0 ELSE 1 END,
              d.fetched_at DESC
            LIMIT 80
            """
        )
        async with async_session() as session:
            rows = (await session.execute(stmt, {"source_id": str(UUID(source_id))})).mappings().all()
        if not rows:
            return None

        docs: list[dict] = []
        for row in rows:
            url = str(row.get("canonical_url") or "").strip()
            title = str(row.get("title") or "").strip()
            txt = str(row.get("full_text") or "").strip()
            if not url or not txt:
                continue
            if slugs:
                low = f"{url.lower()} {title.lower()} {txt[:2500].lower()}"
                if not any(slug in low for slug in slugs):
                    continue
            docs.append({"url": url, "title": title, "text": txt})
        if not docs:
            docs = [{"url": str(r.get("canonical_url") or ""), "title": str(r.get("title") or ""), "text": str(r.get("full_text") or "")} for r in rows]

        q = (query or "").lower()
        if self._is_duration_query(q):
            best: tuple[int, str, str] | None = None
            for d in docs:
                low_url = d["url"].lower()
                if "/ofertas-acad/" in low_url and "/carreras/" not in low_url:
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
            for d in docs:
                low_url = d["url"].lower()
                if "/ofertas-acad/" in low_url and "/carreras/" not in low_url:
                    continue
                patterns = (
                    r"(?:director(?:a)?\s+de\s+carrera|direcci[oó]n\s+de\s+carrera|coordinador(?:a)?(?:\s+de\s+carrera)?|responsable\s+de\s+carrera)\s*[:\-]\s*([^\n|]{3,120})",
                    r"(?:director(?:a)?\s+de\s+(?:la\s+)?carrera(?:\s+de)?[^\n:|]{0,90}?)\s+(?:es\s+)?([A-ZÁÉÍÓÚÑ][^\n|]{2,120})",
                    r"(?:secretario(?:a)?\s+acad[eé]mic[oa])\s*[:\-]\s*([^\n|]{3,120})",
                    r"(?:decano(?:a)?|vicedecano(?:a)?)\s*[:\-]\s*([^\n|]{3,120})",
                )
                for p in patterns:
                    m = re.search(p, d["text"], flags=re.IGNORECASE)
                    if not m:
                        continue
                    value = re.sub(r"\s+", " ", m.group(1)).strip(" .:-*_`\t")
                    score = 100 if "/carreras/" in d["url"].lower() else 70
                    candidates.append((score, value, d["url"]))
                    break
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                label = (
                    "decano/a"
                    if self._wants_dean(q)
                    else ("secretario/a académico/a" if self._wants_secretary(q) else "director/a de carrera")
                )
                pname = program_name or "la carrera"
                return f"El/la {label} de {pname} es {candidates[0][1]}. Fuente: {candidates[0][2]}"

        if asked_year is not None and any(t in q for t in ("materia", "materias", "asignatura", "asignaturas", "plan de estudios", "todas", "complet")):
            merged: list[str] = []
            src = ""
            for d in docs:
                subs = self._extract_subjects_from_year_block(d["text"], asked_year)
                if not subs:
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
        return None

    async def _answer_from_program_facts(
        self,
        source_id: str,
        query: str,
        history: list[str],
    ) -> str | None:
        if not source_id:
            return None
        try:
            UUID(source_id)
        except ValueError:
            return None

        is_duration = self._is_duration_query(query)
        asked_year = self._extract_year_from_query(query)
        inferred_year = self._infer_year_from_history(history, query)
        asks_subjects_terms = any(
            t in (query or "").lower()
            for t in ("materia", "materias", "asignatura", "asignaturas", "plan de estudios")
        )
        is_subjects_followup = self._looks_like_subjects_followup(query) and inferred_year is not None
        if asked_year is None and (asks_subjects_terms or is_subjects_followup):
            asked_year = inferred_year
        is_year_subjects = asked_year is not None and (asks_subjects_terms or is_subjects_followup)
        is_authority = self._is_authority_query(query)
        is_program_count = self._is_program_count_query(query)
        is_programs_overview = self._is_programs_query(query) and not self._query_has_specific_program(query)
        profile_intent = self._extract_profile_intent(query)
        is_tramites = self._is_tramites_query(query)
        is_admissions = self._is_admissions_query(query)
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
        program_mentions = self._extract_program_mentions_from_text(query)
        inferred_program = self._infer_program_from_history(history, query)
        program_name = program_mentions[0] if program_mentions else inferred_program
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
                    SELECT canonical_url, COALESCE(title, '') AS title
                    FROM documents
                    WHERE source_id = CAST(:source_id AS uuid)
                      AND (
                        canonical_url ILIKE '%/carreras/%'
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
                for row in career_rows:
                    url = str(row.get("canonical_url") or "").strip()
                    title = str(row.get("title") or "").strip()
                    candidate = self._career_name_from_url(url)
                    if not candidate:
                        title_match = re.search(
                            r"\b(licenciatura\s+en\s+[a-záéíóúñü\s]+|medicina)\b",
                            title,
                            flags=re.IGNORECASE,
                        )
                        if title_match:
                            candidate = self._clean_program_name(title_match.group(1))
                    candidate = self._sanitize_career_name(candidate)
                    if not self._is_plausible_career_name(candidate):
                        continue
                    key = self._normalize_name_key(candidate)
                    if key in seen_careers:
                        continue
                    seen_careers.add(key)
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
                      AND canonical_url ILIKE '%/carreras/%'
                      AND (
                          lower(program_name) = :program_exact
                          OR (:program_like <> '' AND lower(program_name) LIKE :program_like)
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
                    if self._is_canonical_program_url(str((dict(r)).get("canonical_url") or ""))
                ]
                fallback_rows = [
                    dict(r)
                    for r in rows
                    if not is_non_academic_noise(str((dict(r)).get("canonical_url") or ""), "", "")
                ]
                best = self._pick_best_fact_row(canonical_rows or fallback_rows)
                if best:
                    value = (best.get("fact_value") or "").strip()
                    pname = (best.get("program_name") or "").strip() or (program_name or "la carrera")
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
                          lower(program_name) = :program_exact
                          OR (:program_like <> '' AND lower(program_name) LIKE :program_like)
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
                          lower(program_name) = :program_exact
                          OR (:program_like <> '' AND lower(program_name) LIKE :program_like)
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
                    for row in rows:
                        src = str(row.get("canonical_url") or "").strip()
                        if is_non_academic_noise(src, "", ""):
                            continue
                        v = re.sub(r"\s+", " ", str(row.get("fact_value") or "")).strip()
                        if not v:
                            continue
                        key = v.lower()
                        if key in seen_values:
                            continue
                        seen_values.add(key)
                        deduped.append(dict(row) | {"fact_value": v})
                    if deduped:
                        pname = (
                            str(deduped[0].get("program_name") or "").strip()
                            or (program_name or "la carrera")
                        )
                        src = str(deduped[0].get("canonical_url") or "").strip()
                        lines = [f"{idx}. {str(r.get('fact_value') or '').strip()}" for idx, r in enumerate(deduped[:20], 1)]
                        return (
                            f"Materias de año {asked_year} de {pname}:\n"
                            + "\n".join(lines)
                            + f"\n\nFuente: {src}"
                        )

            if profile_intent is not None or is_tramites or is_admissions:
                keys: list[str] = []
                if profile_intent is not None:
                    keys.append(f"profile_{profile_intent}_page")
                if is_tramites:
                    keys.append("tramites_page")
                if is_admissions:
                    keys.append("admissions_page")
                keys = list(dict.fromkeys(keys))
                profile_stmt = text(
                    """
                    SELECT fact_key, fact_value, evidence_text, confidence, fetched_at
                    FROM program_facts
                    WHERE source_id = CAST(:source_id AS uuid)
                      AND fact_key = ANY(:fact_keys)
                    ORDER BY confidence DESC, fetched_at DESC
                    LIMIT 30
                    """
                )
                rows = (
                    await session.execute(
                        profile_stmt,
                        {"source_id": source_uuid, "fact_keys": keys},
                    )
                ).mappings().all()
                if rows:
                    dedup_urls: list[dict] = []
                    seen_urls: set[str] = set()
                    for row in rows:
                        url = str(row.get("fact_value") or "").strip()
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        dedup_urls.append(dict(row))
                    if dedup_urls:
                        lines: list[str] = []
                        for idx, row in enumerate(dedup_urls[:5], 1):
                            title = str(row.get("evidence_text") or "").strip()
                            url = str(row.get("fact_value") or "").strip()
                            lines.append(f"{idx}. {title or 'Página informativa'}: {url}")
                        if profile_intent is not None:
                            return (
                                f"Páginas relevantes para {profile_intent}:\n"
                                + "\n".join(lines)
                                + "\n\nSi querés, te respondo una consulta puntual usando estas fuentes."
                            )
                        if is_tramites:
                            return "Páginas de trámites encontradas:\n" + "\n".join(lines)
                        if is_admissions:
                            return "Páginas de ingreso/admisión encontradas:\n" + "\n".join(lines)
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

    async def _retrieve_from_source(self, source_url: str, query: str) -> list[str]:
        if not self.ENABLE_RUNTIME_SCRAPE:
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

    async def _retrieve_program_page_context(self, source_url: str, query: str) -> list[str]:
        if not self.ENABLE_RUNTIME_SCRAPE:
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
            candidates.extend(
                [
                    urljoin(base, f"carreras/{slug}/"),
                    urljoin(base, f"carreras/{slug}"),
                    urljoin(base, f"ofertas-acad/{slug}"),
                    urljoin(base, f"oferta-academica/{slug}"),
                ]
            )
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
                    for token in ("/carreras/", "/oferta-academica", "/programas/", "oferta académica")
                ):
                    score += 8
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

        if RAGService._is_authority_query(q):
            candidates: list[tuple[int, str, str]] = []
            for block in context_blocks:
                src = RAGService._extract_url_from_block(block)
                low_src = (src or "").lower()
                if "/ofertas-acad/" in low_src and "/carreras/" not in low_src:
                    continue
                direct_patterns = (
                    r"(?:director(?:a)?\s+de\s+carrera|direcci[oó]n\s+de\s+carrera|coordinador(?:a)?(?:\s+de\s+carrera)?|responsable\s+de\s+carrera)\s*[:\-]\s*([^\n|]+)",
                    r"(?:director(?:a)?\s+de\s+(?:la\s+)?carrera(?:\s+de)?[^\n:|]{0,90}?)\s+(?:es\s+)?([A-ZÁÉÍÓÚÑ][^\n|]{2,120})",
                    r"(?:secretario(?:a)?\s+acad[eé]mic[oa])\s*[:\-]\s*([^\n|]+)",
                    r"(?:decano(?:a)?|vicedecano(?:a)?)\s*[:\-]\s*([^\n|]+)",
                    r"\|\s*(?:director(?:a)?\s+de\s+carrera|direcci[oó]n\s+de\s+carrera|coordinador(?:a)?|responsable)\s*\|\s*([^\|\n]+)\|",
                    r"\|\s*(?:secretario(?:a)?\s+acad[eé]mic[oa])\s*\|\s*([^\|\n]+)\|",
                    r"(?:^|\n)\s*#{1,4}\s*(?:director(?:a)?|coordinador(?:a)?|direcci[oó]n)\s*(?:de\s+carrera)?\s*\n+\s*([^\n]+)",
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
                    if value:
                        src = RAGService._extract_url_from_block(block)
                        score = RAGService._authority_url_score(src)
                        candidates.append((score, value, src))
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
                return (
                    f"La autoridad indicada en el sitio es {best['value']}. "
                    f"Fuente: {best['src'] or 'contexto recuperado'}"
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
            SELECT d.canonical_url AS url, COALESCE(d.title, '') AS title, c.text AS chunk_text, d.fetched_at AS fetched_at
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            JOIN sources s ON s.source_id = d.source_id
            WHERE c.embedding IS NOT NULL
              AND (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
              AND COALESCE(d.page_type, 'institutional_info') NOT IN ('news_blocked', 'utility_noise', 'procurement')
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
            SELECT d.canonical_url AS url, COALESCE(d.title, '') AS title, c.text AS chunk_text, d.fetched_at AS fetched_at
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            JOIN sources s ON s.source_id = d.source_id
            WHERE to_tsvector('spanish', COALESCE(d.title, '') || ' ' || c.text)
                  @@ websearch_to_tsquery('spanish', :q)
              AND (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
              AND COALESCE(d.page_type, 'institutional_info') NOT IN ('news_blocked', 'utility_noise', 'procurement')
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
            SELECT d.canonical_url AS url, COALESCE(d.title, '') AS title, c.text AS chunk_text, d.fetched_at AS fetched_at
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
                OR d.title ILIKE '%requisito%'
            )
              AND (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
              AND COALESCE(d.page_type, 'institutional_info') NOT IN ('news_blocked', 'utility_noise', 'procurement')
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
            """
            SELECT d.canonical_url AS url, COALESCE(d.title, '') AS title, c.text AS chunk_text, d.fetched_at AS fetched_at
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            JOIN sources s ON s.source_id = d.source_id
            WHERE (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
              AND COALESCE(d.page_type, 'institutional_info') NOT IN ('news_blocked', 'utility_noise', 'procurement')
              AND (
                  d.canonical_url ILIKE '%/carreras/%'
                  OR d.canonical_url ILIKE '%enfermeria%'
                  OR d.title ILIKE '%director%'
                  OR d.title ILIKE '%coordinador%'
                  OR d.title ILIKE '%secretari%'
                  OR c.text ILIKE '%director de carrera%'
                  OR c.text ILIKE '%coordinador%'
                  OR c.text ILIKE '%responsable de carrera%'
                  OR c.text ILIKE '%secretario academico%'
                  OR c.text ILIKE '%secretario académico%'
                  OR c.text ILIKE '%secretaria academica%'
                  OR to_tsvector('spanish', COALESCE(d.title, '') || ' ' || c.text)
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

        contexts: list[str] = []
        seen: set[tuple[str, str]] = set()
        authority_query = self._is_authority_query(resolved_query)

        async with async_session() as session:
            vector_rows = (
                await session.execute(
                    vector_stmt,
                    {
                        "query_embedding": vector_literal,
                        "k": self.VECTOR_K,
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
                            "k": self.LEXICAL_K,
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
                        {"k": self.URL_HINT_K, "domain_1": domain_1, "domain_2": domain_2},
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
                                "k": self.LEXICAL_K,
                                "domain_1": domain_1,
                                "domain_2": domain_2,
                            },
                        )
                    ).mappings().all()
                    retry_rows.extend(rows)
            authority_rows = []
            if authority_query:
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

        max_contexts = self.AUTHORITY_CONTEXTS if authority_query else self.NON_AUTH_CONTEXTS
        for row in [*authority_rows, *lexical_rows, *hinted_rows, *vector_rows, *retry_rows]:
            url = (row.get("url") or "").strip()
            title = (row.get("title") or "").strip()
            chunk_text = (row.get("chunk_text") or "").strip()
            fetched_at = row.get("fetched_at")
            if not url or not chunk_text:
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

        if self.ENABLE_RUNTIME_SCRAPE and source_url and (
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

        if self.ENABLE_RUNTIME_SCRAPE and source_url and self._query_has_specific_program(
            resolved_query
        ):
            program_contexts = await self._retrieve_program_page_context(source_url, resolved_query)
            if program_contexts:
                contexts = self._rank_context_blocks([*program_contexts, *contexts], resolved_query)[
                    :max_contexts
                ]

        return {"context": contexts}

    async def generate(self, state: AgentState):
        context_blocks = state.get("context") or []
        history_blocks = state.get("history") or []
        query = (state.get("query") or "").strip()
        source_id = (state.get("source_id") or "").strip()

        # For authority questions, demand explicit program unless it can be inferred safely.
        if self._is_authority_query(query):
            inferred_program = self._infer_program_from_history(history_blocks, query)
            if not self._query_has_specific_program(query) and not inferred_program:
                return {
                    "response": "Decime la carrera exacta (por ejemplo: Medicina o Licenciatura en Enfermería) y te doy el dato puntual con fuente."
                }

        if self.USE_PROGRAM_FACTS:
            facts_answer = await self._answer_from_program_facts(source_id, query, history_blocks)
            if facts_answer:
                return {"response": facts_answer}
        doc_answer = await self._answer_from_documents(source_id, query, history_blocks)
        if doc_answer:
            return {"response": doc_answer}
        if self._is_duration_query(query):
            inferred_program = self._infer_program_from_history(history_blocks, query)
            if not self._query_has_specific_program(query) and not inferred_program:
                return {
                    "response": "Necesito el nombre exacto de la carrera para darte la duración correcta con fuente."
                }
        if self._needs_program_clarification(query):
            inferred_program = self._infer_program_from_history(history_blocks, query)
            if not inferred_program:
                return {
                    "response": "¿A qué carrera te referís? Si me decís el nombre exacto, te doy el dato puntual."
                }
        if not context_blocks:
            return {
                "response": "No tengo evidencia suficiente en los documentos indexados actuales para responder con precisión. Si me indicás la carrera exacta, puedo reintentar con búsqueda más específica."
            }
        extracted = self._extract_answer_from_context(query, context_blocks)
        if extracted:
            return {"response": extracted}

        context = "\n\n---\n\n".join(context_blocks)
        history = "\n".join(history_blocks[-16:]).strip()
        prompt = (
            f"{SYSTEM_RAG}\n\n"
            "Regla crítica: si hay conflicto entre fuentes, prioriza la evidencia más reciente "
            "(campo FetchedAt y páginas canónicas de carrera como /carreras/ sobre noticias/eventos).\n\n"
            f"Historial reciente:\n{history}\n\n"
            f"Contexto recuperado:\n{context}\n\n"
            f"Pregunta del usuario:\n{state['query']}"
        )
        res = await self.llm.ainvoke(prompt)
        return {"response": (res.content or "").strip()}
