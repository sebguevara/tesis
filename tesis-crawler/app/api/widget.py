import logging
import asyncio
import uuid
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth.service import verify_widget_access_for_source
from app.config import settings
from app.core.domain_utils import domain_variants, normalize_domain
from app.core.chat_format import add_conversational_lead, apply_source_visibility
from app.core.rag_service import RAGService
from app.core.session_memory import session_memory
from app.core.widget_origin import allowed_origins_for_domain, get_test_origin, is_origin_allowed_for_source
from app.storage.db_client import async_session

router = APIRouter(tags=["Widget"])
rag = RAGService()
logger = logging.getLogger(__name__)


def _is_retryable_llm_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    raw = str(exc).lower()
    return any(
        token in raw
        for token in (
            "context_length_exceeded",
            "maximum context length",
            "messages resulted in",
            "too many tokens",
            "rate limit",
            "timeout",
            "server_error",
        )
    )


async def _invoke_with_compact_retry(
    *,
    question: str,
    source_id: UUID,
    session_state: dict,
    history: list[str],
) -> dict:
    graph = rag.build_graph()
    payload: dict = {
        "query": question,
        "context": [],
        "response": "",
        "history": history,
        "source_id": str(source_id),
        "session_state": session_state,
    }
    try:
        return await asyncio.wait_for(
            graph.ainvoke(payload),
            timeout=float(getattr(settings, "RAG_GRAPH_TIMEOUT_SECONDS", 25)),
        )
    except Exception as exc:
        if not _is_retryable_llm_error(exc):
            raise
        compact_payload = dict(payload)
        compact_payload["history"] = (history or [])[-6:]
        compact_payload["session_state"] = dict(session_state or {}) | {"retry_mode": "compact"}
        logger.warning("Widget RAG primary invoke failed, retrying in compact mode: %s", exc)
        return await asyncio.wait_for(
            graph.ainvoke(compact_payload),
            timeout=float(getattr(settings, "RAG_GRAPH_COMPACT_TIMEOUT_SECONDS", 14)),
        )


def _request_domain(request: Request) -> str:
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        return normalize_domain(origin)

    referer = (request.headers.get("referer") or "").strip()
    if referer:
        return normalize_domain(referer)

    return ""


def _looks_like_uuid(value: str) -> bool:
    try:
        UUID((value or "").strip())
        return True
    except Exception:
        return False


def _request_origin_host(request: Request) -> str:
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return ""
    try:
        return (urlparse(origin).hostname or "").strip().lower()
    except Exception:
        return ""


def _is_localhost_request(request: Request) -> bool:
    return _request_origin_host(request) in {"localhost", "127.0.0.1", "::1"}


async def _resolve_source_id_from_domain(domain: str) -> UUID | None:
    host = normalize_domain(domain)
    variants = sorted(domain_variants(host))
    if not variants:
        return None

    stmt = text(
        """
        SELECT s.source_id::text AS source_id
        FROM sources s
        WHERE lower(s.domain) = :domain1 OR lower(s.domain) = :domain2
        LIMIT 1
        """
    )
    async with async_session() as session:
        row = (
            await session.execute(
                stmt,
                {"domain1": variants[0], "domain2": variants[1]},
            )
        ).mappings().first()
    if not row:
        return None
    return UUID(str(row.get("source_id")))


async def _resolve_source_id_from_input(source_input: str) -> UUID | None:
    source_raw = (source_input or "").strip()
    if not source_raw:
        return None

    if _looks_like_uuid(source_raw):
        return UUID(source_raw)
    return await _resolve_source_id_from_domain(source_raw)


class WidgetQueryRequest(BaseModel):
    question: str
    source_id: str | None = Field(default=None, description="UUID o dominio de la fuente scrapeada")
    session_id: str | None = Field(default=None)
    metadata: dict | None = Field(default=None, description="Metadata contextual del widget")
    debug: bool = Field(default=False, description="Si true, devuelve los chunks de contexto usados (para evaluación)")


