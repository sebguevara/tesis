from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from app.core.domain_utils import domain_variants, normalize_domain
from app.storage.db_client import async_session

router = APIRouter(tags=["Sources"])


@router.get("/sources")
async def list_sources(limit: int = Query(default=100, ge=1, le=1000)):
    stmt = text(
        """
        SELECT source_id::text AS source_id, domain, created_at
        FROM sources
        ORDER BY created_at DESC
        LIMIT :limit
        """
    )
    async with async_session() as session:
        rows = (await session.execute(stmt, {"limit": limit})).mappings().all()
    return {"items": [dict(row) for row in rows]}


@router.get("/sources/lookup")
async def lookup_source(domain: str = Query(..., min_length=3)):
    norm_domain = normalize_domain(domain)
    variants = sorted(domain_variants(norm_domain))
    if not norm_domain or not variants:
        raise HTTPException(status_code=422, detail="Dominio inválido")
    stmt = text(
        """
        SELECT source_id::text AS source_id, domain, created_at
        FROM sources
        WHERE lower(domain) = :domain1 OR lower(domain) = :domain2
        ORDER BY created_at ASC
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
        raise HTTPException(status_code=404, detail="Source no encontrada para ese dominio")
    return dict(row)
