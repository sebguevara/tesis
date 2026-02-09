import uuid
import secrets
import logging
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from typing import Any
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.rag_service import RAGService
from app.core.session_memory import session_memory
from app.core.widget_auth import (
    WidgetAuthResult,
    verify_widget_api_key,
    verify_widget_api_key_for_source_id,
)
from app.core.widget_origin import (
    allowed_origins_for_domain,
    get_test_origin,
    is_origin_allowed_for_source,
)
from app.config import settings
from app.core.domain_utils import domain_variants, normalize_domain
from app.storage.db_client import async_session

router = APIRouter(tags=["Widget"])
rag = RAGService()
logger = logging.getLogger(__name__)


def _require_admin_token(x_admin_token: str | None) -> None:
    expected_admin_token = (settings.WIDGET_ADMIN_TOKEN or "").strip()
    if not expected_admin_token:
        raise HTTPException(status_code=503, detail="WIDGET_ADMIN_TOKEN no configurado")
    if (x_admin_token or "").strip() != expected_admin_token:
        raise HTTPException(status_code=401, detail="Admin token inválido")


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


async def _build_dev_auth_for_source(source_id: UUID) -> WidgetAuthResult | None:
    stmt = text(
        """
        SELECT s.source_id::text AS source_id, wc.source_public_id
        FROM sources s
        LEFT JOIN widget_credentials wc
          ON wc.source_id = s.source_id
         AND wc.is_active = true
        WHERE s.source_id = CAST(:source_id AS uuid)
        ORDER BY wc.created_at DESC NULLS LAST
        LIMIT 1
        """
    )
    async with async_session() as session:
        row = (await session.execute(stmt, {"source_id": str(source_id)})).mappings().first()
    if not row:
        return None
    return WidgetAuthResult(
        source_public_id=((row.get("source_public_id") or str(source_id)).strip().lower()),
        source_id=source_id,
    )


async def _resolve_source_public_id_from_input(source_input: str) -> str | None:
    source_raw = (source_input or "").strip()
    if not source_raw:
        return None

    if _looks_like_uuid(source_raw):
        stmt = text(
            """
            SELECT wc.source_public_id
            FROM widget_credentials wc
            WHERE wc.is_active = true
              AND wc.source_id = CAST(:source_id AS uuid)
            ORDER BY wc.created_at DESC
            LIMIT 1
            """
        )
        async with async_session() as session:
            row = (await session.execute(stmt, {"source_id": source_raw})).mappings().first()
        if not row:
            return None
        value = (row.get("source_public_id") or "").strip().lower()
        return value or None

    return source_raw.lower()


