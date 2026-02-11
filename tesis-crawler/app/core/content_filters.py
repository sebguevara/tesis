"""
Content Filters — Strict filtering for institutional content.

ALL news, communications, press releases, newsletters, events, and
time-sensitive ephemeral content is BLOCKED unconditionally.
Only static academic/institutional content is indexed.
"""

from __future__ import annotations

import re
from datetime import datetime


# ── URL-based news detection ─────────────────────────────────────────

NEWS_URL_TOKENS = (
    "/notimed", "/noticia", "/noticias", "/novedad", "/novedades",
    "/prensa", "/comunicado", "/comunicados",
    "/blog", "/news", "/agenda", "/evento", "/eventos",
    "/tag/", "/category/", "/author/",
    "/boletin", "/newsletter", "/actualidad", "/actualidades",
    "/gacetilla", "/efemeride", "/efemerides",
)

NOISE_URL_TOKENS = (
    "/revista",
    "/revistas",
    "/viaje",
    "/viajes",
    "/simposio",
    "/simposios",
    "/jornada",
    "/jornadas",
    "/congreso",
    "/congresos",
    "/publicacion",
    "/publicaciones",
    "/intercambio",
    "/convocatoria",
    "/cuaderno-urbano",
)

NEWS_TEXT_HINTS = (
    "área de prensa", "sala de prensa", "novedades", "comunicados",
    "felicitamos", "efemérides", "día mundial", "saludo institucional",
    "gacetilla", "nota de prensa", "comunicado de prensa",
)

NOISE_TEXT_HINTS = (
    "revista",
    "revistas",
    "viaje de estudios",
    "intercambio internacional",
    "simposio",
    "jornadas",
    "congreso",
    "publicaciones",
    "cuaderno urbano",
    "convocatoria",
)

YEAR_REGEX = re.compile(r"(?:19|20)\d{2}")


def _normalize(value: str) -> str:
    return (value or "").strip().lower()


def _year_values(value: str) -> list[int]:
    return [int(y) for y in YEAR_REGEX.findall(value)]


def is_institutional_news(url: str, title: str, content: str = "") -> bool:
    """
    Returns True if this is news/event/communication content.
    Any True result means the page MUST be blocked — no exceptions.
    """
    url_lc = _normalize(url)
    title_lc = _normalize(title)
    content_lc = _normalize(content)

    if any(token in url_lc for token in NEWS_URL_TOKENS):
        return True

    title_hit = any(hint in title_lc for hint in NEWS_TEXT_HINTS)
    content_hit = any(hint in content_lc[:1000] for hint in NEWS_TEXT_HINTS)
    return title_hit or content_hit


def is_non_academic_noise(url: str, title: str, content: str = "") -> bool:
    """
    Returns True for content that is not useful for factual academic QA.
    Examples: revistas, viajes, simposios, jornadas, publicaciones editoriales.
    """
    url_lc = _normalize(url)
    title_lc = _normalize(title)
    content_lc = _normalize(content)

    if any(token in url_lc for token in NOISE_URL_TOKENS):
        return True

    title_hit = any(hint in title_lc for hint in NOISE_TEXT_HINTS)
    content_hit = any(hint in content_lc[:900] for hint in NOISE_TEXT_HINTS)
    return title_hit or content_hit


def is_outdated_content(url: str, title: str, content: str = "", max_age_years: int = 2) -> bool:
    """Returns True if the content appears to be outdated based on year references."""
    current_year = datetime.now().year
    min_fresh_year = current_year - max_age_years

    url_years = _year_values(_normalize(url))
    if url_years and max(url_years) < min_fresh_year:
        return True

    content_years = _year_values(_normalize(content[:2000]))
    if content_years and max(content_years) < min_fresh_year:
        return True

    return False


def should_index_page(
    url: str,
    title: str = "",
    content: str = "",
) -> tuple[bool, str]:
    """
    High-level decision: should a page be indexed?

    Returns (should_index, reason).
    News/events/communications are ALWAYS blocked.
    Outdated content is blocked.
    Everything else is indexed.
    """
    # Hard-block ALL news, events, communications.
    # If a news item is also old, label it explicitly as outdated_news.
    if is_institutional_news(url, title, content):
        if is_outdated_content(url, title, content):
            return False, "outdated_news"
        return False, "news_blocked"

    if is_non_academic_noise(url, title, content):
        return False, "non_academic_noise"

    return True, "standard"
