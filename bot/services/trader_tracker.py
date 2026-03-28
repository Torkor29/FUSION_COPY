"""TraderTracker — continuous performance tracking for followed wallets.

Tracks rolling stats (24h, 7d, 30d), detects hot/cold streaks,
auto-pauses underperforming traders, and provides sizing multipliers.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, and_, case
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from bot.db.session import async_session
from bot.models.trader_stats import TraderStats
from bot.models.trader_market_history import TraderMarketHistory
from bot.models.trade import Trade, TradeStatus
from bot.models.base import utcnow

logger = logging.getLogger(__name__)

# Period definitions
PERIODS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


class TraderTracker:
    """Tracks and analyzes performance of followed trader wallets."""

    def __init__(self, topic_router=None):
        self._topic_router = topic_router
        # In-memory cache for fast lookups
        self._stats_cache: dict[str, dict[str, TraderStats]] = {}

    async def recalculate_stats(self, wallet: str) -> dict[str, TraderStats]:
        """Recalculate 24h/7d/30d stats from settled Trade records.

        Returns dict keyed by period: {"24h": TraderStats, "7d": ..., "30d": ...}
        """
        results = {}
        now = utcnow()

        async with async_session() as session:
            for period_name, delta in PERIODS.items():
                cutoff = now - delta

                # Query settled trades for this master wallet in this period
                stmt = select(
                    func.count(Trade.id).label("total"),
                    func.sum(
                        case(
                            (Trade.settlement_pnl > 0, 1),
                            else_=0,
                        )
                    ).label("wins"),
                    func.sum(
                        case(
                            (Trade.settlement_pnl < 0, 1),
                            else_=0,
                        )
                    ).label("losses"),
                    func.avg(Trade.settlement_pnl).label("avg_pnl"),
                    func.sum(Trade.settlement_pnl).label("total_pnl"),
                ).where(
                    and_(
                        Trade.master_wallet == wallet,
                        Trade.is_settled == True,  # noqa: E712
                        Trade.created_at >= cutoff,
                        Trade.status == TradeStatus.FILLED,
                    )
                )
                row = (await session.execute(stmt)).first()

                total = row.total or 0
                wins = row.wins or 0
                losses = row.losses or 0
                win_rate = (wins / total * 100) if total > 0 else 0.0
                avg_return = float(row.avg_pnl or 0.0)
                total_pnl = float(row.total_pnl or 0.0)

                # Streak calculation (last N trades ordered by time)
                streak = await self._calculate_streak(session, wallet, cutoff)

                # Category breakdown
                best_cat, worst_cat = await self._category_breakdown(
                    session, wallet, cutoff
                )

                # Status flags
                is_hot = win_rate >= 65 and total >= 10
                is_cold = win_rate <= 40 and total >= 15

                # Upsert into DB
                stats = TraderStats(
                    wallet=wallet,
                    period=period_name,
                    win_rate=round(win_rate, 1),
                    avg_return_pct=round(avg_return, 2),
                    total_pnl=round(total_pnl, 2),
                    trade_count=total,
                    best_category=best_cat,
                    worst_category=worst_cat,
                    current_streak=streak,
                    is_hot=is_hot,
                    is_cold=is_cold,
                    last_updated=now,
                )

                # Try to update existing, or insert new
                existing = (
                    await session.execute(
                        select(TraderStats).where(
                            and_(
                                TraderStats.wallet == wallet,
                                TraderStats.period == period_name,
                            )
                        )
                    )
                ).scalar_one_or_none()

                if existing:
                    existing.win_rate = stats.win_rate
                    existing.avg_return_pct = stats.avg_return_pct
                    existing.total_pnl = stats.total_pnl
                    existing.trade_count = stats.trade_count
                    existing.best_category = stats.best_category
                    existing.worst_category = stats.worst_category
                    existing.current_streak = stats.current_streak
                    existing.is_hot = stats.is_hot
                    existing.is_cold = stats.is_cold
                    existing.last_updated = stats.last_updated
                    stats = existing
                else:
                    session.add(stats)

                results[period_name] = stats

            await session.commit()

        # Update cache
        self._stats_cache[wallet] = results
        return results

    async def get_stats(
        self, wallet: str, period: str = "7d"
    ) -> Optional[TraderStats]:
        """Get cached stats for a wallet, or fetch from DB."""
        # Check cache first
        if wallet in self._stats_cache and period in self._stats_cache[wallet]:
            cached = self._stats_cache[wallet][period]
            # Cache valid for 15 min
            if (utcnow() - cached.last_updated).total_seconds() < 900:
                return cached

        # Fetch from DB
        async with async_session() as session:
            stmt = select(TraderStats).where(
                and_(TraderStats.wallet == wallet, TraderStats.period == period)
            )
            result = (await session.execute(stmt)).scalar_one_or_none()
            if result:
                self._stats_cache.setdefault(wallet, {})[period] = result
            return result

    async def check_auto_pause(self, wallet: str) -> bool:
        """Check if trader should be auto-paused based on cold streak.

        Returns True if trader is cold and should be paused.
        """
        stats = await self.get_stats(wallet, "7d")
        if not stats:
            return False
        return stats.is_cold and not stats.auto_paused

    async def get_hot_multiplier(self, wallet: str) -> float:
        """Return sizing multiplier based on trader performance.

        - Hot streak (>65% WR, >10 trades): returns hot_streak_boost (default 1.5x)
        - Cold streak (<40% WR, >15 trades): returns 0.5x
        - Normal: returns 1.0x
        """
        stats = await self.get_stats(wallet, "7d")
        if not stats or stats.trade_count < 5:
            return 1.0

        if stats.is_hot:
            return 1.5  # Will be overridden by user's hot_streak_boost setting
        elif stats.is_cold:
            return 0.5
        return 1.0

    async def record_trade_outcome(
        self,
        wallet: str,
        market_type: str,
        won: bool,
        return_pct: float,
    ) -> None:
        """Update trader's market history after a trade settles."""
        async with async_session() as session:
            existing = (
                await session.execute(
                    select(TraderMarketHistory).where(
                        and_(
                            TraderMarketHistory.wallet == wallet,
                            TraderMarketHistory.market_type == market_type,
                        )
                    )
                )
            ).scalar_one_or_none()

            if existing:
                existing.trades_count += 1
                if won:
                    existing.wins += 1
                else:
                    existing.losses += 1
                # Running average return
                old_total = existing.avg_return_pct * (existing.trades_count - 1)
                existing.avg_return_pct = round(
                    (old_total + return_pct) / existing.trades_count, 2
                )
                existing.last_updated = utcnow()
            else:
                session.add(
                    TraderMarketHistory(
                        wallet=wallet,
                        market_type=market_type,
                        trades_count=1,
                        wins=1 if won else 0,
                        losses=0 if won else 1,
                        avg_return_pct=round(return_pct, 2),
                        last_updated=utcnow(),
                    )
                )

            await session.commit()

    async def get_trader_market_history(
        self, wallet: str, market_type: str
    ) -> Optional[TraderMarketHistory]:
        """Get trader's history on a specific market type."""
        async with async_session() as session:
            stmt = select(TraderMarketHistory).where(
                and_(
                    TraderMarketHistory.wallet == wallet,
                    TraderMarketHistory.market_type == market_type,
                )
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def get_category_breakdown(self, wallet: str) -> dict:
        """Win rate per market type for this trader."""
        async with async_session() as session:
            stmt = select(TraderMarketHistory).where(
                TraderMarketHistory.wallet == wallet
            )
            rows = (await session.execute(stmt)).scalars().all()
            return {
                row.market_type: {
                    "win_rate": row.win_rate,
                    "trades": row.trades_count,
                    "avg_return": row.avg_return_pct,
                }
                for row in rows
            }

    async def format_trader_report(self, wallet: str) -> str:
        """Format a trader performance report — visuel avec barres et badges."""
        from bot.utils.formatting import (
            short_wallet as sw, badge_trader_status, fmt_usd, fmt_streak,
            bar, fmt_pnl_compact, SEP_LIGHT,
        )

        stats_all = self._stats_cache.get(wallet) or await self.recalculate_stats(
            wallet
        )
        s7 = stats_all.get("7d")
        s30 = stats_all.get("30d")

        addr = sw(wallet)
        badge = badge_trader_status(
            s7.win_rate if s7 else 0,
            s7.trade_count if s7 else 0,
        )
        streak = fmt_streak(s7.current_streak) if s7 else "0"

        lines = [f"👤 `{addr}` {badge} | Streak: {streak}"]

        if s7 and s7.trade_count > 0:
            wr_bar = bar(s7.win_rate, 100, 10)
            lines.append(f"  *7j:* {wr_bar} {s7.win_rate:.0f}% ({s7.trade_count}t)")
            lines.append(f"  PNL: *{fmt_usd(s7.total_pnl)}* | Moy: {fmt_pnl_compact(s7.avg_return_pct)}")
            if s7.best_category:
                lines.append(f"  ✅ Fort: _{s7.best_category[:25]}_")
            if s7.worst_category:
                lines.append(f"  ❌ Faible: _{s7.worst_category[:25]}_")
        else:
            lines.append("  _7j: pas assez de données_")

        if s30 and s30.trade_count > 0:
            wr_bar = bar(s30.win_rate, 100, 10)
            lines.append(f"  *30j:* {wr_bar} {s30.win_rate:.0f}% ({s30.trade_count}t) | {fmt_usd(s30.total_pnl)}")

        return "\n".join(lines)

    # ── Private helpers ───────────────────────────────────────────

    async def _calculate_streak(
        self, session, wallet: str, cutoff: datetime
    ) -> int:
        """Calculate current win/loss streak for a trader."""
        stmt = (
            select(Trade.settlement_pnl)
            .where(
                and_(
                    Trade.master_wallet == wallet,
                    Trade.is_settled == True,  # noqa: E712
                    Trade.created_at >= cutoff,
                    Trade.status == TradeStatus.FILLED,
                )
            )
            .order_by(Trade.created_at.desc())
            .limit(50)
        )
        rows = (await session.execute(stmt)).scalars().all()

        if not rows:
            return 0

        streak = 0
        direction = None
        for pnl in rows:
            if pnl is None:
                continue
            current_dir = "win" if pnl > 0 else "loss"
            if direction is None:
                direction = current_dir
            if current_dir == direction:
                streak += 1 if direction == "win" else -1
            else:
                break

        return streak

    async def _category_breakdown(
        self, session, wallet: str, cutoff: datetime
    ) -> tuple[Optional[str], Optional[str]]:
        """Find best and worst categories by win rate."""
        # This is a simplified version — uses market_question keywords
        # In a full implementation, you'd join with MarketIntel for category
        stmt = (
            select(
                Trade.market_question,
                func.count(Trade.id).label("total"),
                func.sum(
                    case((Trade.settlement_pnl > 0, 1), else_=0)
                ).label("wins"),
            )
            .where(
                and_(
                    Trade.master_wallet == wallet,
                    Trade.is_settled == True,  # noqa: E712
                    Trade.created_at >= cutoff,
                    Trade.market_question.isnot(None),
                )
            )
            .group_by(Trade.market_question)
            .having(func.count(Trade.id) >= 3)
        )
        rows = (await session.execute(stmt)).all()

        if not rows:
            return None, None

        best = max(rows, key=lambda r: (r.wins or 0) / max(r.total, 1))
        worst = min(rows, key=lambda r: (r.wins or 0) / max(r.total, 1))

        return (
            str(best.market_question)[:50] if best else None,
            str(worst.market_question)[:50] if worst else None,
        )
