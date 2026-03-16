"""Polymarket API wrapper — market data, public positions, and order execution."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
RETRY_BACKOFF_S = 1.0

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
DATA_HOST = "https://data-api.polymarket.com"


@dataclass
class MarketInfo:
    market_id: str
    question: str
    slug: str
    tokens: list  # [{token_id, outcome}]
    active: bool
    end_date: Optional[str] = None
    category: Optional[str] = None


@dataclass
class Position:
    market_id: str
    token_id: str
    outcome: str
    size: float
    avg_price: float
    current_price: float
    pnl_pct: float
    title: str = ""
    # Enriched fields from Data API
    cash_pnl: float = 0.0          # Unrealized PNL in USDC
    realized_pnl: float = 0.0     # Realized PNL in USDC
    percent_realized_pnl: float = 0.0
    initial_value: float = 0.0    # Total cost basis
    current_value: float = 0.0    # Current value
    redeemable: bool = False      # True if market resolved
    end_date: str = ""            # Market end date
    slug: str = ""                # Market URL slug


@dataclass
class Activity:
    timestamp: int
    market_id: str
    title: str
    outcome: str
    side: str
    size: float
    usdc_size: float
    price: float
    tx_hash: str
    slug: str


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    filled_size: float = 0.0
    avg_price: float = 0.0
    error: Optional[str] = None


@dataclass
class TraderProfile:
    """Public profile stats for a Polymarket trader."""
    wallet: str
    username: str = ""
    pseudonym: str = ""
    pnl_total: float = 0.0
    pnl_1d: float = 0.0
    pnl_1w: float = 0.0
    pnl_1m: float = 0.0
    volume: float = 0.0
    markets_traded: int = 0
    positions_value: float = 0.0
    biggest_win: float = 0.0


class PolymarketClient:
    """Wrapper for Polymarket public and trading APIs."""

    def __init__(self) -> None:
        # Cache markets by conditionId (market_id)
        self._market_cache: dict[str, MarketInfo] = {}
        # Persistent HTTP client for connection pooling (massive speed gain)
        self._http: Optional["httpx.AsyncClient"] = None
        # Cache CLOB clients per private key hash to avoid re-deriving API creds
        self._clob_cache: dict[str, "ClobClient"] = {}

    async def _get_http(self) -> "httpx.AsyncClient":
        """Return a persistent httpx client with connection pooling."""
        import httpx

        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=10,
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=20,
                    keepalive_expiry=120,
                ),
                http2=True,
            )
        return self._http

    async def close(self) -> None:
        """Close the persistent HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    def create_user_client(self, private_key: str) -> "ClobClient":
        """Return a CLOB client for a user, cached to avoid re-deriving API creds.

        derive_api_creds is SLOW (~200-400ms) — caching saves this on every trade.
        """
        import hashlib

        cache_key = hashlib.sha256(private_key.encode()).hexdigest()[:16]

        cached = self._clob_cache.get(cache_key)
        if cached is not None:
            return cached

        from py_clob_client.client import ClobClient

        client = ClobClient(
            host=CLOB_HOST,
            key=private_key,
            chain_id=137,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        self._clob_cache[cache_key] = client
        return client

    async def ensure_allowances(self, private_key: str) -> bool:
        """Set all ERC-20/ERC-1155 token approvals required by Polymarket.

        Uses py_clob_client's built-in method when available, with manual
        fallback through PolygonClient.
        """
        try:
            client = self.create_user_client(private_key)
            client.set_allowances()
            logger.info("Polymarket allowances set via py_clob_client")
            return True
        except Exception as e:
            logger.warning(
                f"py_clob_client set_allowances failed ({e}), using manual approval"
            )
            from bot.services.web3_client import polygon_client
            from web3 import Web3

            account = Web3().eth.account.from_key(private_key)
            return await polygon_client.ensure_polymarket_approvals(
                account.address, private_key
            )

    async def get_positions_by_address(self, wallet_address: str) -> list[Position]:
        """Fetch positions for any public wallet address via the Data API.

        No private key or API credentials needed — this is public data.
        Retries up to MAX_RETRIES times on network errors.
        Falls back to Gamma API if Data API fails.
        """
        last_err: Optional[Exception] = None

        # Try Data API first (correct endpoint)
        for attempt in range(MAX_RETRIES + 1):
            try:
                http = await self._get_http()
                resp = await http.get(
                    f"{DATA_HOST}/positions",
                    params={
                        "user": wallet_address.lower(),
                        "sizeThreshold": 0,
                        "limit": 500,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                return self._parse_positions(data)

            except Exception as e:
                last_err = e
                if attempt < MAX_RETRIES:
                    logger.warning(
                        f"Retry {attempt + 1}/{MAX_RETRIES} fetching positions "
                        f"(Data API) for {wallet_address[:10]}...: {e}"
                    )
                    await asyncio.sleep(RETRY_BACKOFF_S * (attempt + 1))

        # Fallback: try Gamma API (legacy)
        logger.warning(
            f"Data API failed for {wallet_address[:10]}..., trying Gamma API fallback"
        )
        try:
            http = await self._get_http()
            resp = await http.get(
                f"{GAMMA_HOST}/positions",
                params={"user": wallet_address.lower()},
            )
            resp.raise_for_status()
            data = resp.json()
            return self._parse_positions(data)
        except Exception as e2:
            logger.error(
                f"Failed to fetch positions for {wallet_address[:10]}... "
                f"(Data API: {last_err}, Gamma fallback: {e2})"
            )
            return []

    def _parse_positions(self, data: list) -> list[Position]:
        """Parse position data from either Data API or Gamma API response."""
        positions = []
        for p in data:
            size = float(p.get("size", 0))
            if size <= 0:
                continue

            avg_price = float(p.get("avgPrice", 0))
            cur_price = float(p.get("curPrice", p.get("currentPrice", avg_price)))
            initial_value = float(p.get("initialValue", size * avg_price))
            current_value = float(p.get("currentValue", size * cur_price))
            cash_pnl = float(p.get("cashPnl", current_value - initial_value))
            pnl_pct = float(p.get("percentPnl", 0))
            if pnl_pct == 0 and avg_price > 0:
                pnl_pct = ((cur_price - avg_price) / avg_price) * 100

            positions.append(Position(
                market_id=p.get("conditionId", p.get("marketId", "")),
                token_id=p.get("asset", p.get("tokenId", "")),
                outcome=p.get("outcome", ""),
                size=size,
                avg_price=avg_price,
                current_price=cur_price,
                pnl_pct=pnl_pct,
                title=p.get("title", p.get("question", "")),
                cash_pnl=cash_pnl,
                realized_pnl=float(p.get("realizedPnl", 0)),
                percent_realized_pnl=float(p.get("percentRealizedPnl", 0)),
                initial_value=initial_value,
                current_value=current_value,
                redeemable=bool(p.get("redeemable", False)),
                end_date=str(p.get("endDate", "")),
                slug=str(p.get("slug", "")),
            ))
        return positions

    async def get_activity_by_address(
        self,
        wallet_address: str,
        limit: int = 100,
        start: Optional[int] = None,
        side: Optional[str] = None,
    ) -> list[Activity]:
        """Fetch recent trading activity for a public wallet via Data API.

        Args:
            wallet_address: Public wallet/proxy address.
            limit: Max results (default 100, max 500).
            start: Unix timestamp — only return activity after this time.
            side: Filter by BUY or SELL.
        """
        try:
            params: dict = {
                "user": wallet_address.lower(),
                "limit": min(limit, 500),
                "type": "TRADE",
            }
            if start:
                # API may expect seconds or milliseconds — send seconds
                # (if it expects ms, multiply; tests show seconds work)
                params["start"] = start
            if side:
                params["side"] = side.upper()

            http = await self._get_http()
            resp = await http.get(f"{DATA_HOST}/activity", params=params)
            resp.raise_for_status()
            data = resp.json()

            activities: list[Activity] = []
            for a in data:
                raw_ts = int(a.get("timestamp", 0))
                # Normalize: if timestamp > 10^12, it's milliseconds → convert to seconds
                ts = raw_ts // 1000 if raw_ts > 1e12 else raw_ts
                activities.append(Activity(
                    timestamp=ts,
                    market_id=a.get("conditionId", ""),
                    title=a.get("title", ""),
                    outcome=a.get("outcome", ""),
                    side=a.get("side", ""),
                    size=float(a.get("size", 0)),
                    usdc_size=float(a.get("usdcSize", 0)),
                    price=float(a.get("price", 0)),
                    tx_hash=a.get("transactionHash", ""),
                    slug=a.get("slug", ""),
                ))
            return activities

        except Exception as e:
            logger.error(
                f"Failed to fetch activity for {wallet_address[:10]}...: {e}"
            )
            return []

    async def get_activity_paginated(
        self,
        wallet_address: str,
        start: int,
        max_trades: int = 5000,
    ) -> list[Activity]:
        """Fetch ALL activity for a wallet since `start` timestamp by paginating.

        Uses the `end` param to page backward from newest to oldest.
        Stops when we reach trades older than `start` or hit max_trades.

        Returns: list of Activity sorted newest-first.
        """
        all_activities: list[Activity] = []
        end_cursor: Optional[int] = None  # No upper bound initially
        page_size = 500

        for _ in range(max_trades // page_size + 1):
            params: dict = {
                "user": wallet_address.lower(),
                "limit": page_size,
                "type": "TRADE",
                "start": start,
            }
            if end_cursor:
                params["end"] = end_cursor

            try:
                http = await self._get_http()
                resp = await http.get(f"{DATA_HOST}/activity", params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning(f"Pagination error for {wallet_address[:10]}...: {e}")
                break

            if not data:
                break

            page_activities: list[Activity] = []
            for a in data:
                raw_ts = int(a.get("timestamp", 0))
                ts = raw_ts // 1000 if raw_ts > 1e12 else raw_ts
                page_activities.append(Activity(
                    timestamp=ts,
                    market_id=a.get("conditionId", ""),
                    title=a.get("title", ""),
                    outcome=a.get("outcome", ""),
                    side=a.get("side", ""),
                    size=float(a.get("size", 0)),
                    usdc_size=float(a.get("usdcSize", 0)),
                    price=float(a.get("price", 0)),
                    tx_hash=a.get("transactionHash", ""),
                    slug=a.get("slug", ""),
                ))

            all_activities.extend(page_activities)

            # Stop conditions
            if len(data) < page_size:
                break  # No more pages
            if len(all_activities) >= max_trades:
                break  # Hit our safety limit

            # Move cursor backward: use oldest timestamp from this page
            oldest_ts = min(a.timestamp for a in page_activities)
            if end_cursor and oldest_ts >= end_cursor:
                break  # Stuck, no progress
            end_cursor = oldest_ts

        return all_activities

    async def get_trader_profile(self, wallet_address: str) -> Optional[TraderProfile]:
        """Fetch a trader's public profile stats by scraping their Polymarket page.

        Steps:
        1. Call /api/profile/userData?address=WALLET to get username
        2. Fetch profile page /@username and extract __NEXT_DATA__ JSON
        3. Parse PnL timeseries (1D, 1W, 1M, ALL) and profile stats

        Returns None if the profile can't be fetched.
        """
        import json
        import re

        http = await self._get_http()
        profile = TraderProfile(wallet=wallet_address)

        # Step 1: Wallet → username
        try:
            resp = await http.get(
                "https://polymarket.com/api/profile/userData",
                params={"address": wallet_address},
            )
            resp.raise_for_status()
            user_data = resp.json()
            profile.username = user_data.get("name", "")
            profile.pseudonym = user_data.get("pseudonym", "")
        except Exception as e:
            logger.warning(f"Failed to get userData for {wallet_address[:10]}...: {e}")
            return None

        if not profile.username:
            logger.warning(f"No username found for {wallet_address[:10]}...")
            return None

        # Step 2: Fetch profile page HTML
        try:
            resp = await http.get(
                f"https://polymarket.com/@{profile.username}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.warning(f"Failed to fetch profile page @{profile.username}: {e}")
            return None

        # Step 3: Extract __NEXT_DATA__ JSON
        try:
            match = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                html,
                re.DOTALL,
            )
            if not match:
                logger.warning(f"No __NEXT_DATA__ found on @{profile.username}")
                return None

            next_data = json.loads(match.group(1))
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Failed to parse __NEXT_DATA__ for @{profile.username}: {e}")
            return None

        # Step 4: Parse stats from dehydrated state
        try:
            # Navigate the dehydrated React Query state
            dehydrated = next_data.get("props", {}).get("pageProps", {}).get(
                "dehydratedState", {}
            )
            queries = dehydrated.get("queries", [])

            for q in queries:
                qkey = q.get("queryKey", [])
                data = q.get("state", {}).get("data", None)
                if not data:
                    continue

                key_str = str(qkey)

                # Portfolio PnL timeseries
                if "portfolio-pnl" in key_str and isinstance(data, list) and len(data) > 1:
                    first_p = data[0].get("p", 0) if isinstance(data[0], dict) else 0
                    last_p = data[-1].get("p", 0) if isinstance(data[-1], dict) else 0
                    pnl = last_p - first_p

                    if "1D" in key_str:
                        profile.pnl_1d = pnl
                    elif "1W" in key_str:
                        profile.pnl_1w = pnl
                    elif "1M" in key_str:
                        profile.pnl_1m = pnl
                    elif "ALL" in key_str:
                        profile.pnl_total = last_p  # ALL = total PnL from start

                # Profile stats (volume, markets traded, etc.)
                if isinstance(data, dict):
                    if "volume" in data:
                        profile.volume = float(data.get("volume", 0))
                    if "marketsTraded" in data:
                        profile.markets_traded = int(data.get("marketsTraded", 0))
                    if "positionsValue" in data:
                        profile.positions_value = float(data.get("positionsValue", 0))
                    if "profit" in data:
                        profile.pnl_total = float(data.get("profit", 0))
                    if "biggestWin" in data:
                        profile.biggest_win = float(data.get("biggestWin", 0))

            # Fallback: extract from page props directly
            page_props = next_data.get("props", {}).get("pageProps", {})
            if page_props.get("profit") and profile.pnl_total == 0:
                profile.pnl_total = float(page_props.get("profit", 0))
            if page_props.get("volume") and profile.volume == 0:
                profile.volume = float(page_props.get("volume", 0))

        except Exception as e:
            logger.warning(f"Failed to parse profile stats for @{profile.username}: {e}")

        logger.info(
            f"Profile @{profile.username}: PnL total={profile.pnl_total:+.0f}, "
            f"1W={profile.pnl_1w:+.0f}, 1D={profile.pnl_1d:+.0f}, "
            f"volume={profile.volume:.0f}"
        )
        return profile

    async def get_markets(
        self, limit: int = 50, category: Optional[str] = None
    ) -> list[MarketInfo]:
        """Fetch active markets from Polymarket Gamma API."""
        try:
            params = {"limit": limit, "active": True, "closed": False}
            if category:
                params["tag"] = category

            http = await self._get_http()
            resp = await http.get(f"{GAMMA_HOST}/markets", params=params)
            resp.raise_for_status()
            data = resp.json()

            markets: list[MarketInfo] = []
            for m in data:
                tokens = []
                for t in m.get("clobTokenIds", "").split(","):
                    if t.strip():
                        tokens.append({"token_id": t.strip()})

                mi = MarketInfo(
                    market_id=m.get("conditionId", ""),
                    question=m.get("question", ""),
                    slug=m.get("slug", ""),
                    tokens=tokens,
                    active=m.get("active", False),
                    end_date=m.get("endDate"),
                    category=m.get("groupItemTitle"),
                )
                markets.append(mi)
                if mi.market_id:
                    self._market_cache[mi.market_id] = mi

            return markets

        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            return []

    async def get_market_by_condition_id(
        self, condition_id: str
    ) -> Optional[MarketInfo]:
        """Return MarketInfo for a given conditionId (market_id), with caching.

        Falls back to fetching a batch of markets when not cached.
        """
        if not condition_id:
            return None

        cached = self._market_cache.get(condition_id)
        if cached:
            return cached

        # Fetch a reasonably large batch of active markets and populate cache
        markets = await self.get_markets(limit=500)
        for m in markets:
            if m.market_id == condition_id:
                self._market_cache[condition_id] = m
                return m

        return None

    async def place_order(
        self,
        private_key: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> OrderResult:
        """Place a limit order on Polymarket for a user."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            client = self.create_user_client(private_key)

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )

            signed_order = client.create_order(order_args)
            result = client.post_order(signed_order, order_type=OrderType.GTC)

            if result and result.get("orderID"):
                return OrderResult(
                    success=True,
                    order_id=result["orderID"],
                    filled_size=float(result.get("filledSize", 0)),
                    avg_price=price,
                )
            else:
                return OrderResult(
                    success=False,
                    error=result.get("errorMsg", "Unknown error"),
                )

        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return OrderResult(success=False, error=str(e))

    async def place_market_order(
        self,
        private_key: str,
        token_id: str,
        side: str,
        amount_usdc: float,
    ) -> OrderResult:
        """Place a market (FOK) order — fill immediately at best price.

        C4 FIX: FOK orders are atomic — do NOT retry on order rejection
        (would create duplicate orders). Only retry on network errors
        (timeout, connection error, 5xx).
        """
        import httpx

        last_err: Optional[str] = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                from py_clob_client.clob_types import MarketOrderArgs, OrderType

                client = self.create_user_client(private_key)

                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=amount_usdc,
                    side=side,
                )

                signed_order = client.create_market_order(order_args)
                result = client.post_order(signed_order, order_type=OrderType.FOK)

                if result and result.get("orderID"):
                    return OrderResult(
                        success=True,
                        order_id=result["orderID"],
                        filled_size=float(result.get("filledSize", 0)),
                        avg_price=float(result.get("avgPrice", 0)),
                    )
                else:
                    # C4 FIX: FOK rejected by exchange → do NOT retry
                    error_msg = result.get("errorMsg", "Order not filled") if result else "No response"
                    logger.warning(f"FOK order rejected (no retry): {error_msg}")
                    return OrderResult(success=False, error=error_msg)

            except (httpx.TimeoutException, httpx.ConnectError, ConnectionError, TimeoutError) as e:
                # Network error → safe to retry (order may not have reached exchange)
                last_err = str(e)
                if attempt < MAX_RETRIES:
                    logger.warning(
                        f"Retry {attempt + 1}/{MAX_RETRIES} market order (network): {e}"
                    )
                    await asyncio.sleep(RETRY_BACKOFF_S * (attempt + 1))
                    continue
                logger.error(f"Failed to place market order after retries: {e}")
                return OrderResult(success=False, error=str(e))

            except Exception as e:
                # Unknown error → do NOT retry FOK (could cause duplicates)
                logger.error(f"FOK order error (no retry): {e}")
                return OrderResult(success=False, error=str(e))

        return OrderResult(success=False, error=last_err or "Max retries exceeded")

    async def cancel_order(self, private_key: str, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            client = self.create_user_client(private_key)
            result = client.cancel(order_id)
            return bool(result)
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def get_order_book(self, token_id: str) -> dict:
        """Get the order book for a token."""
        try:
            http = await self._get_http()
            resp = await http.get(
                f"{CLOB_HOST}/book",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to get order book: {e}")
            return {"bids": [], "asks": []}

    async def check_market_resolution(self, condition_id: str) -> Optional[dict]:
        """Check if a market has resolved and determine the winning outcome.

        Returns None if market is still open, or a dict with:
            {
                "resolved": True,
                "winning_token_id": "...",
                "winning_outcome": "Yes" | "No",
                "outcome_prices": {"Yes": 1.0, "No": 0.0},
            }

        Detection logic:
        - Fetch market from Gamma API by conditionId
        - If `closed == true` and one outcomePrices is ~1.0 → resolved
        """
        try:
            http = await self._get_http()
            resp = await http.get(
                f"{GAMMA_HOST}/markets",
                params={"condition_id": condition_id, "limit": 1},
            )
            resp.raise_for_status()
            data = resp.json()

            if not data:
                logger.debug(f"No market found for condition_id={condition_id[:12]}...")
                return None

            market = data[0]
            is_closed = market.get("closed", False)
            if not is_closed:
                return None

            # Parse outcome prices — e.g. "0.99,0.01" or "1,0"
            outcome_prices_str = market.get("outcomePrices", "")
            outcomes_str = market.get("outcomes", "")
            clob_token_ids_str = market.get("clobTokenIds", "")

            if not outcome_prices_str or not outcomes_str:
                return None

            try:
                prices = [float(p.strip()) for p in outcome_prices_str.split(",")]
                outcomes = [o.strip().strip('"') for o in outcomes_str.split(",")]
                token_ids = [t.strip() for t in clob_token_ids_str.split(",")]
            except (ValueError, AttributeError):
                logger.warning(
                    f"Cannot parse outcomes for condition_id={condition_id[:12]}: "
                    f"prices={outcome_prices_str}, outcomes={outcomes_str}"
                )
                return None

            # Find the winner: price >= 0.95 means resolved to that outcome
            winner_idx = None
            for i, price in enumerate(prices):
                if price >= 0.95:
                    winner_idx = i
                    break

            if winner_idx is None:
                # Closed but no clear winner (might be voided or not fully settled)
                return None

            outcome_prices_dict = {}
            for i, outcome in enumerate(outcomes):
                outcome_prices_dict[outcome] = prices[i] if i < len(prices) else 0.0

            result = {
                "resolved": True,
                "winning_outcome": outcomes[winner_idx] if winner_idx < len(outcomes) else "Unknown",
                "winning_token_id": token_ids[winner_idx] if winner_idx < len(token_ids) else "",
                "outcome_prices": outcome_prices_dict,
            }
            logger.info(
                f"Market {condition_id[:12]}... resolved → "
                f"winner={result['winning_outcome']}"
            )
            return result

        except Exception as e:
            logger.error(f"Error checking resolution for {condition_id[:12]}...: {e}")
            return None

    async def get_price(self, token_id: str, side: str = "BUY") -> float:
        """Get the current best price for a token.

        Tries multiple sources in order:
        1. CLOB /midpoint (best bid/ask midpoint)
        2. CLOB /price (best price for BUY side)
        3. CLOB /book (extract best bid from order book)
        """
        http = await self._get_http()

        # 1) Try midpoint
        try:
            resp = await http.get(
                f"{CLOB_HOST}/midpoint",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            mid = float(resp.json().get("mid", 0))
            if mid > 0:
                return mid
        except Exception:
            pass

        # 2) Try /price endpoint
        try:
            resp = await http.get(
                f"{CLOB_HOST}/price",
                params={"token_id": token_id, "side": "buy"},
            )
            resp.raise_for_status()
            price = float(resp.json().get("price", 0))
            if price > 0:
                return price
        except Exception:
            pass

        # 3) Try order book — extract best bid
        try:
            resp = await http.get(
                f"{CLOB_HOST}/book",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            book = resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids:
                best_bid = float(bids[0].get("price", 0))
                if best_bid > 0:
                    return best_bid
            if asks:
                best_ask = float(asks[0].get("price", 0))
                if best_ask > 0:
                    return best_ask
        except Exception:
            pass

        logger.warning(f"No price available for token {token_id[:16]}...")
        return 0.0


# Singleton
polymarket_client = PolymarketClient()
