"""Polymarket CLOB WebSocket monitor — foundation for real-time signals.

Cette première version se connecte au WebSocket CLOB, se maintient en vie
et loggue les messages reçus (en particulier les événements last_trade_price).
L'intégration fine avec le moteur de copytrade (TradeSignal) sera branchée
dans une étape suivante.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import websockets
from websockets import WebSocketClientProtocol

logger = logging.getLogger(__name__)


CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class RawWsEvent:
    """Représente un événement brut reçu du WebSocket CLOB."""

    type: str
    payload: dict


class ClobWsMonitor:
    """Client WebSocket pour le CLOB Polymarket.

    Subscribes to specific token_ids from followed traders' positions
    for instant trade detection (sub-second).
    """

    def __init__(
        self,
        on_event: Optional[Callable[[RawWsEvent], Awaitable[None]]] = None,
    ) -> None:
        self._on_event = on_event
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._ws: Optional[WebSocketClientProtocol] = None
        self._subscribed_assets: set[str] = set()

    async def start(self) -> None:
        if self._running:
            logger.warning("ClobWsMonitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("ClobWsMonitor started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ClobWsMonitor stopped")

    async def _run_loop(self) -> None:
        """Boucle principale de connexion + reconnexion."""
        backoff = 5
        while self._running:
            try:
                logger.info("Connecting to Polymarket CLOB WebSocket...")
                async with websockets.connect(CLOB_WS_URL, ping_interval=10, ping_timeout=20) as ws:
                    logger.info("Connected to CLOB WebSocket")
                    backoff = 5  # reset backoff on successful connect
                    await self._listen(ws)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"CLOB WebSocket error: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def update_subscriptions(self, asset_ids: set[str]) -> None:
        """Subscribe to new asset_ids, unsubscribe from old ones.

        Called periodically by the scheduler to track tokens held by
        followed traders.
        """
        if not self._ws:
            self._subscribed_assets = asset_ids
            return

        new_ids = asset_ids - self._subscribed_assets
        old_ids = self._subscribed_assets - asset_ids

        try:
            if new_ids:
                msg = json.dumps({
                    "assets_ids": list(new_ids),
                    "operation": "subscribe",
                    "custom_feature_enabled": True,
                })
                await self._ws.send(msg)
                logger.info(f"WS subscribed to {len(new_ids)} new token(s)")

            if old_ids:
                msg = json.dumps({
                    "assets_ids": list(old_ids),
                    "operation": "unsubscribe",
                })
                await self._ws.send(msg)

            self._subscribed_assets = asset_ids
        except Exception as e:
            logger.warning(f"WS subscription update failed: {e}")

    async def _listen(self, ws: WebSocketClientProtocol) -> None:
        """Écoute les messages jusqu'à fermeture ou arrêt."""
        self._ws = ws

        # Subscribe to all tracked assets on connect
        if self._subscribed_assets:
            try:
                msg = json.dumps({
                    "assets_ids": list(self._subscribed_assets),
                    "type": "market",
                    "custom_feature_enabled": True,
                })
                await ws.send(msg)
                logger.info(
                    f"WS initial subscription: {len(self._subscribed_assets)} token(s)"
                )
            except Exception as e:
                logger.warning(f"WS initial subscription failed: {e}")

        while self._running:
            try:
                raw = await ws.recv()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"CLOB WebSocket recv error: {e}")
                break

            try:
                data = json.loads(raw)
            except Exception:
                logger.debug(f"Non-JSON message from CLOB WS: {raw!r}")
                continue

            # Handle list messages (CLOB WS sometimes sends arrays)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    logger.debug(f"Non-dict WS item: {type(item)}")
                    continue

                event_type = item.get("type") or item.get("event") or "unknown"
                evt = RawWsEvent(type=event_type, payload=item)

                if event_type == "last_trade_price":
                    asset = item.get("asset_id") or item.get("asset") or "?"
                    price = item.get("price")
                    size = item.get("size")
                    logger.info(
                        f"CLOB trade event: asset={asset} price={price} size={size}"
                    )
                else:
                    logger.debug(f"CLOB WS event: {event_type}")

                if self._on_event:
                    try:
                        await self._on_event(evt)
                    except Exception as e:
                        logger.error(f"Error in CLOB WS on_event callback: {e}")

