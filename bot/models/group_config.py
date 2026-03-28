"""GroupConfig model — stores Telegram group + topic IDs for auto-setup."""

from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, String, Boolean, DateTime, BigInteger
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class GroupConfig(Base):
    __tablename__ = "group_config"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Telegram group info
    group_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    group_title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    is_forum: Mapped[bool] = mapped_column(Boolean, default=True)

    # Auto-created topic thread IDs
    topic_signals_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    topic_traders_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    topic_portfolio_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    topic_alerts_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    topic_admin_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    setup_complete: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def topics_dict(self) -> dict[str, Optional[int]]:
        """Return topic IDs as a dict for TopicRouter."""
        return {
            "signals": self.topic_signals_id,
            "traders": self.topic_traders_id,
            "portfolio": self.topic_portfolio_id,
            "alerts": self.topic_alerts_id,
            "admin": self.topic_admin_id,
        }

    @property
    def all_topics_created(self) -> bool:
        return all([
            self.topic_signals_id,
            self.topic_traders_id,
            self.topic_portfolio_id,
            self.topic_alerts_id,
            self.topic_admin_id,
        ])

    def __repr__(self) -> str:
        status = "READY" if self.setup_complete else "PENDING"
        return f"<GroupConfig group={self.group_id} [{status}]>"
