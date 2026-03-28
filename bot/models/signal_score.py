"""SignalScore model — scored trade signal with component breakdown."""

import hashlib
from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, Float, String, Boolean, JSON, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class SignalScore(Base):
    __tablename__ = "signal_scores"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Unique hash = md5(master_wallet + market_id + token_id + side + timestamp_bucket)
    signal_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    master_wallet: Mapped[str] = mapped_column(String(64), index=True)
    market_id: Mapped[str] = mapped_column(String(128))
    token_id: Mapped[str] = mapped_column(String(128))
    side: Mapped[str] = mapped_column(String(8))  # "BUY" or "SELL"

    # Score 0-100
    total_score: Mapped[float] = mapped_column(Float, default=0.0)

    # Component breakdown: {"spread": 12, "liquidity": 15, "conviction": 18, ...}
    components: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)

    # Whether this score passed the user's minimum threshold
    passed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_signal_scores_wallet_created", "master_wallet", "created_at"),
    )

    @staticmethod
    def make_hash(master_wallet: str, market_id: str, token_id: str, side: str) -> str:
        """Create a unique signal hash for deduplication."""
        # Bucket by 60s windows to allow re-scoring if needed
        ts_bucket = int(datetime.utcnow().timestamp()) // 60
        raw = f"{master_wallet}:{market_id}:{token_id}:{side}:{ts_bucket}"
        return hashlib.md5(raw.encode()).hexdigest()

    def __repr__(self) -> str:
        return f"<SignalScore {self.total_score:.0f}/100 wallet={self.master_wallet[:10]}>"
