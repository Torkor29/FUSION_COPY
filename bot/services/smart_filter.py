"""SmartFilter — intelligent pattern-based trade filtering.

Only copies trades where:
- Market is not a coin-flip (price ~0.50)
- Trader has proven edge on this market type (>55% win rate)
- Trader shows sufficient conviction (trade > 2% of portfolio)
- Price hasn't drifted too much since master's entry
"""

import logging
import re
from typing import Optional

from bot.models.base import utcnow

logger = logging.getLogger(__name__)


class SmartFilter:
    """Combines multiple intelligent filters into a single should_copy() check."""

    def __init__(
        self,
        market_intel_service=None,
        trader_tracker=None,
        polymarket_client=None,
    ):
        self._intel = market_intel_service
        self._tracker = trader_tracker
        self._pm = polymarket_client

    async def should_copy(
        self,
        signal,
        settings,
    ) -> tuple[bool, str]:
        """Master filter combining all smart checks.

        Args:
            signal: TradeSignal from monitor
            settings: UserSettings with smart filter config

        Returns:
            (should_copy: bool, reason: str)
            reason is "OK" if should copy, or explanation if filtered out.
        """
        if not settings.smart_filter_enabled:
            return True, "Smart filter disabled"

        # 1. Coin-flip detection
        result = await self._check_coin_flip(signal, settings)
        if result:
            return False, result

        # 2. Trader market-type win rate
        result = await self._check_trader_edge(signal, settings)
        if result:
            return False, result

        # 3. Conviction check
        result = await self._check_conviction(signal, settings)
        if result:
            return False, result

        # 4. Price drift check
        result = await self._check_price_drift(signal, settings)
        if result:
            return False, result

        return True, "OK"

    # ── Individual checks (return reason string if blocked, None if OK) ──

    async def _check_coin_flip(self, signal, settings) -> Optional[str]:
        """Skip markets where price is ~0.50 (no clear edge)."""
        if not settings.skip_coin_flip:
            return None

        if not self._intel:
            # Fallback: use signal price directly
            if 0.45 <= signal.price <= 0.55:
                return f"Coin-flip market (price ${signal.price:.2f} is near $0.50)"
            return None

        try:
            is_flip = await self._intel.is_coin_flip(signal.market_id)
            if is_flip:
                return "Coin-flip market (price in $0.45-$0.55 zone)"
        except Exception as e:
            logger.debug("Coin-flip check failed: %s", e)

        return None

    async def _check_trader_edge(self, signal, settings) -> Optional[str]:
        """Check if trader has a proven edge on this market type."""
        if not self._tracker:
            return None

        min_wr = settings.min_trader_winrate_for_type
        min_trades = settings.min_trader_trades_for_type

        try:
            market_type = self.categorize_market_type(
                getattr(signal, "market_question", "") or ""
            )

            history = await self._tracker.get_trader_market_history(
                signal.master_wallet, market_type
            )

            if not history:
                # No history = no edge data, allow by default
                return None

            if history.trades_count < min_trades:
                # Not enough data to judge
                return None

            wr = history.win_rate
            if wr < min_wr:
                return (
                    f"Trader has {wr:.0f}% win rate on '{market_type}' "
                    f"({history.trades_count} trades, min {min_wr:.0f}% required)"
                )

        except Exception as e:
            logger.debug("Trader edge check failed: %s", e)

        return None

    async def _check_conviction(self, signal, settings) -> Optional[str]:
        """Check if trader's trade shows sufficient conviction (% of portfolio)."""
        min_conviction = settings.min_conviction_pct / 100

        if min_conviction <= 0 or not self._pm:
            return None

        try:
            trade_value = signal.size * signal.price

            # Get master's portfolio value
            positions = await self._pm.get_positions_by_address(signal.master_wallet)
            if not positions:
                return None  # Can't check, allow

            portfolio_value = sum(
                abs(float(p.get("currentValue", 0) or 0)) for p in positions
            )
            if portfolio_value <= 0:
                return None

            conviction = trade_value / portfolio_value

            if conviction < min_conviction:
                return (
                    f"Low conviction: trade is {conviction*100:.1f}% of trader's portfolio "
                    f"(min {settings.min_conviction_pct:.0f}% required)"
                )

        except Exception as e:
            logger.debug("Conviction check failed: %s", e)

        return None

    async def _check_price_drift(self, signal, settings) -> Optional[str]:
        """Check if price has moved too much since master's entry."""
        max_drift = settings.max_price_drift_pct

        if max_drift <= 0 or not self._pm:
            return None

        try:
            current_price = await self._pm.get_price(signal.token_id, signal.side)

            if current_price <= 0 or signal.price <= 0:
                return None

            drift_pct = abs(current_price - signal.price) / signal.price * 100

            if drift_pct > max_drift:
                direction = "up" if current_price > signal.price else "down"
                return (
                    f"Price drifted {drift_pct:.1f}% {direction} since master's entry "
                    f"(${signal.price:.4f} → ${current_price:.4f}, max {max_drift:.0f}%)"
                )

        except Exception as e:
            logger.debug("Price drift check failed: %s", e)

        return None

    # ── Market type categorization ────────────────────────────────

    @staticmethod
    def categorize_market_type(question: str) -> str:
        """Categorize a market question into a type string.

        Examples:
            "Will BTC be above $90K at 5pm?" → "crypto_btc_5min"
            "Will Trump win the 2024 election?" → "politics_us"
            "Will the Lakers win tonight?" → "sports_nba"
        """
        q = question.lower().strip()

        # Crypto short-term (5-min, hourly up/down)
        if re.search(r'\b(btc|bitcoin)\b.*\b(above|below|up|down|over|under)\b.*\d', q):
            if re.search(r'\b(\d+\s*(am|pm)|minute|5.?min)\b', q):
                return "crypto_btc_5min"
            if re.search(r'\b(hour|1h|4h)\b', q):
                return "crypto_btc_hourly"
            return "crypto_btc_daily"

        if re.search(r'\b(eth|ethereum)\b.*\b(above|below|up|down)\b', q):
            return "crypto_eth"

        if re.search(r'\b(sol|solana)\b.*\b(above|below|up|down)\b', q):
            return "crypto_sol"

        if any(k in q for k in ("crypto", "token", "coin", "defi", "nft")):
            return "crypto_other"

        # Politics
        if any(k in q for k in ("trump", "biden", "election", "congress", "president", "senate")):
            return "politics_us"
        if any(k in q for k in ("macron", "starmer", "eu", "european")):
            return "politics_intl"

        # Sports
        if any(k in q for k in ("nfl", "football", "super bowl", "quarterback")):
            return "sports_nfl"
        if any(k in q for k in ("nba", "basketball", "lakers", "celtics")):
            return "sports_nba"
        if any(k in q for k in ("soccer", "premier league", "champions league", "fifa")):
            return "sports_soccer"
        if any(k in q for k in ("mlb", "baseball")):
            return "sports_mlb"

        # Economy
        if any(k in q for k in ("fed", "rate", "gdp", "inflation", "unemployment", "cpi")):
            return "economy_macro"
        if any(k in q for k in ("stock", "sp500", "nasdaq", "dow", "earnings")):
            return "economy_stocks"

        # Entertainment / Pop culture
        if any(k in q for k in ("oscar", "grammy", "movie", "album", "concert")):
            return "entertainment"

        # Tech
        if any(k in q for k in ("ai", "openai", "google", "apple", "tesla", "spacex")):
            return "tech"

        # Weather
        if any(k in q for k in ("weather", "temperature", "hurricane", "snow")):
            return "weather"

        return "other"
