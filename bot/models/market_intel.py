"""MarketIntel model — cached market intelligence data."""

from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, Float, String, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class MarketIntel(Base):
    __tablename__ = "market_intel"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    market_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    question: Mapped[str] = mapped_column(String(512), default="")
    category: Mapped[str] = mapped_column(String(64), default="Other")
    subcategory: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Timing
    expiry: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Volume & Activity
    volume_24h: Mapped[float] = mapped_column(Float, default=0.0)  # USDC
    open_interest: Mapped[float] = mapped_column(Float, default=0.0)
    holders_count: Mapped[int] = mapped_column(Integer, default=0)

    # Price & Spread
    spread_avg: Mapped[float] = mapped_column(Float, default=0.0)  # %
    price_current: Mapped[float] = mapped_column(Float, default=0.0)
    price_1h_ago: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_6h_ago: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_24h_ago: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Computed
    momentum_1h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # % change
    liquidity_score: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100
    is_coin_flip: Mapped[bool] = mapped_column(Boolean, default=False)  # price 0.45-0.55

    last_updated: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:
        flip = " COINFLIP" if self.is_coin_flip else ""
        return (
            f"<MarketIntel {self.market_id[:16]} "
            f"vol24h=${self.volume_24h:,.0f} liq={self.liquidity_score:.0f}{flip}>"
        )
