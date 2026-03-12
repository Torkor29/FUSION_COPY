"""Multi-master position monitor — watches all followed wallets for trade signals."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from bot.services.polymarket import polymarket_client, Position

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    """A trade detected from a followed master trader."""
    master_wallet: str
    market_id: str
    token_id: str
    outcome: str
    side: str  # "BUY" or "SELL"
    size: float  # shares
    price: float
    master_pnl_pct: float = 0.0
    market_question: str = ""


@dataclass
class WalletState:
    """Tracks a single wallet's known positions."""
    positions: dict[str, Position] = field(default_factory=dict)
    initialized: bool = False


class MultiMasterMonitor:
    """Polls positions of all followed wallets and emits trade signals.

    The set of watched wallets is refreshed periodically via
    refresh_watched_wallets(), which queries the DB for all unique
    followed_wallets across all active users.
    """

    def __init__(
        self,
        poll_interval: int = 15,
        on_signal: Optional[Callable[[TradeSignal], Awaitable[None]]] = None,
    ):
        self._poll_interval = poll_interval
        self._on_signal = on_signal
        self._wallet_states: dict[str, WalletState] = {}
        self._is_running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start monitoring in background."""
        if self._is_running:
            logger.warning("Monitor already running")
            return

        self._is_running = True
        await self.refresh_watched_wallets()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"Multi-master monitor started "
            f"(interval: {self._poll_interval}s, "
            f"wallets: {len(self._wallet_states)})"
        )

    async def stop(self) -> None:
        """Stop monitoring."""
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Multi-master monitor stopped")

    async def refresh_watched_wallets(self) -> None:
        """Refresh the set of wallets to watch from the database.

        Adds new wallets, removes wallets nobody follows anymore.
        """
        from bot.db.session import async_session
        from bot.services.user_service import get_all_followed_wallets

        try:
            async with async_session() as session:
                active_wallets = await get_all_followed_wallets(session)
        except Exception as e:
            logger.error(f"Failed to refresh watched wallets: {e}")
            return

        active_set = {w.lower() for w in active_wallets}
        current_set = set(self._wallet_states.keys())

        added = active_set - current_set
        removed = current_set - active_set

        for wallet in added:
            self._wallet_states[wallet] = WalletState()

        for wallet in removed:
            del self._wallet_states[wallet]

        if added or removed:
            logger.info(
                f"Watched wallets updated: +{len(added)} -{len(removed)} "
                f"= {len(self._wallet_states)} total"
            )

    async def _poll_loop(self) -> None:
        """Main polling loop."""
        # Initial snapshot for all wallets
        await self._snapshot_all(initial=True)

        while self._is_running:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._check_all_wallets()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Monitor poll error: {e}")
                await asyncio.sleep(5)

    async def _snapshot_all(self, initial: bool = False) -> None:
        """Fetch positions for all watched wallets."""
        tasks = [
            self._snapshot_wallet(wallet, initial)
            for wallet in list(self._wallet_states.keys())
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _snapshot_wallet(self, wallet: str, initial: bool = False) -> None:
        """Fetch positions for a single wallet."""
        state = self._wallet_states.get(wallet)
        if state is None:
            return

        positions = await polymarket_client.get_positions_by_address(wallet)

        if initial or not state.initialized:
            state.positions = {p.token_id: p for p in positions}
            state.initialized = True
            logger.info(
                f"Snapshot {wallet[:10]}...: {len(positions)} positions"
            )

    async def _check_all_wallets(self) -> None:
        """Check all wallets for position changes."""
        tasks = [
            self._check_wallet(wallet)
            for wallet in list(self._wallet_states.keys())
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_wallet(self, wallet: str) -> None:
        """Compare current positions with known state for one wallet."""
        state = self._wallet_states.get(wallet)
        if state is None or not state.initialized:
            return

        current_positions = await polymarket_client.get_positions_by_address(wallet)
        current_map = {p.token_id: p for p in current_positions}
        known = state.positions

        # New or increased positions → BUY signals
        for token_id, pos in current_map.items():
            if token_id not in known:
                signal = TradeSignal(
                    master_wallet=wallet,
                    market_id=pos.market_id,
                    token_id=pos.token_id,
                    outcome=pos.outcome,
                    side="BUY",
                    size=pos.size,
                    price=pos.current_price,
                    master_pnl_pct=pos.pnl_pct,
                )
                await self._emit_signal(signal)

            elif pos.size > known[token_id].size:
                added = pos.size - known[token_id].size
                signal = TradeSignal(
                    master_wallet=wallet,
                    market_id=pos.market_id,
                    token_id=pos.token_id,
                    outcome=pos.outcome,
                    side="BUY",
                    size=added,
                    price=pos.current_price,
                    master_pnl_pct=pos.pnl_pct,
                )
                await self._emit_signal(signal)

            elif pos.size < known[token_id].size:
                reduced = known[token_id].size - pos.size
                signal = TradeSignal(
                    master_wallet=wallet,
                    market_id=pos.market_id,
                    token_id=pos.token_id,
                    outcome=pos.outcome,
                    side="SELL",
                    size=reduced,
                    price=pos.current_price,
                    master_pnl_pct=pos.pnl_pct,
                )
                await self._emit_signal(signal)

        # Closed positions → SELL signals
        for token_id, pos in known.items():
            if token_id not in current_map:
                signal = TradeSignal(
                    master_wallet=wallet,
                    market_id=pos.market_id,
                    token_id=pos.token_id,
                    outcome=pos.outcome,
                    side="SELL",
                    size=pos.size,
                    price=pos.current_price,
                    master_pnl_pct=pos.pnl_pct,
                )
                await self._emit_signal(signal)

        state.positions = current_map

    async def _emit_signal(self, signal: TradeSignal) -> None:
        """Emit a trade signal to the copytrade engine."""
        logger.info(
            f"Signal [{signal.master_wallet[:10]}...]: "
            f"{signal.side} {signal.size:.2f} shares "
            f"of {signal.token_id[:12]}... @ {signal.price:.4f}"
        )
        if self._on_signal:
            try:
                await self._on_signal(signal)
            except Exception as e:
                logger.error(f"Signal handler error: {e}")

    @property
    def watched_wallets(self) -> list[str]:
        return list(self._wallet_states.keys())

    @property
    def is_running(self) -> bool:
        return self._is_running

    async def fast_check_all_wallets(self) -> None:
        """Expose a public trigger to forcer un check immédiat.

        Utilisé par le monitor WebSocket (CLOB) pour accélérer la détection
        en complément du polling Gamma.
        """
        if not self._is_running:
            return
        await self._check_all_wallets()
