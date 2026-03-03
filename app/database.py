"""Dual database connection management: MSSQL (read-only) + SQLite (read-write)."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

# --- MSSQL Engine (IT Master DB - Read Only) ---
mssql_engine = create_async_engine(
    settings.mssql_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)
MSSQLSessionLocal = async_sessionmaker(mssql_engine, expire_on_commit=False)

# --- SQLite Engine (Auth Local DB - Read/Write) ---
sqlite_engine = create_async_engine(
    settings.sqlite_url,
    echo=False,
)
SQLiteSessionLocal = async_sessionmaker(sqlite_engine, expire_on_commit=False)


async def get_mssql_session() -> AsyncGenerator[AsyncSession]:
    """Dependency: yields a read-only MSSQL session."""
    async with MSSQLSessionLocal() as session:
        yield session


async def get_sqlite_session() -> AsyncGenerator[AsyncSession]:
    """Dependency: yields a read-write SQLite session."""
    async with SQLiteSessionLocal() as session:
        yield session
