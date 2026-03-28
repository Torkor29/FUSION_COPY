"""TopicRouter — routes bot messages to the correct Telegram Forum Group topic.

Supports 5 topics: Signals, Traders, Portfolio, Alerts, Admin.

Configuration priority:
1. Database (GroupConfig) — auto-setup via group_setup handler
2. Environment variables (.env) — manual config
3. Disabled — DM-only fallback
"""

import logging
from typing import Optional

from telegram import Bot, Message, InlineKeyboardMarkup

from bot.config import settings

logger = logging.getLogger(__name__)


class TopicRouter:
    """Routes messages to Telegram Forum Group topics or DMs."""

    def __init__(self, bot: Bot):
        self._bot = bot
        self._group_id: Optional[int] = None
        self._topics: dict[str, Optional[int]] = {
            "signals": None,
            "traders": None,
            "portfolio": None,
            "alerts": None,
            "admin": None,
        }
        self._enabled = False

        # Try loading from .env first (backward compat)
        if settings.group_chat_id:
            try:
                self._group_id = int(settings.group_chat_id)
                self._topics = {
                    "signals": settings.topic_signals_id or None,
                    "traders": settings.topic_traders_id or None,
                    "portfolio": settings.topic_portfolio_id or None,
                    "alerts": settings.topic_alerts_id or None,
                    "admin": settings.topic_admin_id or None,
                }
                self._enabled = bool(any(self._topics.values()))
            except (ValueError, TypeError):
                pass

        if self._enabled:
            logger.info(
                "TopicRouter enabled from .env — group=%s topics=%s",
                self._group_id,
                {k: v for k, v in self._topics.items() if v},
            )
        else:
            logger.info(
                "TopicRouter not configured from .env — "
                "will try DB on first use or after auto-setup"
            )

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    async def try_load_from_db(self) -> bool:
        """Try to load group config from the database.

        Called at startup and after auto-setup.
        Returns True if config was loaded.
        """
        try:
            from bot.db.session import async_session
            from bot.models.group_config import GroupConfig
            from sqlalchemy import select

            async with async_session() as session:
                config = (
                    await session.execute(
                        select(GroupConfig)
                        .where(GroupConfig.is_active == True)  # noqa: E712
                        .where(GroupConfig.setup_complete == True)  # noqa: E712
                        .order_by(GroupConfig.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()

                if not config:
                    return False

                self._group_id = config.group_id
                self._topics = config.topics_dict
                self._enabled = True

                logger.info(
                    "TopicRouter loaded from DB: group=%s topics=%s",
                    config.group_id,
                    {k: v for k, v in config.topics_dict.items() if v},
                )
                return True

        except Exception as e:
            logger.debug("TopicRouter DB load failed: %s", e)
            return False

    async def _send_to_topic(
        self,
        topic_key: str,
        text: str,
        parse_mode: str = "Markdown",
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> Optional[Message]:
        """Send a message to a specific topic in the group."""
        # Lazy-load from DB if not yet enabled
        if not self._enabled:
            await self.try_load_from_db()
            if not self._enabled:
                return None

        topic_id = self._topics.get(topic_key)
        if not topic_id:
            logger.debug("Topic '%s' not configured, skipping", topic_key)
            return None

        try:
            return await self._bot.send_message(
                chat_id=self._group_id,
                message_thread_id=topic_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.error("Failed to send to topic '%s': %s", topic_key, e)
            return None

    # ── Topic-specific methods ────────────────────────────────────

    async def send_signal(
        self, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None
    ) -> Optional[Message]:
        """Post a scored signal to the 📊 Signals topic."""
        return await self._send_to_topic("signals", text, reply_markup=reply_markup)

    async def send_trader_report(self, text: str) -> Optional[Message]:
        """Post trader analytics to the 👤 Traders topic."""
        return await self._send_to_topic("traders", text)

    async def send_portfolio(self, text: str) -> Optional[Message]:
        """Post portfolio overview to the 💼 Portfolio topic."""
        return await self._send_to_topic("portfolio", text)

    async def send_alert(self, text: str) -> Optional[Message]:
        """Post critical alert to the 🚨 Alerts topic."""
        return await self._send_to_topic("alerts", text)

    async def send_admin(self, text: str) -> Optional[Message]:
        """Post system info to the ⚙️ Admin topic."""
        return await self._send_to_topic("admin", text)

    # ── Smart routing (DM + Group based on user preference) ───────

    async def notify_user(
        self,
        user_telegram_id: int,
        text: str,
        notification_mode: str = "dm",
        topic: str = "signals",
        parse_mode: str = "Markdown",
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> list[Message]:
        """Send notification respecting user's notification_mode preference.

        Args:
            user_telegram_id: User's Telegram ID for DM
            text: Message text
            notification_mode: "dm" | "group" | "both"
            topic: Which topic to post in if sending to group
            parse_mode: Telegram parse mode
            reply_markup: Optional inline keyboard

        Returns:
            List of Message objects sent
        """
        sent: list[Message] = []

        # DM
        if notification_mode in ("dm", "both"):
            try:
                msg = await self._bot.send_message(
                    chat_id=user_telegram_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                sent.append(msg)
            except Exception as e:
                logger.error("Failed to DM user %s: %s", user_telegram_id, e)

        # Group topic
        if notification_mode in ("group", "both"):
            msg = await self._send_to_topic(topic, text, parse_mode, reply_markup)
            if msg:
                sent.append(msg)

        return sent
