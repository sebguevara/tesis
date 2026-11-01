"""
Singleton wrapper around a cross-encoder for retrieval reranking.

Lazy-loaded the first time it's used (the model takes ~10-20s to load and
~120MB on disk). Predictions run in a thread to avoid blocking the asyncio
loop. Default is the multilingual mMARCO MiniLM: on the 3 UNNE eval sets it
beat the English ms-marco-MiniLM-L-12-v2 (pass_rate arq 70→80%, exa 82→85%,
med 80→80% with hallucination 7.9→4.8%). RERANKER_MODEL overrides it.
"""
from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("RERANKER_MODEL", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1")


@lru_cache(maxsize=1)
def _get_cross_encoder(model_name: str = DEFAULT_MODEL):
    # Imported inside the function so the heavy torch/transformers stack is
    # only loaded if the reranker is actually used.
    from sentence_transformers import CrossEncoder  # type: ignore

    logger.info("Loading cross-encoder %s (first call only)…", model_name)
    return CrossEncoder(model_name)


async def rerank(
    query: str,
    candidates: Sequence[str],
    *,
    top_k: int | None = None,
    model_name: str = DEFAULT_MODEL,
) -> list[tuple[int, float]]:
    """
    Score `candidates` against `query` with a cross-encoder.
    Returns a list of (original_index, score) sorted by score desc.
    Top_k truncates the returned list (None = all).
    """
    if not candidates:
        return []
    pairs: list[tuple[str, str]] = [(query or "", c or "") for c in candidates]

    def _predict() -> list[float]:
        ce = _get_cross_encoder(model_name)
        scores = ce.predict(pairs)
        return [float(s) for s in scores]

    scores = await asyncio.to_thread(_predict)
    ranked = sorted(enumerate(scores), key=lambda t: t[1], reverse=True)
    if top_k is not None:
        ranked = ranked[: max(0, int(top_k))]
    return ranked


def warmup(model_name: str = DEFAULT_MODEL) -> None:
    """Block-load the model so the first request doesn't pay the cold-start."""
    _get_cross_encoder(model_name)
