"""
RAGService — pipeline RAG en 4 nodos (Etapas 3 + 4):

    rewrite → retrieve → generate → verify

  • rewrite (gpt-4o-mini): si hay history, reescribe la query como autocontenida.
  • retrieve: hybrid (pgvector cosine top-K + Postgres FTS top-K) → RRF → cross-encoder.
  • generate (OPENAI_CHAT_MODEL): respuesta breve con SYSTEM_RAG anti-alucinación.
  • verify (gpt-4o-mini): groundedness 0..1; si < 0.6 reemplaza por decline.

Pre-Etapa 3 había ~4000 líneas de fast-paths regex / URL hints / fact extraction /
live-fetch / SIMPLE_RETRIEVAL_MODE / etc. Quedaron documentados en docs/RAG_ITERATIONS.md
y borrados en Etapa 5 — el git log preserva las versiones anteriores.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, TypedDict
from uuid import UUID

from langgraph.graph import END, StateGraph
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from openai import AsyncOpenAI
from sqlalchemy import text

from app.config import settings
from app.core.domain_utils import domain_variants, normalize_domain
from app.core.reranker import rerank as cross_encoder_rerank
from app.embedding.models import EMBEDDING_DIM
from app.llm.prompts import (
    REWRITE_QUERY_SYSTEM,
    REWRITE_QUERY_USER,
    VERIFY_GROUNDEDNESS_SYSTEM,
    VERIFY_GROUNDEDNESS_USER,
    site_facts_for,
    system_rag_for,
)
from app.storage.db_client import async_session


logger = logging.getLogger(__name__)


EMBEDDING_MODEL = "text-embedding-3-large"


def _vec_to_pg_literal(vec) -> str:
    """Format a Python sequence as a pgvector literal: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{float(x):.7f}" for x in vec) + "]"


class AgentState(TypedDict, total=False):
    query: str
    context: List[str]
    response: str
    history: List[str]
    source_id: str | None
    session_state: Dict[str, Any] | None
    # Stage 4 additions
    resolved_query: str
    groundedness: float
    unsupported_claims: List[str]
    site_domain: str


# Per-site regex for the canonical "one page per career" URLs. When a site has
# one, retrieval runs an extra dense lookup scoped to those pages and expands
# their neighbor chunks (plans of study are split across chunks). Sites without
# an entry fall back to plain hybrid retrieval + neighbor expansion of the top hits.
PROGRAM_URL_PATTERNS: dict[str, str] = {
    "med.unne.edu.ar": r"/carreras/[^/]+$",
}


def program_url_pattern_for(domain: str) -> str | None:
    d = (domain or "").lower().removeprefix("www.")
    return PROGRAM_URL_PATTERNS.get(d)


