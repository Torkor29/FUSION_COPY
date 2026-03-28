"""TraderStats model — rolling performance tracking per followed wallet."""

from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, Float, String, Boolean, DateTime, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class TraderStats(Base):
    __tablename__ = "trader_stats"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    wallet: Mapped[str] = mapped_column(String(64), index=True)
    period: Mapped[str] = mapped_column(String(8))  # "24h", "7d", "30d"

    # Performance metrics
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100%
    avg_return_pct: Mapped[float] = mapped_column(Float, default=0.0)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # USDC
    trade_count: Mapped[int] = mapped_column(Integer, default=0)

    # Category breakdown
    best_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    worst_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Risk metrics
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Streak tracking — positive = consecutive wins, negative = consecutive losses
    current_streak: Mapped[int] = mapped_column(Integer, default=0)

    # Status flags (computed from win_rate + trade_count)
    is_hot: Mapped[bool] = mapped_column(Boolean, default=False)
    is_cold: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_paused: Mapped[bool] = mapped_column(Boolean, default=False)

    last_updated: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("wallet", "period", name="uq_trader_stats_wallet_period"),
        Index("ix_trader_stats_wallet_period", "wallet", "period"),
    )

    def __repr__(self) -> str:
        status = "HOT" if self.is_hot else ("COLD" if self.is_cold else "OK")
        return (
            f"<TraderStats {self.wallet[:10]} {self.period} "
            f"WR={self.win_rate:.0f}% trades={self.trade_count} [{status}]>"
        )
