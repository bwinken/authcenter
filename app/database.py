"""Dual database connection management: MySQL (read-only) + SQLite (read-write)."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

# --- MySQL Engine (IT Master DB - Read Only) ---
mysql_engine = create_async_engine(
    settings.mysql_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)
MySQLSessionLocal = async_sessionmaker(mysql_engine, expire_on_commit=False)

# --- SQLite Engine (Auth Local DB - Read/Write) ---
sqlite_engine = create_async_engine(
    settings.sqlite_url,
    echo=False,
)
SQLiteSessionLocal = async_sessionmaker(sqlite_engine, expire_on_commit=False)


async def get_mysql_session() -> AsyncGenerator[AsyncSession]:
    """Dependency: yields a read-only MySQL session."""
    async with MySQLSessionLocal() as session:
        yield session


async def get_sqlite_session() -> AsyncGenerator[AsyncSession]:
    """Dependency: yields a read-write SQLite session."""
    async with SQLiteSessionLocal() as session:
        yield session
