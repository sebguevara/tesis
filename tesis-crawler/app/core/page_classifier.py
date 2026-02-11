"""
Page Classifier — Assigns page_type and authority_score to URLs and content.

Rule-based classification designed for institutional university websites.
Determines whether a page should be indexed, blocked, or downranked.

ALL news, communications, events, and ephemeral content is BLOCKED.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class ClassificationResult:
    """Result of classifying a page."""

    page_type: str
    authority_score: float
    should_block: bool = False
    reason: str = ""


# ── Classification Rules (ordered by priority) ───────────────────────
# Each rule: (page_type, url_tokens, authority_score)
# First match wins, so order matters.

_BLOCK_RULES: list[tuple[str, ...]] = (
    # WordPress internals
    "/wp-admin/",
    "/wp-login.php",
    "/xmlrpc.php",
    "/wp-json/",
    "/feed/",
    # Navigation noise
    "/tag/",
    "/author/",
    "/category/",
    "/attachment/",
    "attachment_id=",
    # Search / forms
    "/?s=",
    "/search/",
    "?fluentcrm=",
    "/cvm-prop-form/",
    # Pure listing cruft
    "/sitemap",
    "/archives/",
    "/revista/",
    "/revistas/",
    "/viaje/",
    "/viajes/",
    "/publicacion/",
    "/publicaciones/",
    "/simposio/",
    "/simposios/",
    "/jornada/",
    "/jornadas/",
    "/congreso/",
    "/congresos/",
    "/intercambio/",
    "/convocatoria/",
    "/cuaderno-urbano",
)

# ── NEWS / EVENTS / COMMUNICATIONS — always blocked ──────────────────
_NEWS_BLOCK_TOKENS: tuple[str, ...] = (
    "/notimed/", "/noticia/", "/noticias/", "/novedad/", "/novedades/",
    "/prensa/", "/comunicado/", "/comunicados/", "/blog/", "/news/",
    "/eventos/", "/evento/", "/agenda/", "/actualidad/", "/actualidades/",
    "/boletin/", "/newsletter/", "/gacetilla/", "/efemeride/", "/efemerides/",
    "/revista/", "/revistas/", "/viaje/", "/viajes/",
    "/simposio/", "/simposios/", "/jornada/", "/jornadas/",
    "/congreso/", "/congresos/", "/intercambio/", "/convocatoria/",
    "/publicacion/", "/publicaciones/", "/cuaderno-urbano",
)

_CLASSIFICATION_RULES: list[tuple[str, tuple[str, ...], float]] = [
    # ── Highest authority: Career / academic offer pages ──
    ("career_canonical", ("/carreras/",), 1.0),
    ("offer_canonical", ("/oferta-academica/", "/ofertas-acad/", "/ofertas-academicas/"), 0.95),

    # ── Curriculum and study plans ──
    ("curriculum", ("/plan-de-estudios/", "/planes-de-estudio/", "/correlatividades/", "/plan-estudios/"), 0.95),

    # ── Admission and procedures ──
    ("procedure", (
        "/tramites/", "/trámites/", "/ingreso/", "/ingresos/",
        "/inscripcion/", "/inscripciones/", "/admision/", "/admisiones/",
        "/ingresantes/", "/requisitos/",
    ), 0.90),

    # ── Regulations and norms ──
    ("regulation", (
        "/resolucion/", "/resoluciones/", "/reglamento/", "/reglamentos/",
        "/normativa/", "/normativas/", "/disposicion/", "/disposiciones/",
        "/ordenanza/", "/ordenanzas/",
    ), 0.85),

    # ── Authorities and governance ──
    ("authority", (
        "/autoridades/", "/secretaria/", "/secretarias/", "/decanato/",
        "/consejo-directivo/", "/consejo-superior/", "/gobierno/",
        "/decano/", "/vicedecano/",
    ), 0.85),

    # ── Academic units ──
    ("academic_unit", (
        "/catedra/", "/catedras/", "/departamento/", "/departamentos/",
        "/instituto/", "/institutos/", "/area/", "/areas/",
    ), 0.80),

    # ── Student and graduate services ──
    ("student_service", (
        "/bienestar/", "/becas/", "/biblioteca/", "/comedor/",
        "/deportes/", "/extension/", "/extensión/", "/pasantias/",
        "/pasantías/", "/voluntariado/", "/tutoria/", "/tutorias/",
        "/egresados/", "/graduados/", "/posgrado/", "/posgrados/",
        "/doctorado/", "/maestria/", "/maestría/", "/especializacion/",
        "/especialización/",
    ), 0.80),

    # ── Calendar and dates ──
    ("calendar", (
        "/calendario/", "/cronograma/", "/fechas/", "/agenda-academica/",
        "/calendario-academico/",
    ), 0.80),

    # ── Research and academic production ──
    ("research", (
        "/investigacion/", "/investigación/", "/ciencia/",
        "/publicaciones/", "/proyectos/", "/laboratorio/", "/laboratorios/",
    ), 0.75),

    # ── Faculty / staff ──
    ("staff", (
        "/docentes/", "/profesores/", "/personal/", "/nodocentes/",
        "/no-docentes/", "/concurso/", "/concursos/", "/convocatoria/",
    ), 0.75),

    # ── Purchasing and bids (low priority but not blocked) ──
    ("procurement", (
        "/licitaciones/", "/compras/", "/licitaciones-y-compr/",
    ), 0.30),
]


class PageClassifier:
    """Classifies institutional web pages by type and authority."""

    def classify(
        self,
        url: str,
        title: str = "",
        content_preview: str = "",
    ) -> ClassificationResult:
        """
        Classify a URL into a page type with an authority score.

        Parameters
        ----------
        url : str
            The full URL of the page.
        title : str
            The page title (if available).
        content_preview : str
            First ~2000 chars of the page content for hint matching.

        Returns
        -------
        ClassificationResult
        """
        url_lc = (url or "").strip().lower()
        parsed = urlparse(url_lc)
        path = parsed.path or ""

        # ── Phase 1: Check for blocked patterns ──
        for block_token in _BLOCK_RULES:
            if block_token in url_lc:
                return ClassificationResult(
                    page_type="utility_noise",
                    authority_score=0.0,
                    should_block=True,
                    reason=f"blocked:{block_token}",
                )

        # Block root path (homepage is usually just a card index)
        if path in ("", "/"):
            return ClassificationResult(
                page_type="homepage",
                authority_score=0.15,
                should_block=True,
                reason="root_path",
            )

        # ── Phase 2: HARD-BLOCK all news/events/communications ──
        for news_token in _NEWS_BLOCK_TOKENS:
            if news_token in url_lc:
                return ClassificationResult(
                    page_type="news_blocked",
                    authority_score=0.0,
                    should_block=True,
                    reason=f"news_blocked:{news_token}",
                )

        # ── Phase 3: PDF detection ──
        if url_lc.endswith(".pdf") or ".pdf?" in url_lc:
            return self._classify_pdf(url_lc, title)

        # ── Phase 4: Rule-based classification ──
        for page_type, tokens, score in _CLASSIFICATION_RULES:
            if any(token in url_lc for token in tokens):
                return ClassificationResult(
                    page_type=page_type,
                    authority_score=score,
                )

        # ── Phase 5: Fallback — general institutional ──
        return ClassificationResult(
            page_type="institutional_info",
            authority_score=0.55,
        )

    def classify_content(
        self,
        url: str,
        title: str,
        content: str,
    ) -> ClassificationResult:
        """
        Extended classification that also uses content analysis.
        Should be called after content is available (post-scrape).
        Adjusts the initial URL-based classification if content provides
        stronger signals.
        """
        base = self.classify(url, title, content[:2000])

        if base.should_block:
            return base

        # Boost authority if content has strong academic signals
        content_lc = (content or "").lower()[:3000]
        title_lc = (title or "").lower()
        haystack = f"{url.lower()} {title_lc} {content_lc}"

        # career signals boost
        if base.page_type not in ("career_canonical", "offer_canonical"):
            career_signals = ("plan de estudios", "duración de la carrera", "duracion de la carrera",
                              "perfil del egresado", "alcances del título", "alcances del titulo",
                              "campo ocupacional", "incumbencias")
            if sum(1 for s in career_signals if s in haystack) >= 2:
                return ClassificationResult(
                    page_type="career_canonical",
                    authority_score=0.92,
                    reason="content_upgrade:career_signals",
                )

        # procedure signals boost
        if base.page_type not in ("procedure",):
            proc_signals = ("paso a paso", "siu guaraní", "siu guarani",
                            "mesa de entradas", "formulario", "documentación requerida",
                            "documentacion requerida")
            if sum(1 for s in proc_signals if s in haystack) >= 2:
                return ClassificationResult(
                    page_type="procedure",
                    authority_score=max(base.authority_score, 0.85),
                    reason="content_upgrade:procedure_signals",
                )

        return base

    def _classify_pdf(self, url_lc: str, title: str = "") -> ClassificationResult:
        """Classify a PDF URL based on path and filename hints."""
        haystack = f"{url_lc} {(title or '').lower()}"

        # High-value PDF types
        if any(t in haystack for t in ("plan-de-estudio", "plan_de_estudio", "plan de estudio",
                                        "correlatividades", "curriculum", "malla-curricular")):
            return ClassificationResult(page_type="pdf_document", authority_score=0.95)

        if any(t in haystack for t in ("resolucion", "resolución", "reglamento", "normativa",
                                        "ordenanza", "disposicion")):
            return ClassificationResult(page_type="pdf_document", authority_score=0.90)

        if any(t in haystack for t in ("calendario", "cronograma", "agenda-academica")):
            return ClassificationResult(page_type="pdf_document", authority_score=0.85)

        if any(t in haystack for t in ("programa", "syllabus", "catedra", "materia")):
            return ClassificationResult(page_type="pdf_document", authority_score=0.85)

        if any(t in haystack for t in ("ingreso", "admision", "inscripcion", "requisito", "beca")):
            return ClassificationResult(page_type="pdf_document", authority_score=0.85)

        if any(t in haystack for t in ("guia", "guía", "manual", "instructivo", "tutorial")):
            return ClassificationResult(page_type="pdf_document", authority_score=0.80)

        # Default PDF authority score
        return ClassificationResult(page_type="pdf_document", authority_score=0.75)
