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
        # Idempotent — silencieux si la colonne existe déjà.
        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS polymarket_approved BOOLEAN DEFAULT false",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_auto_created BOOLEAN DEFAULT false",
            # user_settings: mode de suivi des masters (Gamma vs WebSocket)
            "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS use_gamma_monitor BOOLEAN DEFAULT true",
            "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS use_ws_monitor BOOLEAN DEFAULT false",
            # user_settings: stop-loss / take-profit toggles
            "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS stop_loss_enabled BOOLEAN DEFAULT true",
            "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS take_profit_enabled BOOLEAN DEFAULT false",
            "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS take_profit_pct FLOAT DEFAULT 50.0",
            # Paper trading columns
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS paper_trading BOOLEAN DEFAULT true",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS paper_balance FLOAT DEFAULT 1000.0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS paper_initial_balance FLOAT DEFAULT 1000.0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS live_mode_confirmed BOOLEAN DEFAULT false",
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS is_paper BOOLEAN DEFAULT false",
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS is_settled BOOLEAN DEFAULT false",
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS settlement_pnl FLOAT",
        ]
        for stmt in migrations:
            await conn.execute(text(stmt))

        # SAFETY: force paper mode for users who never explicitly confirmed live
        await conn.execute(text(
            "UPDATE users SET paper_trading = true "
            "WHERE paper_trading = false AND "
            "(live_mode_confirmed IS NULL OR live_mode_confirmed = false)"
        ))


async def get_session() -> AsyncSession:
    """Get an async database session."""
    async with async_session() as session:
        yield session