async def _resolve_source_public_id_by_domain(domain: str) -> str | None:
    host = normalize_domain(domain)
    variants = sorted(domain_variants(host))
    if not variants:
        return None

    stmt = text(
        """
        SELECT wc.source_public_id
        FROM sources s
        JOIN widget_credentials wc ON wc.source_id = s.source_id
        WHERE wc.is_active = true
          AND (lower(s.domain) = :domain1 OR lower(s.domain) = :domain2)
        ORDER BY wc.created_at DESC
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

    source_public_id = (row.get("source_public_id") or "").strip().lower()
    return source_public_id or None


class WidgetQueryRequest(BaseModel):
    question: str
    source_id: str | None = Field(default=None, description="Public source id, ej: medicina_unne_prod")
    session_id: str | None = Field(default=None)
    metadata: dict[str, Any] | None = Field(default=None, description="Metadata contextual del widget")


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

    source_input = (req.source_id or "").strip()
    source_public_id = ""
    source_uuid: UUID | None = None

    if source_input:
        if _looks_like_uuid(source_input):
            source_uuid = UUID(source_input)
        else:
            source_public_id = source_input.lower()
    else:
        domain = _request_domain(request)
        if not domain:
            raise HTTPException(
                status_code=422,
                detail="No se pudo resolver source_id: faltan source_id y Origin/Referer",
            )
        source_public_id = await _resolve_source_public_id_by_domain(domain) or ""
        if not source_public_id:
            raise HTTPException(
                status_code=404,
                detail="No se encontro source_id activo para el dominio de origen",
            )

    graph = rag.build_graph()
    logger.info(
        "widget_query start source_input=%s is_uuid=%s origin=%s api_prefix=%s",
        source_input,
        bool(source_uuid),
        (request.headers.get("origin") or "").strip(),
        "_".join(api_key.split("_")[:3]) if api_key else "",
    )

    try:
        async with async_session() as session:
            if source_uuid is not None:
                auth = await verify_widget_api_key_for_source_id(session, source_uuid, api_key)
            else:
                auth = await verify_widget_api_key(session, source_public_id, api_key)
        if (
            auth is None
            and source_uuid is not None
            and _is_localhost_request(request)
            and (settings.WIDGET_DEV_API_KEY or "").strip()
            and api_key == (settings.WIDGET_DEV_API_KEY or "").strip()
        ):
            logger.info("widget_query using dev api key fallback for source_id=%s", str(source_uuid))
            auth = await _build_dev_auth_for_source(source_uuid)
        if auth is None:
            logger.warning(
                "widget_query auth_failed source_input=%s source_uuid=%s origin_host=%s",
                source_input,
                str(source_uuid) if source_uuid else "",
                _request_origin_host(request),
            )
            raise HTTPException(status_code=401, detail="Credenciales inválidas: API key no coincide con source_id o no está activa")

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
        metadata = req.metadata or {}
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
            "metadata_received": bool(metadata),
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


class WidgetCredentialGenerateRequest(BaseModel):
    source_public_id: str
    source_id: UUID
    user_id: str
    is_dev: bool = False
    is_active: bool = True


class WidgetCredentialDevUpsertRequest(BaseModel):
    source_public_id: str
    source_id: UUID
    user_id: str
    api_key: str = "pfc_sk_local_demo_univ_2026_001"
    is_active: bool = True


class WidgetCredentialDebugCheckRequest(BaseModel):
    source_id: UUID
    api_key: str


def _generate_widget_api_key(is_dev: bool) -> str:
    flavor = "dev" if is_dev else "live"
    token = secrets.token_urlsafe(24).replace("-", "").replace("_", "")
    return f"pfc_sk_{flavor}_{token[:32]}"


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


@router.post("/widget/credentials/generate")
async def generate_widget_credential(
    req: WidgetCredentialGenerateRequest,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    from app.core.widget_auth import api_key_prefix, hash_api_key

    _require_admin_token(x_admin_token)
    source_public_id = (req.source_public_id or "").strip().lower()
    user_id = (req.user_id or "").strip()
    if not source_public_id:
        raise HTTPException(status_code=422, detail="source_public_id es obligatorio")
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id es obligatorio")

    raw_api_key = _generate_widget_api_key(is_dev=req.is_dev)
    credential_id = str(uuid.uuid4())
    now = "NOW()"
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
                f"""
                INSERT INTO widget_credentials
                (credential_id, source_public_id, source_id, api_key_hash, api_key_prefix, is_active, created_at)
                VALUES
                (CAST(:credential_id AS uuid), :source_public_id, CAST(:source_id AS uuid), :api_key_hash, :api_key_prefix, :is_active, {now})
                """
            ),
            {
                "credential_id": credential_id,
                "source_public_id": source_public_id,
                "source_id": str(req.source_id),
                "api_key_hash": hash_api_key(raw_api_key),
                "api_key_prefix": api_key_prefix(raw_api_key),
                "is_active": req.is_active,
            },
        )
        await session.execute(
            text(
                f"""
                INSERT INTO widget_credential_users
                (link_id, credential_id, user_id, created_at)
                VALUES
                (CAST(:link_id AS uuid), CAST(:credential_id AS uuid), :user_id, {now})
                """
            ),
            {
                "link_id": str(uuid.uuid4()),
                "credential_id": credential_id,
                "user_id": user_id,
            },
        )
        await session.commit()

    return {
        "status": "ok",
        "source_public_id": source_public_id,
        "source_id": str(req.source_id),
        "user_id": user_id,
        "credential_id": credential_id,
        "api_key": raw_api_key,
        "is_dev": bool(req.is_dev),
        "warning": "Guarda esta api_key ahora; luego no se puede recuperar en texto plano.",
    }


@router.post("/widget/credentials/dev/upsert")
async def upsert_dev_widget_credential(
    req: WidgetCredentialDevUpsertRequest,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    from app.core.widget_auth import api_key_prefix, hash_api_key

    _require_admin_token(x_admin_token)
    source_public_id = (req.source_public_id or "").strip().lower()
    user_id = (req.user_id or "").strip()
    raw_api_key = (req.api_key or "").strip()
    if not source_public_id:
        raise HTTPException(status_code=422, detail="source_public_id es obligatorio")
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id es obligatorio")
    if not raw_api_key:
        raise HTTPException(status_code=422, detail="api_key es obligatorio")

    credential_id = str(uuid.uuid4())
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
                "credential_id": credential_id,
                "source_public_id": source_public_id,
                "source_id": str(req.source_id),
                "api_key_hash": hash_api_key(raw_api_key),
                "api_key_prefix": api_key_prefix(raw_api_key),
                "is_active": req.is_active,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO widget_credential_users
                (link_id, credential_id, user_id, created_at)
                VALUES
                (CAST(:link_id AS uuid), CAST(:credential_id AS uuid), :user_id, NOW())
                """
            ),
            {
                "link_id": str(uuid.uuid4()),
                "credential_id": credential_id,
                "user_id": user_id,
            },
        )
        await session.commit()

    return {
        "status": "ok",
        "source_public_id": source_public_id,
        "source_id": str(req.source_id),
        "user_id": user_id,
        "credential_id": credential_id,
        "api_key": raw_api_key,
        "is_dev": True,
    }