class RAGService:
    NO_INFO_RESPONSE = "No tengo información referente a eso."
    VERIFY_NO_EVIDENCE_RESPONSE = (
        "No encontré evidencia suficiente en el sitio para responder con seguridad. "
        "¿Podés reformular la pregunta o ser más específico (carrera, año, trámite)?"
    )

    # Hybrid retrieval (Stage 3, refined in Stage 5; Stage 7 widened the pools
    # so multi-item "listing" queries — "¿qué carreras hay?", "¿qué secretarías
    # hay?" — surface every distinct page instead of clustering on the 2-3 most
    # similar. The cross-encoder top was 14: enough for a single page's answer,
    # too tight when the answer spans 5+ separate institutional pages.)
    HYBRID_TOP_K_PER_LIST = 60
    HYBRID_RRF_C = 60
    HYBRID_RRF_TOP = 72
    HYBRID_FINAL_TOP = 18
    # Stage 6: expand same-doc neighbors for canonical /carreras/ chunks. The
    # plan-de-estudios pages split a single year across "primer semestre" and
    # "segundo semestre" chunks; embedding for "materias del primer año" tends
    # to bring only one. Pull adjacent chunks (by ordinal chunk_id) for top
    # matches that come from /carreras/ URLs so the LLM sees the full year.
    NEIGHBOR_EXPANSION_RADIUS = 3
    NEIGHBOR_EXPANSION_TOP = 4

    # Prompt sizing
    # gpt-4o accepts ~128k tokens; staying well under that. Stage 6: bumped
    # because SYSTEM_RAG grew and neighbor expansion adds chunks — the old
    # 22000 char cap caused naive truncation to chop off the user question
    # at the END of the prompt, producing empty "Hola, en qué te ayudo?"
    # answers. Now the assembly preserves the question verbatim.
    # Stage 7: kept at the proven 22000/32000. Bumping to 28000/38000 widened
    # listing recall slightly but ~tripled latency (both generate AND verify run
    # gpt-4o over the full context). The career-diversification + wider candidate
    # pools below recover listing completeness without paying that latency.
    MAX_CONTEXT_CHARS_TOTAL = 22000
    MAX_PROMPT_CHARS = 32000
    MAX_HISTORY_ITEMS = 12
    MAX_HISTORY_CHARS_PER_ITEM = 500
    MAX_HISTORY_CHARS_TOTAL = 5000

    # Rewrite + verify (Stage 4)
    REWRITE_HISTORY_MAX_ITEMS = 6
    REWRITE_HISTORY_MAX_CHARS = 1500
    # Stage 6: 0.6 → 0.5 → 0.4. The verifier prompt reserves 0.4–0.6 for
    # "1 minor data point not in context" — those answers are still useful
    # to the user. Below 0.4 (multiple unsupported claims, contradictions,
    # out-of-scope) we still decline.
    VERIFY_GROUNDEDNESS_THRESHOLD = 0.4

    def __init__(self):
        # Stage 5 refinement: temperature=0 for reproducibility. Without this,
        # the same query can land on "presencial" one time and "presencial y
        # virtual" the next, making the eval results non-deterministic.
        self.llm = ChatOpenAI(
            model=settings.OPENAI_CHAT_MODEL,
            api_key=settings.OPENAI_API_KEY,
            temperature=0,
            timeout=float(getattr(settings, "RAG_LLM_TIMEOUT_SECONDS", 18)),
            max_retries=int(getattr(settings, "RAG_LLM_MAX_RETRIES", 1)),
        )
        self.embedder = OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            api_key=settings.OPENAI_API_KEY,
            dimensions=EMBEDDING_DIM,
        )
        # Stage 4 helper model for cheap nodes (rewrite). gpt-4o-mini is enough.
        self.helper_model = str(getattr(settings, "OPENAI_CONTEXT_MODEL", "gpt-4o-mini"))
        # Stage 5 refinement: verify uses a stronger model (gpt-4o) — gpt-4o-mini
        # was too lax with out-of-scope / medical advice cases.
        self.verify_model = str(getattr(settings, "OPENAI_VERIFY_MODEL", "gpt-4o"))
        self._openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    # ── Public API ───────────────────────────────────────────────────────

    def build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("rewrite", self.rewrite)
        workflow.add_node("retrieve", self.retrieve)
        workflow.add_node("generate", self.generate)
        workflow.add_node("verify", self.verify)
        workflow.set_entry_point("rewrite")
        workflow.add_edge("rewrite", "retrieve")
        workflow.add_edge("retrieve", "generate")
        workflow.add_edge("generate", "verify")
        workflow.add_edge("verify", END)
        return workflow.compile()

    def derive_session_state(
        self,
        *,
        current_state: dict | None,
        query: str,  # noqa: ARG002 — kept for API compat with widget/query routes
        history: list[str],  # noqa: ARG002
    ) -> dict:
        """
        Stage 3+ no longer relies on a heuristic session_state (active_program,
        active_year, active_intent). The rewrite node uses the raw history with
        an LLM. Kept as a no-op for API compatibility with widget.py/query.py.
        """
        return dict(current_state or {})

    # ── Helpers (used by the 4 nodes) ────────────────────────────────────

    async def _embed_query(self, query: str) -> str:
        """Embed the query and return a pgvector literal ready for SQL casting."""
        vec = await self.embedder.aembed_query(query or "")
        return _vec_to_pg_literal(vec)

    async def _resolve_source_scope(self, source_id: str) -> tuple[str, str, str] | None:
        """Return (domain_variant_1, domain_variant_2, source_url) for a given source_id."""
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
    def _clip_text(value: str, max_chars: int) -> str:
        if not value:
            return ""
        if len(value) <= max_chars:
            return value
        return value[: max(0, max_chars - 1)].rstrip() + "…"

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

    @staticmethod
    def _rrf_fuse(
        ranked_lists: list[list[str]],
        c: int = HYBRID_RRF_C,
        top_n: int = HYBRID_RRF_TOP,
    ) -> list[str]:
        """Reciprocal Rank Fusion: each item's score = sum of 1/(c + rank) across lists."""
        scores: dict[str, float] = {}
        for items in ranked_lists:
            for rank, item_id in enumerate(items):
                scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (c + rank + 1)
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [k for k, _ in ordered[:top_n]]

    async def _helper_json_call(
        self,
        system: str,
        user: str,
        max_tokens: int = 200,
        model: str | None = None,
        temperature: float = 0,
    ) -> dict:
        """Single JSON-mode call. Defaults to helper_model (gpt-4o-mini)."""
        try:
            resp = await self._openai_client.chat.completions.create(
                model=model or self.helper_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
            )
            raw = (resp.choices[0].message.content or "").strip()
            if not raw:
                return {}
            return json.loads(raw)
        except Exception:
            logger.exception("helper json call failed")
            return {}

    @staticmethod
    def _looks_like_no_info_response(response: str) -> bool:
        """True when the answer already declined or asked for clarification."""
        if not response:
            return True
        low = response.lower()
        markers = (
            "no tengo información",
            "no tengo informacion",
            "no encontré",
            "no encontre",
            "no llegué a",
            "no llegue a",
            "para responder bien",
            "decime la carrera",
            "decime primero la carrera",
            "podés reformular",
            "podes reformular",
            "podrías especificar",
            "podrias especificar",
            "podrías indicar",
            "podrias indicar",
            "necesito más detalles",
            "necesito mas detalles",
            "necesitaría que aclares",
            "necesitaria que aclares",
            "no puedo brindar información",
            "no puedo brindar informacion",
            "no puedo proporcionar",
            "lamentablemente no",
            "lo siento, pero no",
            RAGService.NO_INFO_RESPONSE.lower(),
        )
        return any(m in low for m in markers)

    # ── Graph nodes ──────────────────────────────────────────────────────

    async def rewrite(self, state: AgentState):
        """If there is history, rewrite the query as a self-contained question."""
        query = (state.get("query") or "").strip()
        history = list(state.get("history") or [])
        if not query:
            return {"resolved_query": ""}
        if not history:
            return {"resolved_query": query}

        recent = history[-self.REWRITE_HISTORY_MAX_ITEMS :]
        joined_history = "\n".join(recent)
        if len(joined_history) > self.REWRITE_HISTORY_MAX_CHARS:
            joined_history = joined_history[-self.REWRITE_HISTORY_MAX_CHARS :]

        payload = await self._helper_json_call(
            REWRITE_QUERY_SYSTEM,
            REWRITE_QUERY_USER.format(history=joined_history, current=query),
            max_tokens=160,
        )
        rewritten = (payload.get("query") or "").strip()
        if not rewritten:
            return {"resolved_query": query}
        if len(rewritten) > 400:
            rewritten = rewritten[:400].rsplit(" ", 1)[0]
        return {"resolved_query": rewritten}

    async def retrieve(self, state: AgentState):
        """Hybrid retrieval: dense + sparse → RRF → cross-encoder reranker → top-8."""
        query = (state.get("resolved_query") or state.get("query") or "").strip()
        source_id = (state.get("source_id") or "").strip()
        if not query or not source_id:
            return {"context": []}
        try:
            UUID(source_id)
        except ValueError:
            return {"context": []}

        scope = await self._resolve_source_scope(source_id)
        if not scope:
            return {"context": []}
        domain_1, domain_2, _source_url = scope
        program_re = program_url_pattern_for(domain_1)

        try:
            query_vec = await self._embed_query(query)
        except Exception:
            logger.exception("Embedding query failed")
            return {"context": []}

        # Dense (pgvector cosine) + sparse (FTS spanish), both joining
        # chunks → documents → sources. We pull text + (Stage 2) context column
        # so the reranker scores the situated chunk and the LLM sees the same.
        vector_sql = text(
            """
            SELECT
              c.id::text AS cid,
              d.canonical_url AS url,
              COALESCE(d.title, '') AS title,
              c.text AS chunk_text,
              COALESCE(c.context, '') AS chunk_context,
              d.fetched_at AS fetched_at
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            JOIN sources s ON s.source_id = d.source_id
            WHERE (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
              AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> CAST(:query_vec AS vector)
            LIMIT :k
            """
        )
        fts_sql = text(
            """
            SELECT
              c.id::text AS cid,
              d.canonical_url AS url,
              COALESCE(d.title, '') AS title,
              c.text AS chunk_text,
              COALESCE(c.context, '') AS chunk_context,
              d.fetched_at AS fetched_at
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            JOIN sources s ON s.source_id = d.source_id
            WHERE (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
              AND to_tsvector('spanish', COALESCE(d.title, '') || ' ' || c.text)
                  @@ websearch_to_tsquery('spanish', :q)
            ORDER BY ts_rank(
                to_tsvector('spanish', COALESCE(d.title, '') || ' ' || c.text),
                websearch_to_tsquery('spanish', :q)
            ) DESC
            LIMIT :k
            """
        )

        # Stage 6: a parallel dense lookup scoped to canonical /carreras/ pages.
        # Without this the embedder can miss the carrera-home chunks (heavy
        # boilerplate / TOC), and queries like "es presencial Medicina?",
        # "duración Enfermería", "quién es la decana?" land on tangential pages
        # and the model either declines or hallucinates names.
        career_vector_sql = text(
            """
            SELECT
              c.id::text AS cid,
              d.canonical_url AS url,
              COALESCE(d.title, '') AS title,
              c.text AS chunk_text,
              COALESCE(c.context, '') AS chunk_context,
              d.fetched_at AS fetched_at
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            JOIN sources s ON s.source_id = d.source_id
            WHERE (lower(s.domain) = :domain_1 OR lower(s.domain) = :domain_2)
              AND d.canonical_url ~ :program_re
              AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> CAST(:query_vec AS vector)
            LIMIT :k
            """
        )
        async with async_session() as session:
            vec_rows = (
                await session.execute(
                    vector_sql,
                    {
                        "query_vec": query_vec,
                        "k": self.HYBRID_TOP_K_PER_LIST,
                        "domain_1": domain_1,
                        "domain_2": domain_2,
                    },
                )
            ).mappings().all()
            try:
                fts_rows = (
                    await session.execute(
                        fts_sql,
                        {
                            "q": query,
                            "k": self.HYBRID_TOP_K_PER_LIST,
                            "domain_1": domain_1,
                            "domain_2": domain_2,
                        },
                    )
                ).mappings().all()
            except Exception:
                logger.exception("FTS query failed; continuing with dense only")
                fts_rows = []
            career_rows = []
            if program_re:
                try:
                    career_rows = (
                        await session.execute(
                            career_vector_sql,
                            {
                                "query_vec": query_vec,
                                "k": 24,
                                "domain_1": domain_1,
                                "domain_2": domain_2,
                                "program_re": program_re,
                            },
                        )
                    ).mappings().all()
                except Exception:
                    logger.exception("Career-scoped retrieval failed; continuing without")
                    career_rows = []

        by_cid: dict[str, dict] = {}
        for row in [*vec_rows, *fts_rows, *career_rows]:
            cid = str(row.get("cid") or "")
            if not cid or cid in by_cid:
                continue
            by_cid[cid] = dict(row)

        if not by_cid:
            return {"context": [], "site_domain": domain_1}

        # RRF fuses three lists: dense, sparse, and the carrera-scoped dense
        # results. The third list is what makes carrera-home chunks survive
        # past the rerank for short ambiguous queries.
        fused_cids = self._rrf_fuse(
            [
                [str(r.get("cid") or "") for r in vec_rows if r.get("cid")],
                [str(r.get("cid") or "") for r in fts_rows if r.get("cid")],
                [str(r.get("cid") or "") for r in career_rows if r.get("cid")],
            ],
            c=self.HYBRID_RRF_C,
            top_n=self.HYBRID_RRF_TOP,
        )

        # Build candidates — context + chunk text together so the reranker
        # scores the situated content (Stage 2's contextual retrieval).
        candidates: list[tuple[str, str]] = []
        for cid in fused_cids:
            row = by_cid.get(cid)
            if not row:
                continue
            ctx_part = (row.get("chunk_context") or "").strip()
            chunk_part = (row.get("chunk_text") or "").strip()
            if not chunk_part:
                continue
            # The page title is what tells apart near-duplicate pages (course
            # cohorts, "curso" vs "diplomatura" of the same name); without it the
            # reranker picked the wrong cohort's prices and dates.
            title_part = (row.get("title") or "").strip()
            text_for_rerank = "\n\n".join(p for p in (title_part, ctx_part, chunk_part) if p)
            candidates.append((cid, text_for_rerank))

        if not candidates:
            return {"context": []}

        try:
            ranked = await cross_encoder_rerank(
                query,
                [c[1] for c in candidates],
                top_k=self.HYBRID_FINAL_TOP,
            )
        except Exception:
            logger.exception("Cross-encoder rerank failed; using RRF order as fallback")
            ranked = [(i, 0.0) for i in range(min(self.HYBRID_FINAL_TOP, len(candidates)))]

        # Stage 6: pull same-doc neighbors for the top-N matches that come from
        # canonical /carreras/ URLs. The /carreras/ pages split a year across
        # multiple chunks (primer semestre, segundo semestre, etc.); without
        # neighbor expansion the LLM sees only one and answers half the year.
        seed_cids: list[str] = []
        for idx, _score in ranked[: self.NEIGHBOR_EXPANSION_TOP]:
            cid = candidates[idx][0]
            row = by_cid.get(cid) or {}
            url = (row.get("url") or "").strip().lower()
            if not program_re or re.search(program_re, url):
                seed_cids.append(cid)

        if seed_cids:
            neighbor_sql = text(
                """
                WITH seeds AS (
                    SELECT doc_id, chunk_id
                    FROM chunks
                    WHERE id::text = ANY(:seed_cids)
                )
                SELECT DISTINCT
                  c.id::text AS cid,
                  d.canonical_url AS url,
                  COALESCE(d.title, '') AS title,
                  c.text AS chunk_text,
                  COALESCE(c.context, '') AS chunk_context,
                  d.fetched_at AS fetched_at,
                  c.chunk_id
                FROM chunks c
                JOIN documents d ON d.doc_id = c.doc_id
                JOIN seeds s ON s.doc_id = c.doc_id
                WHERE ABS(c.chunk_id - s.chunk_id) <= :radius
                ORDER BY c.chunk_id
                """
            )
            try:
                async with async_session() as session:
                    neighbor_rows = (
                        await session.execute(
                            neighbor_sql,
                            {
                                "seed_cids": seed_cids,
                                "radius": self.NEIGHBOR_EXPANSION_RADIUS,
                            },
                        )
                    ).mappings().all()
                for row in neighbor_rows:
                    cid = str(row.get("cid") or "")
                    if cid and cid not in by_cid:
                        by_cid[cid] = dict(row)
            except Exception:
                logger.exception("Neighbor expansion failed; continuing without")
                neighbor_rows = []

        # Build the final context list. ORDER MATTERS: verify() truncates
        # context to a fixed char budget, so anything past the cutoff is
        # invisible to the verifier. We put /carreras/ chunks FIRST so the
        # canonical institutional info (modalidad, autoridades, duración,
        # plan de estudios) is always in the verifier's window.
        seen_cids: set[str] = set()
        contexts: list[str] = []

        def _format(row: dict) -> str:
            url = (row.get("url") or "").strip()
            title = (row.get("title") or "").strip()
            ctx_part = (row.get("chunk_context") or "").strip()
            chunk_part = (row.get("chunk_text") or "").strip()
            fetched = row.get("fetched_at")
            fetched_str = str(fetched) if fetched is not None else ""
            content_block = f"{ctx_part}\n\n{chunk_part}".strip() if ctx_part else chunk_part
            return f"URL: {url}\nTitulo: {title}\nFetchedAt: {fetched_str}\nContenido: {content_block}"

        # 1) Force-include carrera-scoped matches, DIVERSIFIED BY URL. A plain
        #    top-N clusters on the 1-2 most similar carreras and silently drops
        #    the rest — e.g. "¿qué carreras hay?" answering only Medicina +
        #    Enfermería while Kinesiología/Obstetricia exist. Pass 1 takes the
        #    best chunk of each DISTINCT /carreras/ page (career_rows is ordered
        #    by similarity, so the first hit per URL is its best); pass 2 fills
        #    the remaining budget with the next-best chunks for depth.
        # Same-doc neighbor windows go right where their seed lands, in chunk
        # order. Appended at the end they were cut by the 22000-char budget, so
        # "materias de primer año" saw only the first-semester chunk.
        windows: dict[str, list[dict]] = {}
        if seed_cids:
            for row in neighbor_rows:
                windows.setdefault((row.get("url") or "").strip().lower(), []).append(dict(row))
        seed_set = set(seed_cids)

        def _emit(row: dict) -> bool:
            cid = str(row.get("cid") or "")
            if not cid or cid in seen_cids:
                return False
            url = (row.get("url") or "").strip().lower()
            if cid in seed_set and url in windows:
                # One block of raw consecutive page text, without the per-chunk
                # LLM context: those labels can be wrong at boundaries (the chunk
                # after "#### Primer Año … SEGUNDO SEMESTRE" got "segundo año")
                # and the model trusted them over the page's own headings.
                parts = []
                for nrow in sorted(windows.pop(url), key=lambda r: r.get("chunk_id") or 0):
                    ncid = str(nrow.get("cid") or "")
                    if ncid and ncid not in seen_cids:
                        seen_cids.add(ncid)
                        parts.append((nrow.get("chunk_text") or "").strip())
                if parts:
                    contexts.append(
                        f"URL: {(row.get('url') or '').strip()}\n"
                        f"Titulo: {(row.get('title') or '').strip()}\n"
                        f"Contenido (texto consecutivo de la página, en orden):\n" + "\n".join(parts)
                    )
            if cid not in seen_cids:
                seen_cids.add(cid)
                contexts.append(_format(row))
            return True

        CAREER_FORCE_CAP = 8
        career_added = 0
        career_urls_seen: set[str] = set()
        # Only force the career block when the reranker itself judged a career
        # page relevant; otherwise it filled the head of the context for every
        # query (course prices, cohort dates) and pushed the right page out.
        ranked_cids = {candidates[idx][0] for idx, _ in ranked}
        if not any(str(r.get("cid") or "") in ranked_cids for r in career_rows):
            career_rows = []
        for row in career_rows:  # pass 1 — one chunk per distinct career URL
            if career_added >= CAREER_FORCE_CAP:
                break
            url = (row.get("url") or "").strip().lower()
            if url in career_urls_seen:
                continue
            if _emit(dict(row)):
                career_urls_seen.add(url)
                career_added += 1
        for row in career_rows:  # pass 2 — fill remaining budget, next-best
            if career_added >= CAREER_FORCE_CAP:
                break
            if _emit(dict(row)):
                career_added += 1

        # 2) Cross-encoder reranked top from the hybrid pool (seeds expand inline).
        for idx, _score in ranked:
            _emit(by_cid.get(candidates[idx][0]) or {})

        return {"context": contexts, "site_domain": domain_1}

    async def generate(self, state: AgentState):
        """LLM generation over the retrieved context. The user-facing prompt always
        shows state["query"] (not the rewrite) so the answer references what was asked.
        """
        query = (state.get("query") or "").strip()
        contexts = list(state.get("context") or [])
        history = list(state.get("history") or [])

        if not contexts:
            return {"response": self.NO_INFO_RESPONSE}

        joined: list[str] = []
        running = 0
        for block in contexts:
            block = (block or "").strip()
            if not block:
                continue
            if running + len(block) > self.MAX_CONTEXT_CHARS_TOTAL:
                break
            joined.append(block)
            running += len(block)

        history_text = "\n".join(self._history_for_prompt(history)).strip()
        context_text = "\n\n---\n\n".join(joined)

        # Build prompt with the user's question protected: if total exceeds
        # MAX_PROMPT_CHARS, trim the CONTEXT (least critical) instead of the
        # tail (which would cut the question and leave the model unanchored).
        question_block = f"\n\nPregunta del usuario:\n{query}"
        prefix = (
            f"{system_rag_for(state.get('site_domain') or '')}\n\n"
            f"Historial reciente:\n{history_text}\n\n"
            f"Contexto recuperado:\n"
        )
        budget = self.MAX_PROMPT_CHARS - len(prefix) - len(question_block)
        if budget < 1000:
            # Pathological: SYSTEM_RAG + history alone exhausted the budget.
            # Keep at least a minimal context window.
            budget = 1000
        if len(context_text) > budget:
            context_text = context_text[: budget - 1].rstrip() + "…"
        prompt = prefix + context_text + question_block

        res = await self.llm.ainvoke(prompt)
        return {"response": (res.content or "").strip()}

    async def verify(self, state: AgentState):
        """Replace the response with a decline message when groundedness < threshold."""
        response = (state.get("response") or "").strip()
        question = (state.get("query") or "").strip()
        contexts = list(state.get("context") or [])

        if not response or not question:
            return {}
        if self._looks_like_no_info_response(response):
            return {"groundedness": 1.0, "unsupported_claims": []}

        joined = "\n\n---\n\n".join(b for b in contexts if b)
        # Stage 6: verify must see the same context generate used. Earlier
        # the verify cap was 14000 while generate ran with up to 22000 — when
        # generate listed materias optativas or curso details from chunks
        # past 14000 chars, verify couldn't find them and flagged them as
        # ungrounded. Match the cap to MAX_CONTEXT_CHARS_TOTAL.
        if len(joined) > self.MAX_CONTEXT_CHARS_TOTAL:
            joined = joined[: self.MAX_CONTEXT_CHARS_TOTAL] + "\n[…]"
        if not joined.strip():
            joined = "(sin contexto recuperado)"

        verify_prompt = VERIFY_GROUNDEDNESS_USER.format(
            question=question,
            context=joined,
            answer=response,
            site_facts=site_facts_for(state.get("site_domain") or "") or "  (ninguno)",
        )

        async def _judge(temperature: float = 0) -> tuple[float, dict]:
            payload = await self._helper_json_call(
                VERIFY_GROUNDEDNESS_SYSTEM, verify_prompt, max_tokens=300,
                model=self.verify_model, temperature=temperature,
            )
            try:
                s = float(payload.get("groundedness", 1.0))
            except (TypeError, ValueError):
                s = 1.0
            return max(0.0, min(1.0, s)), payload

        score, payload = await _judge()
        if score < self.VERIFY_GROUNDEDNESS_THRESHOLD:
            # Second opinion before discarding an answer: in the evals the verifier
            # occasionally scored a grounded answer 0.0 (the same answer got 1.0 on
            # re-run); a real hallucination scores low both times.
            score2, payload2 = await _judge(temperature=0.7)
            if score2 > score:
                score, payload = score2, payload2
        unsupported = payload.get("unsupported_claims") or []
        if not isinstance(unsupported, list):
            unsupported = []

        logger.info(
            "verify: groundedness=%.2f unsupported=%r question=%r",
            score, [str(c)[:120] for c in unsupported[:3]], question[:80],
        )
        if score < self.VERIFY_GROUNDEDNESS_THRESHOLD:
            return {
                "response": self.VERIFY_NO_EVIDENCE_RESPONSE,
                "groundedness": score,
                "unsupported_claims": [str(c)[:200] for c in unsupported[:5]],
            }
        return {
            "groundedness": score,
            "unsupported_claims": [str(c)[:200] for c in unsupported[:5]],
        }
