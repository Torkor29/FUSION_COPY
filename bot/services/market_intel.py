"""MarketIntelService — fetches and caches market intelligence data.

Provides: volume, liquidity, spread, momentum, coin-flip detection,
time-value analysis for smart filtering and signal scoring.
"""

import logging
import time
from datetime import datetime
from typing import Optional

from bot.db.session import async_session
from bot.models.market_intel import MarketIntel as MarketIntelModel
from bot.models.base import utcnow

logger = logging.getLogger(__name__)

# Cache TTL in seconds
CACHE_TTL = 300  # 5 minutes


class MarketIntelService:
    """Fetches, caches and serves market intelligence data."""

    def __init__(self, polymarket_client=None):
        self._pm = polymarket_client
        # In-memory cache: market_id → (MarketIntelModel, timestamp)
        self._cache: dict[str, tuple[MarketIntelModel, float]] = {}

    async def get_intel(self, market_id: str) -> Optional[MarketIntelModel]:
        """Get market intelligence, from cache or fresh fetch.

        Returns cached data if less than 5 minutes old.
        """
        # Check memory cache
        if market_id in self._cache:
            cached, ts = self._cache[market_id]
            if time.time() - ts < CACHE_TTL:
                return cached

        # Fetch fresh data
        intel = await self.fetch_market_data(market_id)
        if intel:
            self._cache[market_id] = (intel, time.time())
        return intel

    async def fetch_market_data(self, market_id: str) -> Optional[MarketIntelModel]:
        """Fetch market data from Polymarket Gamma API and compute metrics."""
        if not self._pm:
            return None

        try:
            http = await self._pm._get_http()

            # Fetch market info from Gamma API
            resp = await http.get(
                "https://gamma-api.polymarket.com/markets",
                params={"condition_id": market_id, "limit": 1},
            )
            resp.raise_for_status()
            data = resp.json()

            if not data:
                logger.debug("No market data for %s", market_id[:16])
                return None

            market = data[0]

            # Parse basic fields
            question = market.get("question", "")
            category = market.get("groupItemTitle", "") or "Other"
            volume_24h = float(market.get("volume24hr", 0) or 0)
            volume_total = float(market.get("volume", 0) or 0)

            # Parse outcome prices for current price
            outcome_prices_str = market.get("outcomePrices", "")
            price_current = 0.0
            if outcome_prices_str:
                try:
                    prices = [float(p.strip()) for p in outcome_prices_str.split(",")]
                    price_current = prices[0] if prices else 0.0
                except (ValueError, AttributeError):
                    pass

            # Parse expiry
            end_date_str = market.get("endDate") or market.get("end_date_iso")
            expiry = None
            if end_date_str:
                try:
                    expiry = datetime.fromisoformat(
                        end_date_str.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except (ValueError, TypeError):
                    pass

            # Fetch orderbook for spread
            spread_avg = 0.0
            token_ids_str = market.get("clobTokenIds", "")
            token_id = ""
            if token_ids_str:
                try:
                    token_id = token_ids_str.split(",")[0].strip()
                except (IndexError, AttributeError):
                    pass

            if token_id:
                try:
                    book = await self._pm.get_order_book(token_id)
                    bids = book.get("bids", [])
                    asks = book.get("asks", [])
                    if bids and asks:
                        best_bid = float(bids[0].get("price", 0))
                        best_ask = float(asks[0].get("price", 1))
                        if best_ask > 0:
                            spread_avg = ((best_ask - best_bid) / best_ask) * 100
                except Exception:
                    pass

            # Compute metrics
            is_coin_flip = 0.45 <= price_current <= 0.55
            liquidity_score = self._compute_liquidity_score(volume_24h, spread_avg)

            # Momentum (would need historical prices — simplified here)
            momentum_1h = None  # TODO: track price history for real momentum

            now = utcnow()

            intel = MarketIntelModel(
                market_id=market_id,
                question=question[:512],
                category=category[:64],
                expiry=expiry,
                volume_24h=volume_24h,
                open_interest=volume_total,
                holders_count=0,  # Not available from Gamma API
                spread_avg=round(spread_avg, 2),
                price_current=price_current,
                momentum_1h=momentum_1h,
                liquidity_score=round(liquidity_score, 1),
                is_coin_flip=is_coin_flip,
                last_updated=now,
            )

            # Persist to DB (upsert)
            try:
                async with async_session() as session:
                    from sqlalchemy import select

                    existing = (
                        await session.execute(
                            select(MarketIntelModel).where(
                                MarketIntelModel.market_id == market_id
                            )
                        )
                    ).scalar_one_or_none()

                    if existing:
                        existing.question = intel.question
                        existing.category = intel.category
                        existing.expiry = intel.expiry
                        existing.volume_24h = intel.volume_24h
                        existing.open_interest = intel.open_interest
                        existing.spread_avg = intel.spread_avg
                        existing.price_current = intel.price_current
                        existing.momentum_1h = intel.momentum_1h
                        existing.liquidity_score = intel.liquidity_score
                        existing.is_coin_flip = intel.is_coin_flip
                        existing.last_updated = intel.last_updated
                        intel = existing
                    else:
                        session.add(intel)

                    await session.commit()
            except Exception as e:
                logger.debug("Failed to persist market intel: %s", e)

            return intel

        except Exception as e:
            logger.error("Failed to fetch market intel for %s: %s", market_id[:16], e)
            return None

    async def is_coin_flip(self, market_id: str) -> bool:
        """Check if market price is in the coin-flip zone (0.45-0.55)."""
        intel = await self.get_intel(market_id)
        if not intel:
            return False
        return intel.is_coin_flip

    async def get_momentum(self, market_id: str) -> Optional[float]:
        """Get 1h price momentum as % change."""
        intel = await self.get_intel(market_id)
        if not intel:
            return None
        return intel.momentum_1h

    async def get_time_value_score(
        self, market_id: str, price: float
    ) -> Optional[float]:
        """Assess upside potential vs time remaining.

        Returns 0-100 score. High = good time value.
        Example: price=0.85 with 2 days left = low upside (15% for 48h)
        """
        intel = await self.get_intel(market_id)
        if not intel or not intel.expiry:
            return None

        now = utcnow()
        hours_remaining = (intel.expiry - now).total_seconds() / 3600

        if hours_remaining <= 0:
            return 0.0

        # Potential upside: (1.0 - price) for YES positions
        upside = (1.0 - price) if price < 1.0 else 0.0

        # Downside: price for YES positions
        downside = price

        # Risk-reward ratio
        if downside <= 0:
            return 100.0

        rr = upside / downside

        # Time factor: more time = better (but diminishing returns)
        if hours_remaining < 1:
            time_factor = 0.3
        elif hours_remaining < 24:
            time_factor = 0.7
        elif hours_remaining < 168:
            time_factor = 1.0
        else:
            time_factor = 0.8  # Very long = capital locked

        score = min(100, rr * time_factor * 50)
        return round(score, 1)

    def invalidate_cache(self, market_id: str) -> None:
        """Remove a market from cache to force re-fetch."""
        self._cache.pop(market_id, None)

    # ── Private helpers ───────────────────────────────────────────

    @staticmethod
    def _compute_liquidity_score(volume_24h: float, spread_pct: float) -> float:
        """Compute a 0-100 liquidity score from volume and spread.

        Higher volume + tighter spread = higher score.
        """
        # Volume component (0-60)
        if volume_24h >= 500_000:
            vol_score = 60
        elif volume_24h >= 100_000:
            vol_score = 50
        elif volume_24h >= 50_000:
            vol_score = 40
        elif volume_24h >= 10_000:
            vol_score = 25
        elif volume_24h >= 1_000:
            vol_score = 10
        else:
            vol_score = 0

        # Spread component (0-40)
        if spread_pct < 1:
            spread_score = 40
        elif spread_pct < 2:
            spread_score = 30
        elif spread_pct < 3:
            spread_score = 20
        elif spread_pct < 5:
            spread_score = 10
        else:
            spread_score = 0

        return min(100, vol_score + spread_score)
