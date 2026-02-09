import asyncio

import psycopg
from psycopg import sql
from psycopg.errors import DuplicateDatabase
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from app.config import settings


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


async def init_db():
    await asyncio.to_thread(_ensure_database_exists_sync)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session():
    async with async_session() as session:
        yield session
