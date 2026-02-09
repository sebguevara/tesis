from __future__ import annotations
import re
from datetime import datetime

NEWS_URL_TOKENS = (
    "/notimed", "/noticia", "/novedad", "/prensa", "/comunicado", 
    "/blog", "/news", "/agenda", "/evento", "/tag/", "/category/", "/author/"
)

NEWS_TEXT_HINTS = (
    "área de prensa", "sala de prensa", "novedades", "comunicados", 
    "felicitamos", "efemérides", "día mundial", "saludo institucional"
)

ADMISSION_PRIORITY_HINTS = (
    "admisión",
    "admision",
    "ingresante",
    "ingresantes",
    "ingreso",
    "inscripción",
    "inscripcion",
    "requisitos de ingreso",
    "paso a paso",
    "siu guaraní",
    "siu guarani",
    "plan de estudios",
)

CONDITIONAL_PRIORITY_HINTS = (
    "documentación",
    "documentacion",
    "resolución",
    "resolucion",
)

PRIORITY_CONTEXT_HINTS = (
    "admisión",
    "admision",
    "ingreso",
    "ingresante",
    "inscripción",
    "inscripcion",
    "plan de estudios",
    "correlatividades",
    "carrera",
    "carreras",
)

YEAR_REGEX = re.compile(r"(?:19|20)\d{2}")

def _normalize(value: str) -> str:
    return (value or "").strip().lower()

def _year_values(value: str) -> list[int]:
    return [int(y) for y in YEAR_REGEX.findall(value)]


def _is_priority_academic_content(url: str, title: str, content: str = "") -> bool:
    url_lc = _normalize(url)
    title_lc = _normalize(title)
    content_lc = _normalize(content[:2000])
    full_text = " ".join((url_lc, title_lc, content_lc))

    if any(h in full_text for h in ADMISSION_PRIORITY_HINTS):
        return True

    if any(h in full_text for h in CONDITIONAL_PRIORITY_HINTS):
        return any(ctx in full_text for ctx in PRIORITY_CONTEXT_HINTS)

    return False

def is_institutional_news(url: str, title: str, content: str = "") -> bool:
    url_lc = _normalize(url)
    title_lc = _normalize(title)
    content_lc = _normalize(content)
    
    if _is_priority_academic_content(url, title, content):
        return False
        
    if any(token in url_lc for token in NEWS_URL_TOKENS):
        return True

    title_hit = any(hint in title_lc for hint in NEWS_TEXT_HINTS)
    content_hit = any(hint in content_lc[:1000] for hint in NEWS_TEXT_HINTS)
    return title_hit and content_hit

def is_outdated_content(url: str, title: str, content: str = "", max_age_years: int = 2) -> bool:
    current_year = datetime.now().year
    min_fresh_year = current_year - max_age_years
    
    url_lc = _normalize(url)
    title_lc = _normalize(title)
    content_lc = _normalize(content[:2000])

    if _is_priority_academic_content(url, title, content):
        return False

    url_years = _year_values(url)
    if url_years and max(url_years) < min_fresh_year:
        return True

    if is_institutional_news(url, title, content):
        content_years = _year_values(content_lc)
        if content_years and max(content_years) < min_fresh_year:
            return True

    return False
