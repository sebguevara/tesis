import hashlib
import hmac
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from app.embedding.models import WidgetCredential, utc_now_naive


@dataclass
class WidgetAuthResult:
    source_public_id: str
    source_id: UUID


def hash_api_key(raw_api_key: str) -> str:
    return hashlib.sha256((raw_api_key or "").encode("utf-8")).hexdigest()


def api_key_prefix(raw_api_key: str) -> str:
    key = (raw_api_key or "").strip()
    if not key:
        return "pfc_sk"
    parts = key.split("_")
    if len(parts) >= 3:
        return "_".join(parts[:3])
    return parts[0]


async def verify_widget_api_key(
    db_session,
    source_public_id: str,
    raw_api_key: str,
) -> WidgetAuthResult | None:
    source_key = (source_public_id or "").strip().lower()
    if not source_key or not raw_api_key:
        return None

    stmt = (
        select(WidgetCredential)
        .where(WidgetCredential.source_public_id == source_key)
        .where(WidgetCredential.is_active.is_(True))
        .order_by(WidgetCredential.created_at.desc())
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    if not rows:
        return None

    candidate_hash = hash_api_key(raw_api_key)
    matched: WidgetCredential | None = None
    for cred in rows:
        if hmac.compare_digest(cred.api_key_hash, candidate_hash):
            matched = cred
            break

    if matched is None:
        return None

    matched.last_used_at = utc_now_naive()
    await db_session.commit()
    return WidgetAuthResult(
        source_public_id=matched.source_public_id,
        source_id=matched.source_id,
    )

