"""Async SQLAlchemy engine and schema bootstrap (SQLite via aiosqlite).

Startup uses ``metadata.create_all``. Breaking schema changes should land
through Alembic later; this module does not run migrations.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app import config


class Base(DeclarativeBase):
    """Declarative base for job tables."""


def sqlite_file_path(database_url: str) -> Path | None:
    """Return the on-disk path for a sqlite+aiosqlite URL, if any."""
    if "sqlite" not in database_url.split(":", 1)[0]:
        return None
    if ":///" not in database_url:
        return None
    raw = database_url.split(":///", 1)[1]
    if not raw or raw == ":memory:" or raw.startswith(":memory:"):
        return None
    return Path(raw)


def ensure_sqlite_parent(database_url: str) -> None:
    path = sqlite_file_path(database_url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)


def _register_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def create_db_engine(database_url: str | None = None) -> AsyncEngine:
    url = config.DATABASE_URL if database_url is None else database_url
    ensure_sqlite_parent(url)
    engine = create_async_engine(url)
    if "sqlite" in url:
        _register_sqlite_pragmas(engine)
    return engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create tables if they do not exist (no Alembic)."""
    # Import so table metadata is registered on Base.
    from app.jobs import tables as _tables  # noqa: F401

    async with engine.begin() as conn:
        if engine.url.get_backend_name().startswith("sqlite"):
            await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)
