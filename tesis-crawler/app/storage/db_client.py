import asyncio
import logging
import pgai

import psycopg
from psycopg import sql
from psycopg.errors import DuplicateDatabase
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from app.config import settings


logger = logging.getLogger(__name__)


def _build_async_engine_url() -> str:
    """Use asyncpg for SQLAlchemy async engine on Windows-compatible event loops."""
    url = make_url(settings.DATABASE_URL)
    if url.drivername == "postgresql+psycopg":
        url = url.set(drivername="postgresql+asyncpg")
    elif "+" not in url.drivername and url.drivername.startswith("postgresql"):
        url = url.set(drivername="postgresql+asyncpg")
    return url.render_as_string(hide_password=False)


engine = create_async_engine(_build_async_engine_url(), echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _build_psycopg_dsn(database: str | None = None) -> str:
    url = make_url(settings.DATABASE_URL)
    if database is not None:
        url = url.set(database=database)
    # psycopg.connect espera driver "postgresql", no "postgresql+psycopg"
    url = url.set(drivername=url.drivername.split("+", 1)[0])
    return url.render_as_string(hide_password=False)


def _ensure_database_exists_sync() -> None:
    target_url = make_url(settings.DATABASE_URL)
    target_db = target_url.database
    if not target_db:
        return

    admin_dsn = _build_psycopg_dsn(database="postgres")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
            if cur.fetchone() is None:
                try:
                    cur.execute(
                        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db))
                    )
                except DuplicateDatabase:
                    pass


async def _setup_vectorizer(conn) -> None:
    """Create a pgai vectorizer on the documents table if not already present."""
    vectorizer = None
    needs_recreate = False
    try:
        vectorizer = (
            await conn.execute(
                text(
                    """
                    SELECT id, name, trigger_name
                    FROM ai.vectorizer
                    WHERE source_schema = 'public' AND source_table = 'documents'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                )
            )
        ).mappings().first()
    except Exception:
        # ai.vectorizer may not exist yet if extension just enabled
        vectorizer = None

    if vectorizer:
        try:
            health = (
                await conn.execute(
                    text(
                        """
                        SELECT
                          EXISTS (
                            SELECT 1
                            FROM pg_trigger t
                            JOIN pg_class c ON c.oid = t.tgrelid
                            JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE n.nspname = 'public'
                              AND c.relname = 'documents'
                              AND t.tgname = :trigger_name
                              AND NOT t.tgisinternal
                          ) AS trigger_exists,
                          COALESCE(
                            (SELECT to_regclass(target_table)::text FROM ai.vectorizer_status WHERE id = :id),
                            ''
                          ) AS target_table,
                          COALESCE(
                            (SELECT to_regclass(view)::text FROM ai.vectorizer_status WHERE id = :id),
                            ''
                          ) AS view_table
                        """
                    ),
                    {"id": int(vectorizer["id"]), "trigger_name": str(vectorizer["trigger_name"])},
                )
            ).mappings().first()
        except Exception:
            health = None

        if health and bool(health.get("trigger_exists")) and (
            (health.get("target_table") or "") or (health.get("view_table") or "")
        ):
            logger.info("pgai vectorizer for 'documents' is healthy, skipping creation.")
            return

        logger.warning(
            "Found broken pgai vectorizer '%s' (missing trigger/destination). Recreating.",
            vectorizer["name"],
        )
        needs_recreate = True
        try:
            await conn.execute(
                text("SELECT ai.drop_vectorizer(:name, drop_all => true)"),
                {"name": str(vectorizer["name"])},
            )
        except Exception as exc:
            logger.warning("Could not drop broken pgai vectorizer: %s", exc)

    try:
        result = await conn.execute(
            text("""
                SELECT count(*) FROM ai.vectorizer
                WHERE source_table = 'documents'
            """)
        )
        count = result.scalar()
        if (count and count > 0) and not needs_recreate:
            logger.info("pgai vectorizer for 'documents' already exists, skipping creation.")
            return
    except Exception:
        # Table ai.vectorizer might not exist yet if extension just enabled
        pass

    try:
        await conn.execute(text("""
            SELECT ai.create_vectorizer(
                'public.documents'::regclass,
                loading       => ai.loading_column('content'),
                embedding     => ai.embedding_openai('text-embedding-3-large', 1536),
                chunking      => ai.chunking_character_text_splitter(1500, 200),
                formatting    => ai.formatting_python_template('$chunk'),
                enqueue_existing => true,
                if_not_exists => true
            );
        """))
        logger.info("pgai vectorizer for 'documents' created successfully.")
    except Exception as exc:
        logger.warning("Could not create pgai vectorizer: %s", exc)


async def init_db():
    await asyncio.to_thread(_ensure_database_exists_sync)
    async with engine.begin() as conn:
        # Enable core extensions
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector CASCADE"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS ai CASCADE"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE"))

        # Create tables
        await conn.run_sync(SQLModel.metadata.create_all)
        # Backward-compatible schema bump for existing environments.
        await conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS conversation_sessions
                ADD COLUMN IF NOT EXISTS session_state JSONB NOT NULL DEFAULT '{}'::jsonb
                """
            )
        )

        # Setup pgai vectorizer (auto-generates embeddings via vectorizer-worker)
        await _setup_vectorizer(conn)


async def get_session():
    async with async_session() as session:
        yield session
