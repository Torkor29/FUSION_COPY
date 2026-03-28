"""TraderMarketHistory model — per-trader per-market-type win/loss tracking."""

from datetime import datetime

from sqlalchemy import Integer, Float, String, DateTime, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class TraderMarketHistory(Base):
    __tablename__ = "trader_market_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    wallet: Mapped[str] = mapped_column(String(64), index=True)
    # Market type string, e.g. "crypto_btc_5min", "crypto_btc_daily",
    # "politics_us", "sports_nfl", etc.
    market_type: Mapped[str] = mapped_column(String(64))

    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    avg_return_pct: Mapped[float] = mapped_column(Float, default=0.0)

    last_updated: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("wallet", "market_type", name="uq_trader_market_wallet_type"),
        Index("ix_trader_market_wallet_type", "wallet", "market_type"),
    )

    @property
    def win_rate(self) -> float:
        """Win rate as percentage 0-100."""
        if self.trades_count == 0:
            return 0.0
        return (self.wins / self.trades_count) * 100

    def __repr__(self) -> str:
        return (
            f"<TraderMarketHistory {self.wallet[:10]} type={self.market_type} "
            f"W={self.wins} L={self.losses} WR={self.win_rate:.0f}%>"
        )
