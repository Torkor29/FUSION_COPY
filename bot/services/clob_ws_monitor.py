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
    """Client WebSocket simple pour le CLOB Polymarket.

    Il tourne en tâche de fond, gère les reconnexions et renvoie les events
    bruts à un callback optionnel (pour les brancher plus tard sur TradeSignal).
    """

    def __init__(
        self,
        on_event: Optional[Callable[[RawWsEvent], Awaitable[None]]] = None,
    ) -> None:
        self._on_event = on_event
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None

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

    async def _listen(self, ws: WebSocketClientProtocol) -> None:
        """Écoute les messages jusqu'à fermeture ou arrêt."""
        # Pour l'instant, on ne souscrit pas à des assets spécifiques.
        # Les futures versions pourront envoyer ici un message de subscription.

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

            event_type = data.get("type") or data.get("event") or "unknown"
            evt = RawWsEvent(type=event_type, payload=data)

            # Log minimal, en mettant l'accent sur last_trade_price.
            if event_type == "last_trade_price":
                asset = data.get("asset_id") or data.get("asset") or "?"
                price = data.get("price")
                size = data.get("size")
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

