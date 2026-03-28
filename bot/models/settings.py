"""UserSettings model — per-follower copytrade configuration."""

import enum
from typing import Optional

from sqlalchemy import (
    Integer, Float, String, Boolean, Enum, ForeignKey, JSON,  # noqa: F401
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class SizingMode(str, enum.Enum):
    FIXED = "fixed"               # Fixed USDC amount per trade
    PERCENT = "percent"           # % of allocated capital per trade
    PROPORTIONAL = "proportional" # Proportional to master trader size
    KELLY = "kelly"               # Kelly criterion (advanced)


class GasMode(str, enum.Enum):
    NORMAL = "normal"   # 30 gwei priority fee — standard, ~2s confirmation
    FAST = "fast"       # 50 gwei priority fee — faster, ~1-1.5s confirmation
    ULTRA = "ultra"     # 100 gwei priority fee — fastest, <1s confirmation
    INSTANT = "instant" # 200 gwei priority fee — maximum speed, costs more POL


# Priority fee in gwei for each gas mode
GAS_PRIORITY_FEES = {
    GasMode.NORMAL: 30,
    GasMode.FAST: 50,
    GasMode.ULTRA: 100,
    GasMode.INSTANT: 200,
}

# Labels for UI display
GAS_MODE_LABELS = {
    GasMode.NORMAL: "🐢 Normal (30 gwei) — ~2s",
    GasMode.FAST: "🚀 Fast (50 gwei) — ~1.5s",
    GasMode.ULTRA: "⚡ Ultra (100 gwei) — <1s",
    GasMode.INSTANT: "💎 Instant (200 gwei) — max speed",
}


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    # Capital
    allocated_capital: Mapped[float] = mapped_column(Float, default=100.0)
    sizing_mode: Mapped[SizingMode] = mapped_column(
        Enum(SizingMode), default=SizingMode.FIXED
    )
    fixed_amount: Mapped[float] = mapped_column(Float, default=10.0)
    percent_per_trade: Mapped[float] = mapped_column(Float, default=5.0)
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)

    # Risk management
    stop_loss_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=20.0)
    take_profit_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    take_profit_pct: Mapped[float] = mapped_column(Float, default=50.0)
    max_trade_usdc: Mapped[float] = mapped_column(Float, default=100.0)
    min_trade_usdc: Mapped[float] = mapped_column(Float, default=1.0)

    # Copy behavior
    copy_delay_seconds: Mapped[int] = mapped_column(Integer, default=0)
    manual_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmation_threshold_usdc: Mapped[float] = mapped_column(Float, default=50.0)

    # Bridge
    auto_bridge_sol: Mapped[bool] = mapped_column(Boolean, default=False)

    # Followed traders (list of Polygon wallet addresses to copy)
    followed_wallets: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    # Filters
    categories: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    blacklisted_markets: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    max_expiry_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Per-trader category filters
    # Format: {"0xwallet": {"excluded_categories": ["Crypto/XRP", "Sports"]}, ...}
    trader_filters: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)

    # Gas priority mode — controls how fast transactions confirm on Polygon
    gas_mode: Mapped[GasMode] = mapped_column(
        Enum(GasMode), default=GasMode.FAST
    )

    # Monitor mode (master tracking)
    # Ces flags décrivent comment ce follower souhaite que les masters
    # soient suivis. Dans la pratique, pour un bot mono-admin, ils servent
    # aussi de configuration globale lisible dans l'UI.
    use_gamma_monitor: Mapped[bool] = mapped_column(Boolean, default=True)
    use_ws_monitor: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── V3 — Smart Analysis Settings ──────────────────────────────

    # Notification routing: "dm" | "group" | "both"
    notification_mode: Mapped[str] = mapped_column(String(8), default="dm")

    # Signal Scoring
    signal_scoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_signal_score: Mapped[float] = mapped_column(Float, default=40.0)  # 0-100

    # Per-criterion config: which scoring criteria are active + custom weights
    # Format: {"spread": {"on": true, "w": 15}, "liquidity": {"on": true, "w": 15}, ...}
    # If a criterion is off, its weight is redistributed to the others.
    # If not set, all criteria are ON with default weights.
    scoring_criteria: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    # Trader tracking
    auto_pause_cold_traders: Mapped[bool] = mapped_column(Boolean, default=True)
    cold_trader_threshold: Mapped[float] = mapped_column(Float, default=40.0)  # win rate %
    hot_streak_boost: Mapped[float] = mapped_column(Float, default=1.5)  # sizing multiplier

    # Position management (active SL/TP enforcement)
    trailing_stop_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    trailing_stop_pct: Mapped[float] = mapped_column(Float, default=10.0)
    time_exit_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    time_exit_hours: Mapped[int] = mapped_column(Integer, default=24)
    scale_out_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    scale_out_pct: Mapped[float] = mapped_column(Float, default=50.0)  # % to take at TP1

    # Portfolio risk controls
    max_positions: Mapped[int] = mapped_column(Integer, default=15)
    max_category_exposure_pct: Mapped[float] = mapped_column(Float, default=30.0)
    max_direction_bias_pct: Mapped[float] = mapped_column(Float, default=70.0)

    # Smart filter
    smart_filter_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_trader_winrate_for_type: Mapped[float] = mapped_column(Float, default=55.0)
    min_trader_trades_for_type: Mapped[int] = mapped_column(Integer, default=10)
    skip_coin_flip: Mapped[bool] = mapped_column(Boolean, default=True)
    min_conviction_pct: Mapped[float] = mapped_column(Float, default=2.0)
    max_price_drift_pct: Mapped[float] = mapped_column(Float, default=5.0)

    # Relationship
    user: Mapped["User"] = relationship("User", back_populates="settings")

    def __repr__(self) -> str:
        return f"<UserSettings user_id={self.user_id} mode={self.sizing_mode.value}>"


from .user import User  # noqa: E402, F401
