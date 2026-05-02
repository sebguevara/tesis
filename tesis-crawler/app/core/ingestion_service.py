import hashlib
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import delete, select
from app.config import settings
from app.core.content_filters import should_index_page
from app.core.domain_utils import domain_variants, normalize_domain, normalize_host_exact
from app.embedding.models import Document, ProgramFact, Source, utc_now_naive


logger = logging.getLogger(__name__)


class IngestionService:
    YEAR_LABELS: list[tuple[int, tuple[str, ...]]] = [
        (1, ("primer año", "primer anio", "1er año", "1er anio", "año 1", "anio 1")),
        (2, ("segundo año", "segundo anio", "2do año", "2do anio", "año 2", "anio 2")),
        (3, ("tercer año", "tercer anio", "3er año", "3er anio", "año 3", "anio 3")),
        (4, ("cuarto año", "cuarto anio", "4to año", "4to anio", "año 4", "anio 4")),
        (5, ("quinto año", "quinto anio", "5to año", "5to anio", "año 5", "anio 5")),
        (6, ("sexto año", "sexto anio", "6to año", "6to anio", "año 6", "anio 6")),
    ]

    PROFILE_HINTS: dict[str, tuple[str, ...]] = {
        "ingresantes": ("ingresantes", "ingreso", "admis", "inscrip"),
        "estudiantes": ("estudiantes", "alumnos", "vida estudiantil"),
        "docentes": ("docentes", "profesores", "cátedra", "catedra"),
        "nodocentes": ("nodocentes", "no docentes", "personal"),
        "directivos": ("directivos", "gestión", "gestion", "autoridades"),
    }

    @staticmethod
    def _is_program_canonical_url(url: str) -> bool:
        low = (url or "").lower()
        return any(
            token in low
            for token in (
                "/carreras/",
                "/oferta-academica/",
                "/ofertas-academicas/",
            )
        )

    @staticmethod
    def _is_program_page(page_type: str, url: str) -> bool:
        ptype = (page_type or "").strip().lower()
        low_url = (url or "").lower()
        return ptype in {"career_canonical", "curriculum"} or "/carreras/" in low_url

    @staticmethod
    def _is_fact_source_url(url: str, page_type: str) -> bool:
        """
        Strict gate for ProgramFact extraction.
        Only canonical career/curriculum pages should generate structured facts.
        """
        low_url = (url or "").lower()
        ptype = (page_type or "").strip().lower()
        if ptype in {"career_canonical", "curriculum"}:
            return True
        if "/carreras/" in low_url:
            return True
        if any(token in low_url for token in ("/plan-de-estudios", "/distribucion-de-asignaturas")):
            return True
        return False

    @staticmethod
    def _has_program_page_signals(url: str, title: str, content: str, program_name: str) -> bool:
        if not program_name or program_name == "__general__":
            return False
        haystack = f"{(url or '').lower()} {(title or '').lower()} {(content or '').lower()[:3500]}"
        return any(
            token in haystack
            for token in (
                "duración de la carrera",
                "duracion de la carrera",
                "director de carrera",
                "dirección de carrera",
                "direccion de carrera",
                "coordinador de carrera",
                "responsable de carrera",
                "plan de estudios",
                "perfil del egresado",
                "alcances del título",
                "alcances del titulo",
                "incumbencias",
                "carga horaria",
                "materia:",
                "asignatura:",
            )
        )

    @staticmethod
    def _is_year_subject_fact_source(url: str, page_type: str) -> bool:
        low_url = (url or "").lower()
        if not low_url:
            return False
        if "/wp-content/uploads/" in low_url or low_url.endswith(".pdf") or ".pdf?" in low_url:
            return False
        ptype = (page_type or "").strip().lower()
        if ptype == "career_canonical":
            return True
        if "/carreras/" in low_url:
            return True
        return False

    @staticmethod
    def _is_duration_value_plausible(value: str) -> bool:
        raw = re.sub(r"\s+", " ", (value or "").lower()).strip()
        if not raw:
            return False
        m = re.search(r"(\d{1,2})\s*(?:a[nñ]os|años)\b", raw)
        if not m:
            return False
        years = int(m.group(1))
        return 1 <= years <= 12

    def __init__(self):
        pass  # No splitters needed — pgai handles chunking + embedding

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
            # Keep repeated structural labels; they are meaningful in curricula.
            if normalized in {
                "materia:",
                "asignatura:",
                "carga horaria:",
                "programa:",
                "plan de estudios",
            }:
                cleaned.append(line)
                continue
            max_repetitions = 2
            if "área de prensa" in normalized:
                max_repetitions = 1
            count = line_counts.get(normalized, 0)
            if count >= max_repetitions:
                continue

            line_counts[normalized] = count + 1
            cleaned.append(line)

        return "\n".join(cleaned).strip()

    @staticmethod
    def _slug_to_program_name(slug: str) -> str:
        value = (slug or "").strip().strip("/")
        if not value:
            return ""
        value = value.replace("-", " ")
        value = re.sub(r"\s+", " ", value).strip()
        words: list[str] = []
        for w in value.split(" "):
            lw = w.lower()
            if lw in {"de", "del", "la", "las", "los", "y", "en"}:
                words.append(lw)
            elif lw in {"lic", "lic.", "licenciatura"}:
                words.append("Licenciatura")
            else:
                words.append(w.capitalize())
        return " ".join(words).strip()

    @staticmethod
    def _normalize_program_name(value: str) -> str:
        text = (value or "").strip()
        if not text:
            return ""
        text = re.sub(r"^\s*lic\.?\s+en\s+", "Licenciatura en ", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _is_plausible_program_name(value: str) -> bool:
        text = re.sub(r"\s+", " ", (value or "").strip())
        if not text:
            return False
        if len(text) < 4 or len(text) > 90:
            return False
        low = text.lower()
        blocked = (
            "programa",
            "analitico",
            "examen",
            "cohorte",
            "curso",
            "jornada",
            "congreso",
            "semillero",
            "residencia",
            "departamento",
            "resolucion",
            "universidad nacional del nordeste dependencia",
        )
        if any(tok in low for tok in blocked):
            return False
        return any(
            tok in low
            for tok in ("medicina", "licenciatura en", "tecnicatura en", "doctorado en", "especializacion en")
        )

    @staticmethod
    def _extract_program_name(url: str, title: str, content: str) -> str:
        parsed = urlparse(url)
        path = (parsed.path or "").lower()
        if "/carreras/" in path:
            slug = path.split("/carreras/", 1)[1].split("/", 1)[0]
            candidate = IngestionService._slug_to_program_name(slug)
            if candidate:
                normalized = IngestionService._normalize_program_name(candidate)
                if IngestionService._is_plausible_program_name(normalized):
                    return normalized

        # Conservative fallback: use title-only matches to avoid noisy extraction from long body text.
        low_title = re.sub(r"\s+", " ", (title or "").strip().lower())
        if any(
            tok in low_title
            for tok in ("programa", "analitico", "cohorte", "curso", "jornada", "semillero", "ofertas acad")
        ):
            return ""
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
        match = pattern.search(low_title)
        if not match:
            return ""
        candidate = IngestionService._normalize_program_name(match.group(1))
        return candidate if IngestionService._is_plausible_program_name(candidate) else ""

    @staticmethod
    def _extract_fact_matches(
        content: str,
        patterns: list[str],
        max_len: int = 120,
    ) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            for m in re.finditer(pattern, content, flags=re.IGNORECASE):
                value = (m.group(1) or "").strip(" .:-\t")
                value = re.sub(r"\s+", " ", value).strip()
                if not value or len(value) > max_len:
                    continue
                key = value.lower()
                if key in seen:
                    continue
                seen.add(key)
                found.append(value)
        return found

    @staticmethod
    def _is_plausible_authority_person_name(value: str) -> bool:
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
    def _is_plausible_subject_name(value: str) -> bool:
        raw = re.sub(r"\s+", " ", (value or "").strip(" .:-\t")).lower()
        if not raw:
            return False
        if len(raw) < 4 or len(raw) > 120:
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
        if any(tok in raw for tok in blocked):
            return False
        alpha_tokens = re.findall(r"[a-záéíóúñü]{2,}", raw, flags=re.IGNORECASE)
        if len(alpha_tokens) >= 2:
            return True
        if len(alpha_tokens) == 1 and len(alpha_tokens[0]) >= 8:
            return True
        return False

    @staticmethod
    def _best_year_block(content: str, year_num: int) -> str:
        labels_map = dict(IngestionService.YEAR_LABELS)
        starts = labels_map.get(year_num, ())
        if not starts:
            return ""
        source = content or ""
        if not source:
            return ""
        low = source.lower()

        candidate_starts: list[int] = []
        for label in starts:
            for m in re.finditer(re.escape(label), low):
                candidate_starts.append(m.start())
        if not candidate_starts:
            return ""

        next_markers: list[int] = []
        for next_year, next_labels in IngestionService.YEAR_LABELS:
            if next_year <= year_num:
                continue
            for label in next_labels:
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
    def _slice_year_block(content: str, year_num: int) -> str:
        return IngestionService._best_year_block(content, year_num)

    @staticmethod
    def _extract_year_subject_facts(url: str, content: str) -> list[dict]:
        facts: list[dict] = []
        canonical_bonus = 0.9 if "/carreras/" in (url or "").lower() else 0.75
        for year_num, _labels in IngestionService.YEAR_LABELS:
            block = IngestionService._slice_year_block(content, year_num)
            if not block:
                continue
            subjects = IngestionService._extract_fact_matches(
                block,
                [
                    r"(?:^|\n)\s*materia\s*:\s*([^\n]{3,120})",
                    r"(?:^|\n)\s*materia\s*:\s*\n+\s*([^\n]{3,120})",
                    r"(?:^|\n)\s*asignatura\s*:\s*([^\n]{3,120})",
                    r"(?:^|\n)\s*asignatura\s*:\s*\n+\s*([^\n]{3,120})",
                    r"(?:^|\n)\s*-\s*([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ0-9\s\-/]{3,120})\s*(?:\n|$)",
                    r"(?:^|\n)\s*([A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9\s().,\-/]{3,120})\s*\|",
                ],
            )
            for subject in subjects:
                clean_subject = re.sub(r"\s+", " ", subject).strip(" .:-\t")
                if not clean_subject:
                    continue
                if not IngestionService._is_plausible_subject_name(clean_subject):
                    continue
                facts.append(
                    {
                        "fact_key": f"year_{year_num}_subject",
                        "fact_value": clean_subject,
                        "evidence_text": clean_subject,
                        "confidence": canonical_bonus,
                    }
                )
                if year_num == 1:
                    # Backward compatibility with existing resolver.
                    facts.append(
                        {
                            "fact_key": "first_year_subject",
                            "fact_value": clean_subject,
                            "evidence_text": clean_subject,
                            "confidence": canonical_bonus,
                        }
                    )
        return facts

    @staticmethod
    def _extract_titled_person_lines(content: str, max_scan_lines: int = 0) -> list[str]:
        lines = (content or "").splitlines()
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
            if not IngestionService._is_plausible_authority_person_name(line):
                continue
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(line)
        return out

    @staticmethod
    def _extract_profile_page_facts(url: str, title: str, content: str) -> list[dict]:
        low_url = (url or "").lower()
        low_title = (title or "").lower()
        low_content = (content or "").lower()
        haystack = f"{low_url} {low_title} {low_content[:3000]}"
        if any(token in low_url for token in ("/ofertas-acad/", "/ofertas-academicas/", "/oferta-academica/")):
            return []
        facts: list[dict] = []
        for profile_key, hints in IngestionService.PROFILE_HINTS.items():
            if any(h in haystack for h in hints):
                facts.append(
                    {
                        "fact_key": f"profile_{profile_key}_page",
                        "fact_value": url,
                        "evidence_text": (title or "").strip()[:200],
                        "confidence": 0.86 if any(h in low_url for h in hints) else 0.72,
                    }
                )

        if any(t in haystack for t in ("tramite", "trámite", "mesa de entradas", "diploma", "titulo", "título")):
            facts.append(
                {
                    "fact_key": "tramites_page",
                    "fact_value": url,
                    "evidence_text": (title or "").strip()[:200],
                    "confidence": 0.9 if any(t in low_url for t in ("tramite", "trámite", "diploma", "titulo")) else 0.74,
                }
            )
        admissions_url_hints = (
            "/ingreso",
            "/ingresantes",
            "/admis",
            "/inscrip",
            "/requisitos",
            "/aspirantes",
        )
        if any(t in haystack for t in ("admis", "ingres", "inscrip", "requisito")) and any(
            hint in low_url for hint in admissions_url_hints
        ):
            facts.append(
                {
                    "fact_key": "admissions_page",
                    "fact_value": url,
                    "evidence_text": (title or "").strip()[:200],
                    "confidence": 0.88,
                }
            )
        return facts

    @staticmethod
    def _extract_program_facts(url: str, title: str, content: str, page_type: str = "") -> list[dict]:
        program_name = IngestionService._extract_program_name(url, title, content)
        if not program_name:
            program_name = "__general__"
        is_program_page = IngestionService._is_program_page(page_type, url) or IngestionService._has_program_page_signals(
            url,
            title,
            content,
            program_name,
        )

        facts: list[dict] = []
        if program_name != "__general__":
            facts.append(
                {
                    "fact_key": "program_name",
                    "fact_value": program_name,
                    "evidence_text": title[:200] if title else program_name,
                    "confidence": 0.95 if "/carreras/" in (url or "").lower() else 0.85,
                }
            )

        # High-confidence patterns: explicitly say "director/a de carrera", "dirección de carrera",
        # "coordinador/a de carrera", or "responsable de carrera" — not bare "responsable".
        director_matches_high = IngestionService._extract_fact_matches(
            content,
            [
                r"(?:director(?:a)?\s+de\s+carrera|direcci[oó]n\s+de\s+carrera|coordinador(?:a)?\s+de\s+carrera|responsable\s+de\s+carrera)\s*[:\-]\s*([^\n|]{3,120})",
                r"(?:director(?:a)?\s+de\s+(?:la\s+)?carrera(?:\s+de)?[^\n:|]{0,90}?)\s+(?:es\s+)?([A-ZÁÉÍÓÚÑ][^\n|]{3,120})",
                r"\|\s*(?:director(?:a)?\s+de\s+carrera|direcci[oó]n\s+de\s+carrera|coordinador(?:a)?\s+de\s+carrera)\s*\|\s*([^\|\n]{3,120})\|",
                r"(?:^|\n)\s*#{1,6}\s*(?:director(?:a)?|direcci[oó]n)(?:\s+de\s+carrera)[^\n]*\n+\s*([^\n|]{3,120})",
                r"(?:^|\n)\s*(?:director(?:a)?|direcci[oó]n)(?:\s+de\s+carrera)[^\n]*\n+\s*([^\n|]{3,120})",
            ],
        )
        # Lower-confidence fallback: generic "director", "coordinador" without "de carrera".
        # Bare "responsable" is intentionally excluded — it captures unrelated roles (e.g. diplomas).
        seen_high = {m.lower() for m in director_matches_high}
        director_matches_low = IngestionService._extract_fact_matches(
            content,
            [
                r"(?:director(?:a)?|coordinador(?:a)?|jef(?:e|a)\s+de\s+carrera)\s*[:\-]\s*([^\n|]{3,120})",
                r"\|\s*(?:director(?:a)?|coordinador(?:a)?)\s*\|\s*([^\|\n]{3,120})\|",
                r"(?:^|\n)\s*#{1,6}\s*(?:director(?:a)?|coordinador(?:a)?)[^\n]*\n+\s*([^\n|]{3,120})",
                r"(?:^|\n)\s*(?:director(?:a)?|coordinador(?:a)?)[^\n]*\n+\s*([^\n|]{3,120})",
            ],
        )
        director_matches_low = [m for m in director_matches_low if m.lower() not in seen_high]

        if is_program_page and not director_matches_high and not director_matches_low:
            canonical_root = bool(
                re.search(r"/carreras/[^/]+/?$", (url or "").lower())
                or re.search(r"/oferta-academica/[^/]+/?$", (url or "").lower())
            )
            if canonical_root:
                titled = IngestionService._extract_titled_person_lines(content)
                if titled:
                    director_matches_high = [titled[0]]
        if is_program_page:
            base_conf = 0.95 if "/carreras/" in (url or "").lower() else 0.82
            for match in director_matches_high:
                if not IngestionService._is_plausible_authority_person_name(match):
                    continue
                facts.append(
                    {
                        "fact_key": "director",
                        "fact_value": match,
                        "evidence_text": match,
                        "confidence": base_conf,
                    }
                )
            low_conf = 0.75 if "/carreras/" in (url or "").lower() else 0.6
            for match in director_matches_low:
                if not IngestionService._is_plausible_authority_person_name(match):
                    continue
                facts.append(
                    {
                        "fact_key": "director",
                        "fact_value": match,
                        "evidence_text": match,
                        "confidence": low_conf,
                    }
                )

        secretary_matches = IngestionService._extract_fact_matches(
            content,
            [
                r"(?:secretario(?:a)?\s+acad[eé]mic[oa])\s*[:\-]\s*([^\n|]{3,120})",
                r"(?:secretar[ií]a\s+acad[eé]mica|coordinaci[oó]n\s+acad[eé]mica)\s*[:\-]\s*([^\n|]{3,120})",
                r"\|\s*(?:secretario(?:a)?\s+acad[eé]mic[oa])\s*\|\s*([^\|\n]{3,120})\|",
                r"(?:^|\n)\s*#{1,6}\s*(?:secretario(?:a)?\s+acad[eé]mic[oa])\s*\n+\s*([^\n|]{3,120})",
                r"(?:^|\n)\s*(?:secretario(?:a)?\s+acad[eé]mic[oa])\s*\n+\s*([^\n|]{3,120})",
            ],
        )
        if is_program_page and not secretary_matches:
            canonical_root = bool(
                re.search(r"/carreras/[^/]+/?$", (url or "").lower())
                or re.search(r"/oferta-academica/[^/]+/?$", (url or "").lower())
            )
            if canonical_root:
                titled = IngestionService._extract_titled_person_lines(content)
                if len(titled) >= 2:
                    secretary_matches = [titled[1]]
        if is_program_page:
            for match in secretary_matches:
                if not IngestionService._is_plausible_authority_person_name(match):
                    continue
                facts.append(
                    {
                        "fact_key": "secretary_academic",
                        "fact_value": match,
                        "evidence_text": match,
                        "confidence": 0.85 if "/carreras/" in (url or "").lower() else 0.7,
                    }
                )

        duration_matches = IngestionService._extract_fact_matches(
            content,
            [
                r"(?:duraci[oó]n(?:\s+de\s+la\s+carrera)?|duraci[oó]n)\s*[:\-]\s*([^\n]{2,80})",
                r"(?:duraci[oó]n[^\n]{0,30})(\d+\s*(?:a[nñ]os|años)(?:\s+y\s+\d+\s*(?:meses|mes))?)",
            ],
            max_len=80,
        )
        if is_program_page:
            for match in duration_matches:
                if not IngestionService._is_duration_value_plausible(match):
                    continue
                facts.append(
                    {
                        "fact_key": "duration",
                        "fact_value": match,
                        "evidence_text": match,
                        "confidence": 0.88 if "/carreras/" in (url or "").lower() else 0.72,
                    }
                )

        if is_program_page and IngestionService._is_year_subject_fact_source(url, page_type):
            facts.extend(IngestionService._extract_year_subject_facts(url, content))

        return facts

    async def process_and_save(
        self,
        url: str,
        title: str,
        content: str,
        session,
        page_type: str = "institutional_info",
        content_type: str = "html",
        authority_score: float = 0.5,
        original_filename: str | None = None,
        allowed_host_exact: str | None = None,
    ):
        raw_parsed_url = urlparse((url or "").strip())
        raw_host_exact = normalize_host_exact(raw_parsed_url.netloc or raw_parsed_url.hostname or "")
        expected_host_exact = normalize_host_exact(allowed_host_exact or "")
        if expected_host_exact and raw_host_exact != expected_host_exact:
            return {"saved": False, "reason": "host_mismatch"}

        canonical_url = self._canonicalize_url(url)
        clean_content = self._clean_markdown(content or "")
        if not clean_content:
            return {"saved": False, "reason": "empty_content"}
        normalized = clean_content.lower()
        if "página no encontrada" in normalized or "pagina no encontrada" in normalized:
            return {"saved": False, "reason": "not_found_content"}

        # Smart content filtering: news is downranked, not always blocked
        should_index, filter_reason = should_index_page(url, title, clean_content)
        if not should_index:
            return {"saved": False, "reason": filter_reason}

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
                page_type=page_type,
                content_type=content_type,
                authority_score=authority_score,
                original_filename=original_filename,
            )
            session.add(doc)
            await session.flush()
        else:
            doc = existing_doc
            doc.source_id = source.source_id
            doc.url = canonical_url
            doc.title = title
            doc.content_hash = doc_hash
            doc.page_type = page_type
            doc.content_type = content_type
            doc.authority_score = authority_score
            if original_filename:
                doc.original_filename = original_filename
            doc.fetched_at = utc_now_naive()

        # Save full cleaned text — pgai vectorizer handles chunking + embedding
        doc.content = clean_content

        await session.execute(
            delete(ProgramFact)
            .where(ProgramFact.source_id == source.source_id)
            .where(ProgramFact.canonical_url == canonical_url)
        )
        if self._is_fact_source_url(canonical_url, page_type):
            for fact in self._extract_program_facts(
                canonical_url,
                title or "",
                clean_content,
                page_type=page_type,
            ):
                fact_key = str(fact.get("fact_key"))
                derived_program_name = (
                    str(fact.get("program_name") or "").strip()
                    or (
                        str(fact.get("fact_value") or "").strip()
                        if fact_key == "program_name"
                        else self._extract_program_name(canonical_url, title or "", clean_content)
                    )
                    or "__general__"
                )
                session.add(
                    ProgramFact(
                        source_id=source.source_id,
                        canonical_url=canonical_url,
                        program_name=derived_program_name,
                        fact_key=fact_key,
                        fact_value=str(fact.get("fact_value")),
                        evidence_text=(fact.get("evidence_text") or "")[:500],
                        confidence=float(fact.get("confidence") or 0.7),
                    )
                )
        await session.commit()
        return {"saved": True, "reason": "saved" if existing_doc is None else "updated"}

    async def process_pdf_and_save(
        self,
        url: str,
        title: str,
        markdown_content: str,
        session,
        page_type: str = "pdf_document",
        authority_score: float = 0.75,
        original_filename: str | None = None,
        pdf_metadata: dict | None = None,
        allowed_host_exact: str | None = None,
    ):
        """
        Ingest a PDF document that has already been converted to markdown.

        Delegates to process_and_save with content_type="pdf" and adds
        any PDF-specific metadata to chunk metadata.
        """
        return await self.process_and_save(
            url=url,
            title=title,
            content=markdown_content,
            session=session,
            page_type=page_type,
            content_type="pdf",
            authority_score=authority_score,
            original_filename=original_filename,
            allowed_host_exact=allowed_host_exact,
        )
