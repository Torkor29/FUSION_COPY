"""PositionManager — active SL/TP/trailing stop enforcement.

Monitors all open positions every N seconds and auto-exits when:
- Stop-loss price hit
- Take-profit price hit
- Trailing stop triggered (price drops X% from peak)
- Time-based exit (position flat after N hours)
- Scale-out (take partial profit at TP1)
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select, and_

from bot.db.session import async_session
from bot.models.active_position import ActivePosition
from bot.models.base import utcnow

logger = logging.getLogger(__name__)


class PositionManager:
    """Monitors open positions and enforces SL/TP/trailing stop rules."""

    def __init__(
        self,
        polymarket_client=None,
        topic_router=None,
        check_interval: int = 15,
    ):
        self._pm = polymarket_client
        self._topic_router = topic_router
        self._check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Callback for executing exits — will be set by CopyTradeEngine
        self._on_exit_callback = None

    def set_exit_callback(self, callback):
        """Set the callback function for executing position exits.

        Called with: callback(user_id, position, reason)
        """
        self._on_exit_callback = callback

    async def start(self):
        """Start the position monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info(
            "PositionManager started — checking every %ds", self._check_interval
        )

    async def stop(self):
        """Stop the position monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("PositionManager stopped")

    async def register_position(
        self,
        user_id: int,
        trade_id: str,
        market_id: str,
        token_id: str,
        outcome: str,
        entry_price: float,
        shares: float,
        market_question: str = "",
        sl_pct: Optional[float] = None,
        tp_pct: Optional[float] = None,
        trailing_stop_pct: Optional[float] = None,
    ) -> ActivePosition:
        """Register a new position for monitoring.

        SL/TP prices are computed from entry_price and user settings.
        """
        sl_price = None
        if sl_pct is not None and sl_pct > 0:
            sl_price = round(entry_price * (1 - sl_pct / 100), 6)

        tp_price = None
        if tp_pct is not None and tp_pct > 0:
            tp_price = round(entry_price * (1 + tp_pct / 100), 6)

        pos = ActivePosition(
            user_id=user_id,
            trade_id=trade_id,
            market_id=market_id,
            token_id=token_id,
            outcome=outcome,
            market_question=market_question[:512] if market_question else None,
            entry_price=entry_price,
            current_price=entry_price,
            highest_price=entry_price,
            shares=shares,
            sl_price=sl_price,
            tp_price=tp_price,
            trailing_stop_pct=trailing_stop_pct,
            opened_at=utcnow(),
            last_checked=utcnow(),
        )

        async with async_session() as session:
            session.add(pos)
            await session.commit()
            await session.refresh(pos)

        logger.info(
            "Registered position: user=%d trade=%s entry=%.4f SL=%.4f TP=%s",
            user_id,
            trade_id,
            entry_price,
            sl_price or 0,
            tp_price or "none",
        )

        return pos

    async def get_open_positions(self, user_id: Optional[int] = None) -> list[ActivePosition]:
        """Get all open (unclosed) positions, optionally filtered by user."""
        async with async_session() as session:
            conditions = [ActivePosition.is_closed == False]  # noqa: E712
            if user_id is not None:
                conditions.append(ActivePosition.user_id == user_id)

            stmt = select(ActivePosition).where(and_(*conditions))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_position_count(self, user_id: int) -> int:
        """Count open positions for a user."""
        positions = await self.get_open_positions(user_id)
        return len(positions)

    # ── Main monitoring loop ──────────────────────────────────────

    async def _check_loop(self):
        """Main loop: check all open positions for exit conditions."""
        while self._running:
            try:
                await self._check_all_positions()
            except Exception as e:
                logger.error("Position check error: %s", e, exc_info=True)

            await asyncio.sleep(self._check_interval)

    async def _check_all_positions(self):
        """Fetch current prices and check SL/TP/trailing for all open positions."""
        positions = await self.get_open_positions()
        if not positions:
            return

        # Batch price fetches by token_id to minimize API calls
        token_ids = list({p.token_id for p in positions})
        prices = {}
        for token_id in token_ids:
            try:
                if self._pm:
                    price = await self._pm.get_price(token_id, "SELL")
                    prices[token_id] = price
            except Exception as e:
                logger.debug("Failed to get price for %s: %s", token_id[:16], e)

        # Check each position
        for pos in positions:
            current_price = prices.get(pos.token_id)
            if current_price is None:
                continue

            # Update current price and highest price
            pos.current_price = current_price
            if current_price > pos.highest_price:
                pos.highest_price = current_price
            pos.last_checked = utcnow()

            # Check exit conditions
            reason = self._check_exit_conditions(pos)

            if reason:
                await self._execute_exit(pos, reason)
            else:
                # Just update prices in DB
                await self._update_position(pos)

    def _check_exit_conditions(self, pos: ActivePosition) -> Optional[str]:
        """Check if position should be closed.

        Returns close_reason string or None.
        """
        price = pos.current_price

        # 1. Stop-loss
        if pos.sl_price is not None and price <= pos.sl_price:
            return "sl_hit"

        # 2. Take-profit
        if pos.tp_price is not None and price >= pos.tp_price:
            return "tp_hit"

        # 3. Trailing stop
        if pos.trailing_stop_pct is not None and pos.trailing_stop_pct > 0:
            trail_trigger = pos.highest_price * (1 - pos.trailing_stop_pct / 100)
            if price <= trail_trigger and pos.highest_price > pos.entry_price:
                return "trailing_stop"

        # 4. Time-based exit (checked by caller with user settings)
        # This is handled in the scheduler, not here, since it needs user settings

        return None

    async def _execute_exit(self, pos: ActivePosition, reason: str):
        """Execute a position exit and notify."""
        # Mark as closed in DB
        pos.is_closed = True
        pos.close_reason = reason
        pos.close_price = pos.current_price
        pos.closed_at = utcnow()

        if pos.entry_price > 0:
            pos.pnl_pct = round(
                ((pos.current_price - pos.entry_price) / pos.entry_price) * 100, 2
            )
        else:
            pos.pnl_pct = 0.0

        await self._update_position(pos)

        # Format alert
        # Format notification using the dedicated template
        from bot.handlers.notifications import format_position_exit

        alert_text = format_position_exit(
            market_question=pos.market_question or pos.market_id[:20],
            reason=reason,
            entry_price=pos.entry_price,
            exit_price=pos.current_price,
            pnl_pct=pos.pnl_pct or 0,
            shares=pos.shares,
        )

        # Send alert to the user's own group (multi-tenant), fallback to global
        try:
            from bot.services.topic_router import TopicRouter
            bot = getattr(self._topic_router, "_bot", None)
            user_router = await TopicRouter.for_user(pos.user_id, bot) if bot else None
            effective_router = user_router or self._topic_router
            if effective_router:
                await effective_router.send_alert(alert_text)
        except Exception as _e:
            logger.warning("Failed to send position exit alert: %s", _e)

        # Execute the actual sell via callback
        if self._on_exit_callback:
            try:
                await self._on_exit_callback(pos.user_id, pos, reason)
            except Exception as e:
                logger.error("Exit callback failed for position %s: %s", pos.id, e)

        market_name = pos.market_question or pos.market_id
        logger.info(
            "Position exit: user=%d reason=%s market=%s pnl=%.1f%%",
            pos.user_id,
            reason,
            market_name[:30],
            pos.pnl_pct or 0,
        )

    async def _update_position(self, pos: ActivePosition):
        """Update position in database."""
        try:
            async with async_session() as session:
                existing = (
                    await session.execute(
                        select(ActivePosition).where(ActivePosition.id == pos.id)
                    )
                ).scalar_one_or_none()

                if existing:
                    existing.current_price = pos.current_price
                    existing.highest_price = pos.highest_price
                    existing.last_checked = pos.last_checked
                    existing.is_closed = pos.is_closed
                    existing.close_reason = pos.close_reason
                    existing.close_price = pos.close_price
                    existing.closed_at = pos.closed_at
                    existing.pnl_pct = pos.pnl_pct

                await session.commit()
        except Exception as e:
            logger.debug("Failed to update position %s: %s", pos.id, e)

    # ── Time-based exit check (called from scheduler) ─────────────

    async def check_time_exits(self, time_exit_hours: int = 24):
        """Check for positions that should be exited due to time.

        Called from scheduler, not the main loop, since it needs user settings.
        """
        now = utcnow()
        positions = await self.get_open_positions()

        for pos in positions:
            hours_open = (now - pos.opened_at).total_seconds() / 3600
            if hours_open >= time_exit_hours:
                # Check if position is flat (< 2% change)
                if pos.entry_price > 0:
                    change_pct = abs(
                        (pos.current_price - pos.entry_price) / pos.entry_price * 100
                    )
                    if change_pct < 2.0:
                        await self._execute_exit(pos, "time_exit")
