"""Async SQLAlchemy session factory."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.config import settings
from bot.models.base import Base

engine = create_async_engine(
    settings.db_url,
    echo=False,
    pool_pre_ping=True,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create all tables and apply lightweight column migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Add columns that may not exist yet on already-created tables.
        # Idempotent — silently ignored if the column already exists.
        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS polymarket_approved BOOLEAN DEFAULT false",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_auto_created BOOLEAN DEFAULT false",
        ]
        for stmt in migrations:
            await conn.execute(text(stmt))


async def get_session() -> AsyncSession:
    """Get an async database session."""
    async with async_session() as session:
        yield session