@router.post("/widget/query")
async def widget_query(
    req: WidgetQueryRequest,
    request: Request,
    response: Response,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    auth = None
    source_uuid = None
    session_id = (req.session_id or "").strip() or str(uuid.uuid4())
    question = (req.question or "").strip()
    metadata = req.metadata or {}
    try:
        api_key = (x_api_key or "").strip()
        if not api_key:
            raise HTTPException(status_code=401, detail="Falta X-API-Key")
        if not api_key.startswith("pfc_sk_"):
            raise HTTPException(status_code=401, detail="API key invalida")

        source_input = (req.source_id or "").strip()
        if source_input:
            source_uuid = await _resolve_source_id_from_input(source_input)
            if source_uuid is None:
                raise HTTPException(status_code=404, detail="source_id no existe")
        else:
            domain = _request_domain(request)
            if not domain:
                raise HTTPException(
                    status_code=422,
                    detail="No se pudo resolver source_id: faltan source_id y Origin/Referer",
                )
            source_uuid = await _resolve_source_id_from_domain(domain)
            if source_uuid is None:
                raise HTTPException(status_code=404, detail="No se encontro source_id para el dominio de origen")

        logger.info(
            "widget_query source_input=%s source_uuid=%s origin=%s api_prefix=%s",
            source_input,
            str(source_uuid),
            (request.headers.get("origin") or "").strip(),
            "_".join(api_key.split("_")[:3]) if api_key else "",
        )

        auth = await verify_widget_access_for_source(api_key, source_uuid)
        if (
            auth is None
            and _is_localhost_request(request)
            and (settings.WIDGET_DEV_API_KEY or "").strip()
            and api_key == (settings.WIDGET_DEV_API_KEY or "").strip()
        ):
            class _DevAuth:
                clerk_user_id = "dev"
                source_id = source_uuid

            auth = _DevAuth()

        if auth is None:
            raise HTTPException(status_code=401, detail="API key no autorizada para este source_id")

        origin = (request.headers.get("origin") or "").strip()
        if origin and not await is_origin_allowed_for_source(origin, auth.source_id):
            raise HTTPException(status_code=403, detail="Origin no permitido para esta fuente")
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Credentials"] = "true"

        session_id = await session_memory.ensure_session(session_id)
        session_state = await session_memory.get_state(session_id)

        prior_history = await session_memory.recent_history(
            session_id=session_id, source_id=auth.source_id, max_items=1
        )
        is_first_turn = len(prior_history) == 0
        await session_memory.append_user(session_id, question, source_id=auth.source_id)
        history = await session_memory.recent_history(session_id=session_id, source_id=auth.source_id, max_items=12)
        session_state = rag.derive_session_state(
            current_state=session_state,
            query=question,
            history=history,
        )
        await session_memory.update_state(session_id, session_state)

        result = await _invoke_with_compact_retry(
            question=question,
            source_id=auth.source_id,
            session_state=session_state,
            history=history,
        )
        answer = add_conversational_lead(
            (result.get("response") or "").strip(),
            question,
            is_first_turn=is_first_turn,
        )
        answer = apply_source_visibility(answer)
        await session_memory.append_assistant(session_id, answer, source_id=auth.source_id)
        body: dict = {
            "session_id": session_id,
            "source_id": str(auth.source_id),
            "user_id": auth.clerk_user_id,
            "answer": answer,
            "metadata_received": bool(metadata),
        }
        if req.debug:
            body["context_chunks"] = result.get("context") or []
        return body
    except HTTPException:
        raise
    except Exception:
        logger.exception("Widget query failed")
        fallback_answer = (
            "No llegué a resolverlo bien en este intento. "
            "Si querés, lo intento de nuevo con la carrera y el trámite exacto "
            "(por ejemplo: 'Inscripción a Licenciatura en Enfermería 2026')."
        )
        try:
            safe_session = await session_memory.ensure_session(session_id)
            if source_uuid is not None:
                await session_memory.append_assistant(
                    safe_session,
                    fallback_answer,
                    source_id=source_uuid,
                )
            return {
                "session_id": safe_session,
                "source_id": str(source_uuid) if source_uuid is not None else "",
                "user_id": getattr(auth, "clerk_user_id", ""),
                "answer": fallback_answer,
                "metadata_received": bool(metadata),
            }
        except Exception:
            raise HTTPException(status_code=500, detail="No se pudo procesar la consulta del widget")


@router.get("/widget/origins/allowed")
async def widget_allowed_origins(source_id: str):
    source_key = (source_id or "").strip()
    if not source_key:
        raise HTTPException(status_code=422, detail="source_id es obligatorio")

    stmt = text(
        """
        SELECT s.source_id::text AS source_id, s.domain
        FROM sources s
        WHERE s.source_id = CAST(:source_id AS uuid)
        LIMIT 1
        """
    )
    async with async_session() as session:
        row = (await session.execute(stmt, {"source_id": source_key})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="source_id no existe")

    domain = (row.get("domain") or "").strip().lower()
    return {
        "source_id": row.get("source_id"),
        "domain": domain,
        "allowed_origins": allowed_origins_for_domain(domain),
        "test_origin": get_test_origin() or None,
    }


class WidgetOriginValidateRequest(BaseModel):
    source_id: str
    origin: str


@router.post("/widget/origins/validate")
async def widget_validate_origin(req: WidgetOriginValidateRequest):
    source_id = (req.source_id or "").strip()
    origin = (req.origin or "").strip()
    if not source_id:
        raise HTTPException(status_code=422, detail="source_id es obligatorio")
    if not origin:
        raise HTTPException(status_code=422, detail="origin es obligatorio")

    allowed = await is_origin_allowed_for_source(origin, UUID(source_id))
    return {
        "source_id": source_id,
        "origin": origin,
        "allowed": bool(allowed),
    }
