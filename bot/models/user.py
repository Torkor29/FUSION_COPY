"""User model — stores follower/admin accounts."""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Boolean, Float, BigInteger, Enum, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    FOLLOWER = "follower"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4())
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    telegram_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), nullable=False, default=UserRole.FOLLOWER
    )

    # Wallet info
    wallet_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    wallet_auto_created: Mapped[bool] = mapped_column(Boolean, default=False)
    # Encrypted private key (AES-256-GCM) — NEVER stored in plaintext
    encrypted_private_key: Mapped[Optional[bytes]] = mapped_column(nullable=True)
    # Solana wallet (for bridging)
    solana_wallet_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    encrypted_solana_key: Mapped[Optional[bytes]] = mapped_column(nullable=True)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paper_trading: Mapped[bool] = mapped_column(Boolean, default=True)
    polymarket_approved: Mapped[bool] = mapped_column(Boolean, default=False)

    # Limits
    daily_limit_usdc: Mapped[float] = mapped_column(Float, default=1000.0)
    daily_spent_usdc: Mapped[float] = mapped_column(Float, default=0.0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    # Relationships
    settings: Mapped[Optional["UserSettings"]] = relationship(
        "UserSettings", back_populates="user", uselist=False, lazy="selectin"
    )
    trades: Mapped[list["Trade"]] = relationship(
        "Trade", back_populates="user", lazy="selectin"
    )
    fees: Mapped[list["FeeRecord"]] = relationship(
        "FeeRecord", back_populates="user", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} tg={self.telegram_id} role={self.role.value}>"


# Avoid circular import — these are resolved at runtime by SQLAlchemy
from .settings import UserSettings  # noqa: E402, F401
from .trade import Trade  # noqa: E402, F401
from .fee import FeeRecord  # noqa: E402, F401
