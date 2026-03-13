"""UserSettings model — per-follower copytrade configuration."""

import enum
from typing import Optional

from sqlalchemy import (
    Integer, Float, String, Boolean, Enum, ForeignKey, JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class SizingMode(str, enum.Enum):
    FIXED = "fixed"               # Fixed USDC amount per trade
    PERCENT = "percent"           # % of allocated capital per trade
    PROPORTIONAL = "proportional" # Proportional to master trader size
    KELLY = "kelly"               # Kelly criterion (advanced)


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

    # Monitor mode (master tracking)
    # Ces flags décrivent comment ce follower souhaite que les masters
    # soient suivis. Dans la pratique, pour un bot mono-admin, ils servent
    # aussi de configuration globale lisible dans l'UI.
    use_gamma_monitor: Mapped[bool] = mapped_column(Boolean, default=True)
    use_ws_monitor: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationship
    user: Mapped["User"] = relationship("User", back_populates="settings")

    def __repr__(self) -> str:
        return f"<UserSettings user_id={self.user_id} mode={self.sizing_mode.value}>"


from .user import User  # noqa: E402, F401
