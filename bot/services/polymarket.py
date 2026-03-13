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


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    filled_size: float = 0.0
    avg_price: float = 0.0
    error: Optional[str] = None


class PolymarketClient:
    """Wrapper for Polymarket public and trading APIs."""

    def __init__(self) -> None:
        # Cache markets by conditionId (market_id)
        self._market_cache: dict[str, MarketInfo] = {}

    def create_user_client(self, private_key: str) -> "ClobClient":
        """Create a CLOB client for a specific user (follower) to sign orders."""
        from py_clob_client.client import ClobClient

        client = ClobClient(
            host=CLOB_HOST,
            key=private_key,
            chain_id=137,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
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
        """Fetch positions for any public wallet address via the Gamma API.

        No private key or API credentials needed — this is public data.
        Retries up to MAX_RETRIES times on network errors.
        """
        import httpx

        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=15) as http:
                    resp = await http.get(
                        f"{GAMMA_HOST}/positions",
                        params={"user": wallet_address.lower()},
                    )
                    resp.raise_for_status()
                    data = resp.json()

                positions = []
                for p in data:
                    size = float(p.get("size", 0))
                    if size <= 0:
                        continue

                    avg_price = float(p.get("avgPrice", 0))
                    current_price = float(p.get("currentPrice", avg_price))
                    pnl_pct = 0.0
                    if avg_price > 0:
                        pnl_pct = ((current_price - avg_price) / avg_price) * 100

                    positions.append(Position(
                        market_id=p.get("conditionId", p.get("marketId", "")),
                        token_id=p.get("asset", p.get("tokenId", "")),
                        outcome=p.get("outcome", ""),
                        size=size,
                        avg_price=avg_price,
                        current_price=current_price,
                        pnl_pct=pnl_pct,
                    ))

                return positions

            except Exception as e:
                last_err = e
                if attempt < MAX_RETRIES:
                    logger.warning(
                        f"Retry {attempt + 1}/{MAX_RETRIES} fetching positions "
                        f"for {wallet_address[:10]}...: {e}"
                    )
                    await asyncio.sleep(RETRY_BACKOFF_S * (attempt + 1))

        logger.error(f"Failed to fetch positions for {wallet_address[:10]}...: {last_err}")
        return []

    async def get_markets(
        self, limit: int = 50, category: Optional[str] = None
    ) -> list[MarketInfo]:
        """Fetch active markets from Polymarket Gamma API."""
        import httpx

        try:
            params = {"limit": limit, "active": True, "closed": False}
            if category:
                params["tag"] = category

            async with httpx.AsyncClient(timeout=15) as http:
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

        Retries up to MAX_RETRIES times on network/transient errors.
        """
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
                    error_msg = result.get("errorMsg", "Order not filled") if result else "No response"
                    last_err = error_msg
                    if attempt < MAX_RETRIES:
                        logger.warning(
                            f"Retry {attempt + 1}/{MAX_RETRIES} market order: {error_msg}"
                        )
                        await asyncio.sleep(RETRY_BACKOFF_S * (attempt + 1))
                        continue
                    return OrderResult(success=False, error=error_msg)

            except Exception as e:
                last_err = str(e)
                if attempt < MAX_RETRIES:
                    logger.warning(
                        f"Retry {attempt + 1}/{MAX_RETRIES} market order error: {e}"
                    )
                    await asyncio.sleep(RETRY_BACKOFF_S * (attempt + 1))
                    continue
                logger.error(f"Failed to place market order after retries: {e}")
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
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(
                    f"{CLOB_HOST}/book",
                    params={"token_id": token_id},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error(f"Failed to get order book: {e}")
            return {"bids": [], "asks": []}

    async def get_price(self, token_id: str, side: str = "BUY") -> float:
        """Get the current best price for a token via the order book."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(
                    f"{CLOB_HOST}/midpoint",
                    params={"token_id": token_id},
                )
                resp.raise_for_status()
                data = resp.json()
                return float(data.get("mid", 0))
        except Exception as e:
            logger.error(f"Failed to get price for {token_id}: {e}")
            return 0.0


# Singleton
polymarket_client = PolymarketClient()
