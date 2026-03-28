"""PortfolioManager — holistic portfolio risk management.

Controls: max positions, category exposure limits, direction bias,
correlation guards. Provides daily portfolio reports.
"""

import logging
from typing import Optional
from collections import defaultdict

from sqlalchemy import select, and_

from bot.db.session import async_session
from bot.models.active_position import ActivePosition
from bot.models.base import utcnow

logger = logging.getLogger(__name__)


class PortfolioManager:
    """Manages portfolio-level risk controls and reporting."""

    def __init__(
        self,
        position_manager=None,
        market_intel_service=None,
        market_categorizer=None,
    ):
        self._pos_mgr = position_manager
        self._intel = market_intel_service
        self._categorizer = market_categorizer  # market_categories.categorize()

    async def check_can_open(
        self,
        user_id: int,
        market_id: str,
        category: str,
        side: str,
        max_positions: int = 15,
        max_category_exposure_pct: float = 30.0,
        max_direction_bias_pct: float = 70.0,
    ) -> tuple[bool, str]:
        """Pre-trade check: can this user open a new position?

        Returns (allowed: bool, reason: str).
        Reason is "OK" if allowed, or a description of why blocked.
        """
        positions = await self._get_open_positions(user_id)

        # 1. Max positions check
        if len(positions) >= max_positions:
            return False, f"Max positions reached ({max_positions})"

        # 2. Duplicate market check
        existing_markets = {p.market_id for p in positions}
        if market_id in existing_markets:
            return False, "Already have a position on this market"

        # 3. Category exposure check
        if category and positions:
            cat_exposure = self._calculate_category_exposure(positions, category)
            if cat_exposure >= max_category_exposure_pct:
                return (
                    False,
                    f"Category '{category}' exposure at {cat_exposure:.0f}% "
                    f"(max {max_category_exposure_pct:.0f}%)",
                )

        # 4. Direction bias check (YES vs NO)
        if positions:
            bias = self._calculate_direction_bias(positions, side)
            if bias >= max_direction_bias_pct:
                return (
                    False,
                    f"Direction bias too high: {bias:.0f}% positions are {side} "
                    f"(max {max_direction_bias_pct:.0f}%)",
                )

        # 5. Correlation check — max 3 positions in same subcategory
        if category and positions:
            same_cat_count = sum(
                1
                for p in positions
                if self._get_position_category(p) == category
            )
            if same_cat_count >= 3:
                return (
                    False,
                    f"Already have {same_cat_count} positions in '{category}' "
                    f"(max 3 correlated)",
                )

        return True, "OK"

    async def get_portfolio_summary(self, user_id: int) -> dict:
        """Generate a complete portfolio summary.

        Returns:
            {
                "total_positions": int,
                "total_value_usdc": float,
                "unrealized_pnl_pct": float,
                "category_exposure": {"Crypto": 40, "Politics": 30, ...},
                "direction_split": {"YES": 60, "NO": 40},
                "positions": [...]
            }
        """
        positions = await self._get_open_positions(user_id)

        if not positions:
            return {
                "total_positions": 0,
                "total_value_usdc": 0.0,
                "unrealized_pnl_pct": 0.0,
                "category_exposure": {},
                "direction_split": {},
                "positions": [],
            }

        # Calculate total value
        total_value = sum(p.current_price * p.shares for p in positions)
        total_cost = sum(p.entry_price * p.shares for p in positions)
        unrealized_pnl_pct = (
            ((total_value - total_cost) / total_cost * 100) if total_cost > 0 else 0.0
        )

        # Category exposure
        cat_counts = defaultdict(int)
        for p in positions:
            cat = self._get_position_category(p) or "Unknown"
            cat_counts[cat] += 1

        total = len(positions)
        category_exposure = {
            cat: round(count / total * 100, 1) for cat, count in cat_counts.items()
        }

        # Direction split
        yes_count = sum(1 for p in positions if p.outcome.upper() in ("YES", "Y"))
        no_count = total - yes_count
        direction_split = {
            "YES": round(yes_count / total * 100, 1) if total > 0 else 0,
            "NO": round(no_count / total * 100, 1) if total > 0 else 0,
        }

        return {
            "total_positions": total,
            "total_value_usdc": round(total_value, 2),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
            "category_exposure": dict(
                sorted(category_exposure.items(), key=lambda x: -x[1])
            ),
            "direction_split": direction_split,
            "positions": positions,
        }

    async def format_portfolio_report(self, user_id: int) -> str:
        """Format portfolio summary — visuel avec barres et badges."""
        from bot.utils.formatting import (
            header, bar, fmt_usd, fmt_pnl, badge_position_status,
            bar_bicolor, SEP_LIGHT,
        )

        summary = await self.get_portfolio_summary(user_id)

        if summary["total_positions"] == 0:
            return (
                f"{header('PORTFOLIO', '💼')}\n\n"
                "Aucune position ouverte.\n\n"
                "_Les positions apparaitront ici dès qu'un trade sera copié._"
            )

        total_pos = summary["total_positions"]
        total_val = summary["total_value_usdc"]
        pnl = summary["unrealized_pnl_pct"]
        pnl_line = fmt_pnl(pnl_pct=pnl)

        lines = [
            f"{header('PORTFOLIO', '💼')}\n",
            f"📦 *{total_pos}* positions | {fmt_usd(total_val)} | {pnl_line}\n",
        ]

        # Category exposure with bars
        cat_exp = summary.get("category_exposure", {})
        if cat_exp:
            lines.append(f"*Exposition par catégorie:*")
            for cat, pct in cat_exp.items():
                cat_bar = bar(pct, 100, 15)
                lines.append(f"  {cat_bar} {cat} *{pct:.0f}%*")
            lines.append("")

        # Direction split with bicolor bar
        ds = summary.get("direction_split", {})
        yes_pct = ds.get("YES", 50)
        no_pct = ds.get("NO", 50)
        dir_bar = bar_bicolor(yes_pct, no_pct, 100, 10)
        lines.append(f"*Direction:* {dir_bar} YES {yes_pct:.0f}% / NO {no_pct:.0f}%\n")

        # Positions sorted by PNL
        positions = sorted(
            summary["positions"],
            key=lambda p: p.unrealized_pnl_pct,
            reverse=True,
        )

        if positions:
            lines.append(f"*Positions (par PNL):*")
            for p in positions[:5]:
                name = (p.market_question or p.market_id)[:28]
                pnl_val = p.unrealized_pnl_pct
                badge = badge_position_status(pnl_val)
                sign = "+" if pnl_val >= 0 else ""
                lines.append(f"  {badge} _{name}_ *{sign}{pnl_val:.1f}%*")

            if len(positions) > 5:
                remaining = len(positions) - 5
                lines.append(f"  _... et {remaining} autre(s)_")

        return "\n".join(lines)

    # ── Private helpers ───────────────────────────────────────────

    async def _get_open_positions(self, user_id: int) -> list[ActivePosition]:
        """Get open positions from DB."""
        async with async_session() as session:
            stmt = select(ActivePosition).where(
                and_(
                    ActivePosition.user_id == user_id,
                    ActivePosition.is_closed == False,  # noqa: E712
                )
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    def _calculate_category_exposure(
        self, positions: list[ActivePosition], target_category: str
    ) -> float:
        """Calculate % of positions in the target category."""
        if not positions:
            return 0.0
        same_cat = sum(
            1
            for p in positions
            if self._get_position_category(p) == target_category
        )
        return (same_cat / len(positions)) * 100

    def _calculate_direction_bias(
        self, positions: list[ActivePosition], new_side: str
    ) -> float:
        """Calculate what % of positions would be in the same direction after adding new one."""
        if not positions:
            return 0.0

        # Map outcomes to YES/NO
        yes_sides = {"YES", "Y", "BUY"}
        total = len(positions) + 1  # Including the new position

        if new_side.upper() in yes_sides:
            same_direction = (
                sum(1 for p in positions if p.outcome.upper() in yes_sides) + 1
            )
        else:
            same_direction = (
                sum(1 for p in positions if p.outcome.upper() not in yes_sides) + 1
            )

        return (same_direction / total) * 100

    def _get_position_category(self, pos: ActivePosition) -> str:
        """Get category for a position. Uses market_question keyword matching."""
        if not pos.market_question:
            return "Unknown"

        q = pos.market_question.lower()

        # Quick keyword-based categorization
        if any(k in q for k in ("btc", "bitcoin", "eth", "crypto", "sol", "token")):
            return "Crypto"
        elif any(k in q for k in ("trump", "biden", "election", "congress", "president")):
            return "Politics"
        elif any(k in q for k in ("nfl", "nba", "soccer", "football", "game", "match")):
            return "Sports"
        elif any(k in q for k in ("fed", "gdp", "inflation", "rate", "economy")):
            return "Economy"

        # If we have a categorizer service, use it
        if self._categorizer:
            try:
                result = self._categorizer.categorize(pos.market_question)
                return result.get("category", "Other")
            except Exception:
                pass

        return "Other"
