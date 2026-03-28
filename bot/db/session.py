"""Async SQLAlchemy session factory."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.config import settings
from bot.models.base import Base

logger = logging.getLogger(__name__)

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


async def _safe_add_column(conn, stmt: str) -> None:
    """Execute an ALTER TABLE ADD COLUMN, ignoring 'duplicate column' errors.

    PostgreSQL supports ``IF NOT EXISTS`` natively.
    Older SQLite (< 3.35) does not — we catch the duplicate-column error instead.
    """
    try:
        await conn.execute(text(stmt))
    except Exception as exc:
        err = str(exc).lower()
        if "duplicate column" in err or "already exists" in err:
            logger.debug("Column already exists, skipping: %s", stmt[:80])
        else:
            raise


async def init_db() -> None:
    """Create all tables and apply lightweight column migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        migrations = [
            "ALTER TABLE users ADD COLUMN polymarket_approved BOOLEAN DEFAULT false",
            "ALTER TABLE users ADD COLUMN wallet_auto_created BOOLEAN DEFAULT false",
            "ALTER TABLE user_settings ADD COLUMN use_gamma_monitor BOOLEAN DEFAULT true",
            "ALTER TABLE user_settings ADD COLUMN use_ws_monitor BOOLEAN DEFAULT false",
            "ALTER TABLE user_settings ADD COLUMN stop_loss_enabled BOOLEAN DEFAULT true",
            "ALTER TABLE user_settings ADD COLUMN take_profit_enabled BOOLEAN DEFAULT false",
            "ALTER TABLE user_settings ADD COLUMN take_profit_pct FLOAT DEFAULT 50.0",
            "ALTER TABLE users ADD COLUMN paper_trading BOOLEAN DEFAULT true",
            "ALTER TABLE users ADD COLUMN paper_balance FLOAT DEFAULT 1000.0",
            "ALTER TABLE users ADD COLUMN paper_initial_balance FLOAT DEFAULT 1000.0",
            "ALTER TABLE users ADD COLUMN live_mode_confirmed BOOLEAN DEFAULT false",
            "ALTER TABLE trades ADD COLUMN is_paper BOOLEAN DEFAULT false",
            "ALTER TABLE trades ADD COLUMN is_settled BOOLEAN DEFAULT false",
            "ALTER TABLE trades ADD COLUMN settlement_pnl FLOAT",
            "ALTER TABLE trades ADD COLUMN market_outcome VARCHAR(64)",
            "ALTER TABLE user_settings ADD COLUMN gas_mode VARCHAR(10) DEFAULT 'fast'",
            # ── V3 Smart Analysis migrations ──
            # Notification routing
            "ALTER TABLE user_settings ADD COLUMN notification_mode VARCHAR(8) DEFAULT 'dm'",
            # Signal Scoring
            "ALTER TABLE user_settings ADD COLUMN signal_scoring_enabled BOOLEAN DEFAULT true",
            "ALTER TABLE user_settings ADD COLUMN min_signal_score FLOAT DEFAULT 40.0",
            # Trader tracking
            "ALTER TABLE user_settings ADD COLUMN auto_pause_cold_traders BOOLEAN DEFAULT true",
            "ALTER TABLE user_settings ADD COLUMN cold_trader_threshold FLOAT DEFAULT 40.0",
            "ALTER TABLE user_settings ADD COLUMN hot_streak_boost FLOAT DEFAULT 1.5",
            # Position management
            "ALTER TABLE user_settings ADD COLUMN trailing_stop_enabled BOOLEAN DEFAULT false",
            "ALTER TABLE user_settings ADD COLUMN trailing_stop_pct FLOAT DEFAULT 10.0",
            "ALTER TABLE user_settings ADD COLUMN time_exit_enabled BOOLEAN DEFAULT false",
            "ALTER TABLE user_settings ADD COLUMN time_exit_hours INTEGER DEFAULT 24",
            "ALTER TABLE user_settings ADD COLUMN scale_out_enabled BOOLEAN DEFAULT false",
            "ALTER TABLE user_settings ADD COLUMN scale_out_pct FLOAT DEFAULT 50.0",
            # Portfolio risk controls
            "ALTER TABLE user_settings ADD COLUMN max_positions INTEGER DEFAULT 15",
            "ALTER TABLE user_settings ADD COLUMN max_category_exposure_pct FLOAT DEFAULT 30.0",
            "ALTER TABLE user_settings ADD COLUMN max_direction_bias_pct FLOAT DEFAULT 70.0",
            # Smart filter
            "ALTER TABLE user_settings ADD COLUMN smart_filter_enabled BOOLEAN DEFAULT true",
            "ALTER TABLE user_settings ADD COLUMN min_trader_winrate_for_type FLOAT DEFAULT 55.0",
            "ALTER TABLE user_settings ADD COLUMN min_trader_trades_for_type INTEGER DEFAULT 10",
            "ALTER TABLE user_settings ADD COLUMN skip_coin_flip BOOLEAN DEFAULT true",
            "ALTER TABLE user_settings ADD COLUMN min_conviction_pct FLOAT DEFAULT 2.0",
            "ALTER TABLE user_settings ADD COLUMN max_price_drift_pct FLOAT DEFAULT 5.0",
            "ALTER TABLE user_settings ADD COLUMN scoring_criteria TEXT",
        ]
        for stmt in migrations:
            await _safe_add_column(conn, stmt)

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
