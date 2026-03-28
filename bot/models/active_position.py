"""ActivePosition model — tracks open positions for SL/TP/trailing stop management."""

from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, Float, String, Boolean, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class ActivePosition(Base):
    __tablename__ = "active_positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trade_id: Mapped[str] = mapped_column(String(128), index=True)  # FK to trades.trade_id

    # Market info
    market_id: Mapped[str] = mapped_column(String(128))
    token_id: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(32))  # "YES" or "NO"
    market_question: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # Entry
    entry_price: Mapped[float] = mapped_column(Float)
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    highest_price: Mapped[float] = mapped_column(Float, default=0.0)  # for trailing stop
    shares: Mapped[float] = mapped_column(Float, default=0.0)

    # Risk levels (computed at entry from user settings)
    sl_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tp_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trailing_stop_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Status
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    close_reason: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )  # "sl_hit", "tp_hit", "trailing_stop", "time_exit", "manual", "scale_out"
    close_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Timing
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_checked: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_active_positions_open", "user_id", "is_closed"),
    )

    @property
    def unrealized_pnl_pct(self) -> float:
        """Current unrealized P&L as a percentage."""
        if self.entry_price <= 0:
            return 0.0
        return ((self.current_price - self.entry_price) / self.entry_price) * 100

    def __repr__(self) -> str:
        status = "CLOSED" if self.is_closed else "OPEN"
        return (
            f"<ActivePosition user={self.user_id} {status} "
            f"entry={self.entry_price:.3f} current={self.current_price:.3f}>"
        )
