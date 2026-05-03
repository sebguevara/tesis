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
    SYSTEM_RAG,
    VERIFY_GROUNDEDNESS_SYSTEM,
    VERIFY_GROUNDEDNESS_USER,
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


class RAGService:
    NO_INFO_RESPONSE = "No tengo información referente a eso."
    VERIFY_NO_EVIDENCE_RESPONSE = (
        "No encontré evidencia suficiente en el sitio para responder con seguridad. "
        "¿Podés reformular la pregunta o ser más específico (carrera, año, trámite)?"
    )

    # Hybrid retrieval (Stage 3, refined in Stage 5)
    HYBRID_TOP_K_PER_LIST = 30
    HYBRID_RRF_C = 60
    HYBRID_RRF_TOP = 50
    # Stage 5: 8 → 12 to give listing-style queries (materias, secretarías,
    # trámites) more chances of bringing all expected items into the prompt.
    # Trade-off: a couple more reranker scores per query (~50ms each), and
    # ~4 extra context blocks for the LLM. Safe within MAX_CONTEXT_CHARS_TOTAL.
    HYBRID_FINAL_TOP = 12

    # Prompt sizing
    MAX_CONTEXT_CHARS_TOTAL = 24000
    MAX_PROMPT_CHARS = 22000
    MAX_HISTORY_ITEMS = 12
    MAX_HISTORY_CHARS_PER_ITEM = 500
    MAX_HISTORY_CHARS_TOTAL = 5000

    # Rewrite + verify (Stage 4)
    REWRITE_HISTORY_MAX_ITEMS = 6
    REWRITE_HISTORY_MAX_CHARS = 1500
    VERIFY_GROUNDEDNESS_THRESHOLD = 0.6

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
    ) -> dict:
        """Single JSON-mode call. Defaults to helper_model (gpt-4o-mini)."""
        try:
            resp = await self._openai_client.chat.completions.create(
                model=model or self.helper_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0,
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

        by_cid: dict[str, dict] = {}
        for row in [*vec_rows, *fts_rows]:
            cid = str(row.get("cid") or "")
            if not cid or cid in by_cid:
                continue
            by_cid[cid] = dict(row)

        if not by_cid:
            return {"context": []}

        fused_cids = self._rrf_fuse(
            [
                [str(r.get("cid") or "") for r in vec_rows if r.get("cid")],
                [str(r.get("cid") or "") for r in fts_rows if r.get("cid")],
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
            text_for_rerank = f"{ctx_part}\n\n{chunk_part}".strip() if ctx_part else chunk_part
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

        contexts: list[str] = []
        for idx, _score in ranked:
            cid = candidates[idx][0]
            row = by_cid.get(cid) or {}
            url = (row.get("url") or "").strip()
            title = (row.get("title") or "").strip()
            ctx_part = (row.get("chunk_context") or "").strip()
            chunk_part = (row.get("chunk_text") or "").strip()
            fetched = row.get("fetched_at")
            fetched_str = str(fetched) if fetched is not None else ""
            content_block = f"{ctx_part}\n\n{chunk_part}".strip() if ctx_part else chunk_part
            contexts.append(
                f"URL: {url}\nTitulo: {title}\nFetchedAt: {fetched_str}\nContenido: {content_block}"
            )

        return {"context": contexts}

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

        prompt = (
            f"{SYSTEM_RAG}\n\n"
            f"Historial reciente:\n{history_text}\n\n"
            f"Contexto recuperado:\n{context_text}\n\n"
            f"Pregunta del usuario:\n{query}"
        )
        if len(prompt) > self.MAX_PROMPT_CHARS:
            prompt = prompt[: self.MAX_PROMPT_CHARS]

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
        if len(joined) > 6000:
            joined = joined[:6000] + "\n[…]"
        if not joined.strip():
            joined = "(sin contexto recuperado)"

        payload = await self._helper_json_call(
            VERIFY_GROUNDEDNESS_SYSTEM,
            VERIFY_GROUNDEDNESS_USER.format(
                question=question,
                context=joined,
                answer=response,
            ),
            max_tokens=300,
            model=self.verify_model,
        )
        try:
            score = float(payload.get("groundedness", 1.0))
        except (TypeError, ValueError):
            score = 1.0
        score = max(0.0, min(1.0, score))
        unsupported = payload.get("unsupported_claims") or []
        if not isinstance(unsupported, list):
            unsupported = []

        logger.info(
            "verify: groundedness=%.2f unsupported=%d question=%r",
            score, len(unsupported), question[:80],
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
