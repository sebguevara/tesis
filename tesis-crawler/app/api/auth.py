from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from svix.webhooks import Webhook, WebhookVerificationError

from app.auth.service import (
    from_ms_epoch,
    get_user_by_clerk_id,
    link_user_to_source,
    list_user_sources,
    primary_email,
    rotate_user_api_key,
    unlink_user_from_source,
    upsert_user_from_clerk_event,
)
from app.config import settings

router = APIRouter(tags=["Auth"])


class SyncUserRequest(BaseModel):
    clerk_user_id: str
    email: str | None = None
    last_sign_in_at: datetime | None = None


class RotateApiKeyRequest(BaseModel):
    clerk_user_id: str


class LinkSourceRequest(BaseModel):
    clerk_user_id: str
    source_id: str


@router.post("/auth/clerk/webhook")
@router.post("/clerk/webhook")
async def clerk_webhook(request: Request):
    webhook_secret = (settings.CLERK_WEBHOOK_SECRET or "").strip()
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="CLERK_WEBHOOK_SECRET no configurado")

    svix_id = request.headers.get("svix-id")
    svix_timestamp = request.headers.get("svix-timestamp")
    svix_signature = request.headers.get("svix-signature")
    if not svix_id or not svix_timestamp or not svix_signature:
        raise HTTPException(status_code=400, detail="Headers Svix incompletos")

    payload_bytes = await request.body()
    try:
        event = Webhook(webhook_secret).verify(
            payload_bytes,
            {
                "svix-id": svix_id,
                "svix-timestamp": svix_timestamp,
                "svix-signature": svix_signature,
            },
        )
    except WebhookVerificationError:
        raise HTTPException(status_code=401, detail="Firma de webhook invalida")

    event_type = (event.get("type") or "").strip()
    data = event.get("data") or {}

    if event_type in {"user.created", "user.updated"}:
        clerk_user_id = (data.get("id") or "").strip()
        if not clerk_user_id:
            raise HTTPException(status_code=422, detail="Evento user.* sin id")
        user = await upsert_user_from_clerk_event(
            clerk_user_id=clerk_user_id,
            email=primary_email(data),
            last_sign_in_at=from_ms_epoch(data.get("last_sign_in_at")),
        )
        return {"ok": True, "event_type": event_type, "user": user}

    if event_type == "session.created":
        clerk_user_id = (data.get("user_id") or "").strip()
        if not clerk_user_id:
            raise HTTPException(status_code=422, detail="Evento session.created sin user_id")
        user = await upsert_user_from_clerk_event(
            clerk_user_id=clerk_user_id,
            email=None,
            last_sign_in_at=from_ms_epoch(data.get("created_at")),
        )
        return {"ok": True, "event_type": event_type, "user": user}

    return {"ok": True, "event_type": event_type, "ignored": True}


@router.post("/auth/users/sync")
async def sync_user(req: SyncUserRequest):
    user = await upsert_user_from_clerk_event(
        clerk_user_id=(req.clerk_user_id or "").strip(),
        email=(req.email or None),
        last_sign_in_at=req.last_sign_in_at,
    )
    return {"ok": True, "user": user}


@router.post("/auth/users/api-key/rotate")
async def rotate_api_key(req: RotateApiKeyRequest):
    user = await rotate_user_api_key((req.clerk_user_id or "").strip())
    return {"ok": True, "user": user}


@router.post("/auth/users/sources/link")
async def link_source(req: LinkSourceRequest):
    result = await link_user_to_source(
        clerk_user_id=(req.clerk_user_id or "").strip(),
        source_id=(req.source_id or "").strip(),
    )
    return {"ok": True, "result": result}


@router.post("/auth/users/sources/unlink")
async def unlink_source(req: LinkSourceRequest):
    result = await unlink_user_from_source(
        clerk_user_id=(req.clerk_user_id or "").strip(),
        source_id=(req.source_id or "").strip(),
    )
    return {"ok": True, "result": result}


@router.get("/auth/users/{clerk_user_id}")
async def get_user(clerk_user_id: str):
    user = await get_user_by_clerk_id((clerk_user_id or "").strip())
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no existe")
    return {"ok": True, "user": user}


@router.get("/auth/users/{clerk_user_id}/sources")
async def get_user_sources(clerk_user_id: str):
    user_key = (clerk_user_id or "").strip()
    user = await get_user_by_clerk_id(user_key)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no existe")
    sources = await list_user_sources(user_key)
    return {"ok": True, "clerk_user_id": user_key, "sources": sources, "count": len(sources)}
