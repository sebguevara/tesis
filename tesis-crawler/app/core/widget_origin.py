from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import text

from app.config import settings
from app.core.domain_utils import domain_variants
from app.storage.db_client import async_session


def _normalize_origin(origin: str) -> str:
    value = (origin or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _origin_host(origin: str) -> str:
    parsed = urlparse(origin)
    return (parsed.hostname or "").lower()


def _is_local_dev_origin(origin: str) -> bool:
    host = _origin_host(_normalize_origin(origin))
    return host in {"localhost", "127.0.0.1", "::1"}


def _is_test_origin_allowed(origin: str) -> bool:
    test_origin = _normalize_origin(settings.WIDGET_TEST_ORIGIN)
    if not test_origin:
        return False
    return _normalize_origin(origin) == test_origin


def get_test_origin() -> str:
    return _normalize_origin(settings.WIDGET_TEST_ORIGIN)


async def is_origin_allowed_globally(origin: str) -> bool:
    norm_origin = _normalize_origin(origin)
    if not norm_origin:
        return False
    if _is_local_dev_origin(norm_origin):
        return True
    if _is_test_origin_allowed(norm_origin):
        return True

    host = _origin_host(norm_origin)
    if not host:
        return False

    stmt = text("SELECT domain FROM sources")
    async with async_session() as session:
        rows = (await session.execute(stmt)).mappings().all()
    for row in rows:
        domain = (row.get("domain") or "").strip().lower()
        if host in domain_variants(domain):
            return True
    return False


async def is_origin_allowed_for_source(origin: str, source_id: UUID) -> bool:
    norm_origin = _normalize_origin(origin)
    if not norm_origin:
        return False
    if _is_local_dev_origin(norm_origin):
        return True
    if _is_test_origin_allowed(norm_origin):
        return True

    host = _origin_host(norm_origin)
    if not host:
        return False

    stmt = text(
        """
        SELECT domain
        FROM sources
        WHERE source_id = CAST(:source_id AS uuid)
        LIMIT 1
        """
    )
    async with async_session() as session:
        row = (
            await session.execute(stmt, {"source_id": str(source_id)})
        ).mappings().first()
    if not row:
        return False

    domain = (row.get("domain") or "").strip().lower()
    return host in domain_variants(domain)


def allowed_origins_for_domain(domain: str) -> list[str]:
    normalized_domain = (domain or "").strip().lower()
    if not normalized_domain:
        return []
    origins = [f"https://{host}" for host in sorted(domain_variants(normalized_domain))]
    test_origin = get_test_origin()
    if test_origin:
        origins.append(test_origin)
    # Dedup preserving order
    seen: set[str] = set()
    out: list[str] = []
    for item in origins:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
