"""Trade model — records every copytrade execution."""

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Integer, Float, String, Enum, ForeignKey, BigInteger, Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow


class TradeStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"        # User confirmed (manual mode)
    FEE_PAID = "fee_paid"          # Platform fee transferred
    EXECUTING = "executing"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TradeSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # Market info
    market_id: Mapped[str] = mapped_column(String(255), nullable=False)
    market_slug: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    market_question: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Trade details
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    gross_amount_usdc: Mapped[float] = mapped_column(Float, nullable=False)
    fee_amount_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    net_amount_usdc: Mapped[float] = mapped_column(Float, nullable=False)
    shares: Mapped[float] = mapped_column(Float, default=0.0)

    # Master trade reference
    master_wallet: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    master_trade_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Status
    status: Mapped[TradeStatus] = mapped_column(
        Enum(TradeStatus), default=TradeStatus.PENDING
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Blockchain
    tx_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    fee_tx_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Bridge info (if bridge was used)
    bridge_used: Mapped[bool] = mapped_column(default=False)
    bridge_tx_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Timing
    execution_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    executed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    # Paper trading
    is_paper: Mapped[bool] = mapped_column(default=False)
    is_settled: Mapped[bool] = mapped_column(default=False)
    settlement_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    market_outcome: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="trades")
    fee_record: Mapped[Optional["FeeRecord"]] = relationship(
        "FeeRecord", back_populates="trade", uselist=False, lazy="selectin"
    )

    def __repr__(self) -> str:
        return (
            f"<Trade id={self.trade_id} market={self.market_id} "
            f"side={self.side.value} amount={self.net_amount_usdc}>"
        )


from .user import User  # noqa: E402, F401
from .fee import FeeRecord  # noqa: E402, F401
