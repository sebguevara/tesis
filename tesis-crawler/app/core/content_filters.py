"""
Content Filters — Minimal blocking policy.

Only institutional news/events/communications are blocked.
Everything else should remain indexable.
"""

from __future__ import annotations



# ── URL-based news detection ─────────────────────────────────────────

NEWS_URL_TOKENS = (
    "/notimed", "/noticia", "/noticias", "/novedad", "/novedades",
    "/prensa", "/comunicado", "/comunicados",
    "/blog", "/news", "/agenda", "/evento", "/eventos",
    "/boletin", "/newsletter", "/actualidad", "/actualidades",
    "/gacetilla", "/efemeride", "/efemerides",
)

NEWS_TEXT_HINTS = (
    "área de prensa", "sala de prensa", "novedades", "comunicados",
    "felicitamos", "efemérides", "día mundial", "saludo institucional",
    "gacetilla", "nota de prensa", "comunicado de prensa",
)

def _normalize(value: str) -> str:
    return (value or "").strip().lower()

def is_institutional_news(url: str, title: str, content: str = "") -> bool:
    """
    Returns True if this is news/event/communication content.
    Any True result means the page MUST be blocked — no exceptions.
    """
    url_lc = _normalize(url)
    title_lc = _normalize(title)
    content_lc = _normalize(content)

    # Do not classify canonical/listing career pages as news just because they
    # include labels like "Novedades y Eventos" in cards/tags.
    if any(token in url_lc for token in ("/carreras/", "/category/carreras", "/oferta-academica/", "/ofertas-academicas/")):
        return False

    if any(token in url_lc for token in NEWS_URL_TOKENS):
        return True

    title_hit = any(hint in title_lc for hint in NEWS_TEXT_HINTS)
    content_hit = any(hint in content_lc[:1000] for hint in NEWS_TEXT_HINTS)
    return title_hit or content_hit


def is_non_academic_noise(url: str, title: str, content: str = "") -> bool:
    """Disabled by policy: only news should be blocked."""
    return False


def is_outdated_content(url: str, title: str, content: str = "", max_age_years: int = 1) -> bool:
    """Disabled by policy: only news should be blocked."""
    return False


def should_index_page(
    url: str,
    title: str = "",
    content: str = "",
) -> tuple[bool, str]:
    """
    High-level decision: should a page be indexed?
    Only news/events/communications are blocked.
    """
    if is_institutional_news(url, title, content):
        return False, "news_blocked"

    return True, "standard"
