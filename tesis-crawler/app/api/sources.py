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


@router.get("/sources/overview")
async def list_sources_overview(limit: int = Query(default=100, ge=1, le=500)):
    async with async_session() as session:
        tables_row = (
            await session.execute(
                text(
                    """
                    SELECT
                      to_regclass('public.chunks')::text AS chunks_table,
                      to_regclass('public.documents_embeddings')::text AS embeddings_table,
                      to_regclass('public.documents_embedding')::text AS embedding_view_table,
                      to_regclass('public.documents_embedding_store')::text AS embedding_store_table,
                      COALESCE(
                        (SELECT to_regclass(target_table)::text FROM ai.vectorizer_status ORDER BY id DESC LIMIT 1),
                        ''
                      ) AS vectorizer_target_table,
                      COALESCE(
                        (SELECT to_regclass(view)::text FROM ai.vectorizer_status ORDER BY id DESC LIMIT 1),
                        ''
                      ) AS vectorizer_view_table
                    """
                )
            )
        ).mappings().first()

        chunks_table = tables_row.get("chunks_table") if tables_row else None
        embeddings_table = tables_row.get("embeddings_table") if tables_row else None
        embedding_view_table = tables_row.get("embedding_view_table") if tables_row else None
        embedding_store_table = tables_row.get("embedding_store_table") if tables_row else None
        vectorizer_target_table = tables_row.get("vectorizer_target_table") if tables_row else None
        vectorizer_view_table = tables_row.get("vectorizer_view_table") if tables_row else None

        if chunks_table:
            chunks_count_select = "COALESCE(cs.chunks_count, 0) AS chunks_count"
            chunks_subquery = """
            LEFT JOIN (
              SELECT
                d.source_id,
                COUNT(c.chunk_id) AS chunks_count
              FROM documents d
              LEFT JOIN chunks c ON c.doc_id = d.doc_id
              GROUP BY d.source_id
            ) cs ON cs.source_id = s.source_id
            """
        elif embeddings_table:
            chunks_count_select = "COALESCE(cs.chunks_count, 0) AS chunks_count"
            chunks_subquery = """
            LEFT JOIN (
              SELECT
                d.source_id,
                COUNT(de.doc_id) AS chunks_count
              FROM documents d
              LEFT JOIN documents_embeddings de ON de.doc_id = d.doc_id
              GROUP BY d.source_id
            ) cs ON cs.source_id = s.source_id
            """
        elif embedding_view_table:
            chunks_count_select = "COALESCE(cs.chunks_count, 0) AS chunks_count"
            chunks_subquery = """
            LEFT JOIN (
              SELECT
                d.source_id,
                COUNT(de.doc_id) AS chunks_count
              FROM documents d
              LEFT JOIN documents_embedding de ON de.doc_id = d.doc_id
              GROUP BY d.source_id
            ) cs ON cs.source_id = s.source_id
            """
        elif embedding_store_table:
            chunks_count_select = "COALESCE(cs.chunks_count, 0) AS chunks_count"
            chunks_subquery = """
            LEFT JOIN (
              SELECT
                d.source_id,
                COUNT(de.doc_id) AS chunks_count
              FROM documents d
              LEFT JOIN documents_embedding_store de ON de.doc_id = d.doc_id
              GROUP BY d.source_id
            ) cs ON cs.source_id = s.source_id
            """
        elif vectorizer_view_table:
            relation = str(vectorizer_view_table).split(".", 1)[-1]
            chunks_count_select = "COALESCE(cs.chunks_count, 0) AS chunks_count"
            chunks_subquery = f"""
            LEFT JOIN (
              SELECT
                d.source_id,
                COUNT(de.doc_id) AS chunks_count
              FROM documents d
              LEFT JOIN {relation} de ON de.doc_id = d.doc_id
              GROUP BY d.source_id
            ) cs ON cs.source_id = s.source_id
            """
        elif vectorizer_target_table:
            relation = str(vectorizer_target_table).split(".", 1)[-1]
            chunks_count_select = "COALESCE(cs.chunks_count, 0) AS chunks_count"
            chunks_subquery = f"""
            LEFT JOIN (
              SELECT
                d.source_id,
                COUNT(de.doc_id) AS chunks_count
              FROM documents d
              LEFT JOIN {relation} de ON de.doc_id = d.doc_id
              GROUP BY d.source_id
            ) cs ON cs.source_id = s.source_id
            """
        else:
            chunks_count_select = "0 AS chunks_count"
            chunks_subquery = ""

        stmt = text(
            f"""
            SELECT
              s.source_id::text AS source_id,
              s.domain,
              s.created_at,
              COALESCE(ds.documents_count, 0) AS documents_count,
              COALESCE(ds.first_fetched_at, NULL) AS first_fetched_at,
              COALESCE(ds.last_fetched_at, NULL) AS last_fetched_at,
              {chunks_count_select},
              COALESCE(ss.sessions_count, 0) AS sessions_count
            FROM sources s
            LEFT JOIN (
              SELECT
                d.source_id,
                COUNT(*) AS documents_count,
                MIN(d.fetched_at) AS first_fetched_at,
                MAX(d.fetched_at) AS last_fetched_at
              FROM documents d
              GROUP BY d.source_id
            ) ds ON ds.source_id = s.source_id
            {chunks_subquery}
            LEFT JOIN (
              SELECT
                cm.source_id,
                COUNT(DISTINCT cm.session_id) AS sessions_count
              FROM conversation_messages cm
              WHERE cm.source_id IS NOT NULL AND cm.role = 'user'
              GROUP BY cm.source_id
            ) ss ON ss.source_id = s.source_id
            ORDER BY COALESCE(ds.last_fetched_at, s.created_at) DESC
            LIMIT :limit
            """
        )
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