@router.post("/widget/credentials/debug/check")
async def debug_check_widget_credential(
    req: WidgetCredentialDebugCheckRequest,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    from app.core.widget_auth import hash_api_key

    _require_admin_token(x_admin_token)
    raw_api_key = (req.api_key or "").strip()
    if not raw_api_key:
        raise HTTPException(status_code=422, detail="api_key es obligatorio")

    candidate_hash = hash_api_key(raw_api_key)
    async with async_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT credential_id::text AS credential_id,
                           source_public_id,
                           api_key_prefix,
                           is_active,
                           created_at,
                           last_used_at
                    FROM widget_credentials
                    WHERE source_id = CAST(:source_id AS uuid)
                    ORDER BY created_at DESC
                    """
                ),
                {"source_id": str(req.source_id)},
            )
        ).mappings().all()
        match = (
            await session.execute(
                text(
                    """
                    SELECT credential_id::text AS credential_id
                    FROM widget_credentials
                    WHERE source_id = CAST(:source_id AS uuid)
                      AND is_active = true
                      AND api_key_hash = :api_key_hash
                    LIMIT 1
                    """
                ),
                {"source_id": str(req.source_id), "api_key_hash": candidate_hash},
            )
        ).mappings().first()

    return {
        "source_id": str(req.source_id),
        "provided_prefix": raw_api_key.split("_")[0:3],
        "active_hash_match": bool(match),
        "matched_credential_id": (match or {}).get("credential_id"),
        "credentials_found": len(rows),
        "active_credentials": sum(1 for r in rows if bool(r.get("is_active"))),
        "credentials": [
            {
                "credential_id": r.get("credential_id"),
                "source_public_id": r.get("source_public_id"),
                "api_key_prefix": r.get("api_key_prefix"),
                "is_active": bool(r.get("is_active")),
                "created_at": str(r.get("created_at") or ""),
                "last_used_at": str(r.get("last_used_at") or ""),
            }
            for r in rows
        ],
    }


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
