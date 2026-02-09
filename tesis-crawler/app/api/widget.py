import uuid
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.rag_service import RAGService
from app.core.session_memory import session_memory
from app.core.widget_auth import verify_widget_api_key
from app.core.widget_origin import (
    allowed_origins_for_domain,
    get_test_origin,
    is_origin_allowed_for_source,
)
from app.config import settings
from app.storage.db_client import async_session

router = APIRouter(tags=["Widget"])
rag = RAGService()


def _require_admin_token(x_admin_token: str | None) -> None:
    expected_admin_token = (settings.WIDGET_ADMIN_TOKEN or "").strip()
    if not expected_admin_token:
        raise HTTPException(status_code=503, detail="WIDGET_ADMIN_TOKEN no configurado")
    if (x_admin_token or "").strip() != expected_admin_token:
        raise HTTPException(status_code=401, detail="Admin token inválido")


class WidgetQueryRequest(BaseModel):
    question: str
    source_id: str = Field(description="Public source id, ej: medicina_unne_prod")
    session_id: str | None = Field(default=None)


@router.post("/widget/query")
async def widget_query(
    req: WidgetQueryRequest,
    request: Request,
    response: Response,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    api_key = (x_api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="Falta X-API-Key")

    if not api_key.startswith("pfc_sk_"):
        raise HTTPException(status_code=401, detail="API key inválida")

    source_public_id = (req.source_id or "").strip().lower()
    if not source_public_id:
        raise HTTPException(status_code=422, detail="source_id es obligatorio")

    graph = rag.build_graph()

    try:
        async with async_session() as session:
            auth = await verify_widget_api_key(session, source_public_id, api_key)
        if auth is None:
            raise HTTPException(status_code=401, detail="Credenciales inválidas")

        origin = (request.headers.get("origin") or "").strip()
        if origin and not await is_origin_allowed_for_source(origin, auth.source_id):
            raise HTTPException(status_code=403, detail="Origin no permitido para esta fuente")
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Credentials"] = "true"

        session_id = (req.session_id or "").strip() or str(uuid.uuid4())
        session_id = await session_memory.ensure_session(session_id)

        question = (req.question or "").strip()
        await session_memory.append_user(session_id, question, source_id=auth.source_id)
        history = await session_memory.recent_history(
            session_id=session_id, source_id=auth.source_id, max_items=10
        )

        result = await graph.ainvoke(
            {
                "query": question,
                "context": [],
                "response": "",
                "history": history,
                "source_id": str(auth.source_id),
            }
        )
        answer = (result.get("response") or "").strip()
        await session_memory.append_assistant(session_id, answer, source_id=auth.source_id)

        return {
            "session_id": session_id,
            "source_id": auth.source_public_id,
            "internal_source_id": str(auth.source_id),
            "answer": answer,
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


class WidgetCredentialUpsertRequest(BaseModel):
    source_public_id: str
    source_id: UUID
    api_key: str
    is_active: bool = True


@router.post("/widget/credentials/upsert")
async def upsert_widget_credential(
    req: WidgetCredentialUpsertRequest,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    from app.core.widget_auth import api_key_prefix, hash_api_key

    source_public_id = (req.source_public_id or "").strip().lower()
    raw_api_key = (req.api_key or "").strip()
    _require_admin_token(x_admin_token)
    if not source_public_id:
        raise HTTPException(status_code=422, detail="source_public_id es obligatorio")
    if not raw_api_key:
        raise HTTPException(status_code=422, detail="api_key es obligatorio")

    async with async_session() as session:
        source_exists = (
            await session.execute(
                text("SELECT 1 FROM sources WHERE source_id = CAST(:source_id AS uuid) LIMIT 1"),
                {"source_id": str(req.source_id)},
            )
        ).first()
        if not source_exists:
            raise HTTPException(status_code=404, detail="source_id no existe")

        await session.execute(
            text(
                """
                UPDATE widget_credentials
                SET is_active = false
                WHERE source_public_id = :source_public_id
                """
            ),
            {"source_public_id": source_public_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO widget_credentials
                (credential_id, source_public_id, source_id, api_key_hash, api_key_prefix, is_active, created_at)
                VALUES
                (CAST(:credential_id AS uuid), :source_public_id, CAST(:source_id AS uuid), :api_key_hash, :api_key_prefix, :is_active, NOW())
                """
            ),
            {
                "credential_id": str(uuid.uuid4()),
                "source_public_id": source_public_id,
                "source_id": str(req.source_id),
                "api_key_hash": hash_api_key(raw_api_key),
                "api_key_prefix": api_key_prefix(raw_api_key),
                "is_active": req.is_active,
            },
        )
        await session.commit()

    return {"source_public_id": source_public_id, "source_id": str(req.source_id), "status": "ok"}


@router.get("/widget/origins/allowed")
async def widget_allowed_origins(
    source_public_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin_token(x_admin_token)
    source_key = (source_public_id or "").strip().lower()
    if not source_key:
        raise HTTPException(status_code=422, detail="source_public_id es obligatorio")

    stmt = text(
        """
        SELECT wc.source_public_id, wc.source_id::text AS source_id, s.domain
        FROM widget_credentials wc
        JOIN sources s ON s.source_id = wc.source_id
        WHERE wc.source_public_id = :source_public_id
          AND wc.is_active = true
        ORDER BY wc.created_at DESC
        LIMIT 1
        """
    )
    async with async_session() as session:
        row = (await session.execute(stmt, {"source_public_id": source_key})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="No hay credencial activa para source_public_id")

    domain = (row.get("domain") or "").strip().lower()
    return {
        "source_public_id": row.get("source_public_id"),
        "source_id": row.get("source_id"),
        "domain": domain,
        "allowed_origins": allowed_origins_for_domain(domain),
        "test_origin": get_test_origin() or None,
    }


class WidgetOriginValidateRequest(BaseModel):
    source_public_id: str
    origin: str


@router.post("/widget/origins/validate")
async def widget_validate_origin(
    req: WidgetOriginValidateRequest,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin_token(x_admin_token)
    source_key = (req.source_public_id or "").strip().lower()
    origin = (req.origin or "").strip()
    if not source_key:
        raise HTTPException(status_code=422, detail="source_public_id es obligatorio")
    if not origin:
        raise HTTPException(status_code=422, detail="origin es obligatorio")

    stmt = text(
        """
        SELECT wc.source_id::text AS source_id
        FROM widget_credentials wc
        WHERE wc.source_public_id = :source_public_id
          AND wc.is_active = true
        ORDER BY wc.created_at DESC
        LIMIT 1
        """
    )
    async with async_session() as session:
        row = (await session.execute(stmt, {"source_public_id": source_key})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="No hay credencial activa para source_public_id")

    source_id = UUID(str(row.get("source_id")))
    allowed = await is_origin_allowed_for_source(origin, source_id)
    return {
        "source_public_id": source_key,
        "source_id": str(source_id),
        "origin": origin,
        "allowed": bool(allowed),
    }
