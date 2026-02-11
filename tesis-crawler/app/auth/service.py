import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text

from app.storage.db_client import async_session


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def primary_email(data: dict[str, Any]) -> str | None:
    primary_id = (data.get("primary_email_address_id") or "").strip()
    for email_obj in data.get("email_addresses") or []:
        if (email_obj.get("id") or "").strip() == primary_id:
            value = (email_obj.get("email_address") or "").strip().lower()
            return value or None
    for email_obj in data.get("email_addresses") or []:
        value = (email_obj.get("email_address") or "").strip().lower()
        if value:
            return value
    return None


def from_ms_epoch(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


async def _generate_unique_api_key() -> str:
    for _ in range(8):
        token = secrets.token_urlsafe(24).replace("-", "").replace("_", "")
        candidate = f"pfc_sk_live_{token[:32]}"
        async with async_session() as session:
            exists = (
                await session.execute(
                    text("SELECT 1 FROM \"user\" WHERE api_key = :api_key LIMIT 1"),
                    {"api_key": candidate},
                )
            ).first()
        if not exists:
            return candidate
    raise HTTPException(status_code=500, detail="No se pudo generar api_key unica")


async def upsert_user_from_clerk_event(
    *,
    clerk_user_id: str,
    email: str | None,
    last_sign_in_at: datetime | None,
) -> dict[str, Any]:
    if not clerk_user_id:
        raise HTTPException(status_code=422, detail="clerk_user_id es obligatorio")

    async with async_session() as session:
        existing = (
            await session.execute(
                text(
                    """
                    SELECT user_id::text AS user_id, api_key
                    FROM "user"
                    WHERE clerk_user_id = :clerk_user_id
                    LIMIT 1
                    """
                ),
                {"clerk_user_id": clerk_user_id},
            )
        ).mappings().first()

        if existing:
            await session.execute(
                text(
                    """
                    UPDATE "user"
                    SET email = COALESCE(:email, email),
                        last_sign_in_at = COALESCE(:last_sign_in_at, last_sign_in_at),
                        updated_at = NOW()
                    WHERE clerk_user_id = :clerk_user_id
                    """
                ),
                {
                    "clerk_user_id": clerk_user_id,
                    "email": email,
                    "last_sign_in_at": last_sign_in_at,
                },
            )
            await session.commit()
            return {
                "user_id": existing.get("user_id"),
                "clerk_user_id": clerk_user_id,
                "api_key": existing.get("api_key"),
                "created_new_user": False,
                "rotated_api_key": False,
            }

        api_key = await _generate_unique_api_key()
        user_id = str(uuid.uuid4())
        await session.execute(
            text(
                """
                INSERT INTO "user"
                (user_id, clerk_user_id, email, api_key, created_at, updated_at, last_sign_in_at)
                VALUES
                (CAST(:user_id AS uuid), :clerk_user_id, :email, :api_key, NOW(), NOW(), :last_sign_in_at)
                """
            ),
            {
                "user_id": user_id,
                "clerk_user_id": clerk_user_id,
                "email": email,
                "api_key": api_key,
                "last_sign_in_at": last_sign_in_at,
            },
        )
        await session.commit()
        return {
            "user_id": user_id,
            "clerk_user_id": clerk_user_id,
            "api_key": api_key,
            "created_new_user": True,
            "rotated_api_key": False,
        }


async def rotate_user_api_key(clerk_user_id: str) -> dict[str, Any]:
    user_key = (clerk_user_id or "").strip()
    if not user_key:
        raise HTTPException(status_code=422, detail="clerk_user_id es obligatorio")

    new_key = await _generate_unique_api_key()
    async with async_session() as session:
        row = (
            await session.execute(
                text(
                    """
                    UPDATE "user"
                    SET api_key = :api_key, updated_at = NOW()
                    WHERE clerk_user_id = :clerk_user_id
                    RETURNING user_id::text AS user_id, email
                    """
                ),
                {"api_key": new_key, "clerk_user_id": user_key},
            )
        ).mappings().first()
        await session.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Usuario no existe")
    return {
        "user_id": row.get("user_id"),
        "clerk_user_id": user_key,
        "email": row.get("email"),
        "api_key": new_key,
        "rotated_api_key": True,
    }


async def get_user_by_clerk_id(clerk_user_id: str) -> dict[str, Any] | None:
    user_key = (clerk_user_id or "").strip()
    if not user_key:
        return None
    async with async_session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT user_id::text AS user_id, clerk_user_id, email, api_key, created_at, updated_at, last_sign_in_at
                    FROM "user"
                    WHERE clerk_user_id = :clerk_user_id
                    LIMIT 1
                    """
                ),
                {"clerk_user_id": user_key},
            )
        ).mappings().first()
    return dict(row) if row else None


async def link_user_to_source(*, clerk_user_id: str, source_id: str) -> dict[str, Any]:
    user_key = (clerk_user_id or "").strip()
    source_key = (source_id or "").strip()
    if not user_key:
        raise HTTPException(status_code=422, detail="clerk_user_id es obligatorio")
    if not source_key:
        raise HTTPException(status_code=422, detail="source_id es obligatorio")

    async with async_session() as session:
        user_row = (
            await session.execute(
                text("SELECT user_id::text AS user_id FROM \"user\" WHERE clerk_user_id = :clerk_user_id LIMIT 1"),
                {"clerk_user_id": user_key},
            )
        ).mappings().first()
        if not user_row:
            raise HTTPException(status_code=404, detail="Usuario no existe")

        source_exists = (
            await session.execute(
                text("SELECT 1 FROM sources WHERE source_id = CAST(:source_id AS uuid) LIMIT 1"),
                {"source_id": source_key},
            )
        ).first()
        if not source_exists:
            raise HTTPException(status_code=404, detail="source_id no existe")

        link = (
            await session.execute(
                text(
                    """
                    SELECT link_id::text AS link_id
                    FROM user_sources
                    WHERE user_id = CAST(:user_id AS uuid)
                      AND source_id = CAST(:source_id AS uuid)
                    LIMIT 1
                    """
                ),
                {"user_id": user_row.get("user_id"), "source_id": source_key},
            )
        ).mappings().first()
        if link:
            return {"created_new": False, "link_id": link.get("link_id")}

        link_id = str(uuid.uuid4())
        await session.execute(
            text(
                """
                INSERT INTO user_sources (link_id, user_id, source_id, created_at)
                VALUES (CAST(:link_id AS uuid), CAST(:user_id AS uuid), CAST(:source_id AS uuid), NOW())
                """
            ),
            {"link_id": link_id, "user_id": user_row.get("user_id"), "source_id": source_key},
        )
        await session.commit()
        return {"created_new": True, "link_id": link_id}


async def unlink_user_from_source(*, clerk_user_id: str, source_id: str) -> dict[str, Any]:
    user_key = (clerk_user_id or "").strip()
    source_key = (source_id or "").strip()
    if not user_key:
        raise HTTPException(status_code=422, detail="clerk_user_id es obligatorio")
    if not source_key:
        raise HTTPException(status_code=422, detail="source_id es obligatorio")

    async with async_session() as session:
        result = await session.execute(
            text(
                """
                DELETE FROM user_sources
                WHERE user_id = (SELECT user_id FROM "user" WHERE clerk_user_id = :clerk_user_id LIMIT 1)
                  AND source_id = CAST(:source_id AS uuid)
                RETURNING link_id::text AS link_id
                """
            ),
            {"clerk_user_id": user_key, "source_id": source_key},
        )
        row = result.mappings().first()
        await session.commit()
    return {"deleted": bool(row), "link_id": (row or {}).get("link_id")}


async def list_user_sources(clerk_user_id: str) -> list[dict[str, Any]]:
    user_key = (clerk_user_id or "").strip()
    if not user_key:
        return []
    async with async_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT us.link_id::text AS link_id,
                           us.source_id::text AS source_id,
                           s.domain,
                           us.created_at
                    FROM user_sources us
                    JOIN "user" u ON u.user_id = us.user_id
                    LEFT JOIN sources s ON s.source_id = us.source_id
                    WHERE u.clerk_user_id = :clerk_user_id
                    ORDER BY us.created_at DESC
                    """
                ),
                {"clerk_user_id": user_key},
            )
        ).mappings().all()
    return [
        {
            "link_id": row.get("link_id"),
            "source_id": row.get("source_id"),
            "domain": row.get("domain"),
            "linked_at": str(row.get("created_at") or ""),
        }
        for row in rows
    ]


@dataclass
class UserSourceAccess:
    user_id: UUID
    clerk_user_id: str
    source_id: UUID


async def verify_widget_access_for_source(raw_api_key: str, source_id: UUID) -> UserSourceAccess | None:
    if not raw_api_key or source_id is None:
        return None

    async with async_session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT u.user_id::text AS user_id, u.clerk_user_id
                    FROM "user" u
                    JOIN user_sources us ON us.user_id = u.user_id
                    WHERE u.api_key = :api_key
                      AND us.source_id = CAST(:source_id AS uuid)
                    LIMIT 1
                    """
                ),
                {"api_key": raw_api_key, "source_id": str(source_id)},
            )
        ).mappings().first()
    if not row:
        return None
    return UserSourceAccess(
        user_id=UUID(str(row.get("user_id"))),
        clerk_user_id=str(row.get("clerk_user_id")),
        source_id=source_id,
    )
