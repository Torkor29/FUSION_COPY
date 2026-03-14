"""Copytrade engine — orchestrates the full trade copy flow.

Flow per signal:
1. Receive TradeSignal from MultiMasterMonitor (includes master_wallet)
2. Find all active followers who follow that specific master_wallet
3. For each matching follower:
   a. Check filters (categories, blacklist, expiry)
   b. Calculate trade size (sizing engine)
   c. Calculate and deduct platform fee (1%)
   d. Transfer fee on-chain to FEES_WALLET
   e. Execute trade on Polymarket via CLOB API
   f. Record trade + fee in database
   g. Send Telegram notification
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.session import async_session
from bot.models.user import User
from bot.models.trade import Trade, TradeStatus, TradeSide
from bot.models.fee import FeeRecord
from bot.services.fees import calculate_fee, FeeResult, FeeCalculationError
from bot.services.sizing import calculate_trade_size, SizingError
from bot.services.crypto import decrypt_private_key
from bot.services.polymarket import polymarket_client
from bot.services.web3_client import polygon_client
from bot.services.monitor import TradeSignal
from bot.services.user_service import get_followers_of_wallet, get_or_create_settings

logger = logging.getLogger(__name__)


class CopyTradeEngine:
    """Main copytrade orchestrator — optimized for sub-second execution."""

    def __init__(self, telegram_bot=None):
        self._bot = telegram_bot
        self._master_portfolio_usdc: float = 10000.0
        # Cache master portfolio values (refreshed each signal)
        self._portfolio_cache: dict[str, tuple[float, float]] = {}  # wallet -> (value, timestamp)
        _PORTFOLIO_CACHE_TTL = 30  # seconds

    async def handle_signal(self, signal: TradeSignal) -> None:
        """Process a trade signal — only for followers of signal.master_wallet."""
        logger.info(
            f"Processing signal from {signal.master_wallet[:10]}...: "
            f"{signal.side} {signal.token_id[:12]}..."
        )

        async with async_session() as session:
            followers = await get_followers_of_wallet(
                session, signal.master_wallet
            )

        if not followers:
            logger.debug(
                f"No followers for wallet {signal.master_wallet[:10]}... — skipping"
            )
            return

        logger.info(
            f"{len(followers)} follower(s) for {signal.master_wallet[:10]}..."
        )

        # Estimate master portfolio value once per signal for proportional sizing
        master_portfolio_usdc = await self._compute_master_portfolio(
            signal.master_wallet
        )

        # Allow a relatively high degree of parallelism for fast copying
        semaphore = asyncio.Semaphore(settings.max_concurrent_trades)

        async def process_with_limit(user: User):
            async with semaphore:
                await self._process_follower(user, signal, master_portfolio_usdc)

        tasks = [process_with_limit(f) for f in followers]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_follower(
        self, user: User, signal: TradeSignal, master_portfolio_usdc: float
    ) -> None:
        """Process a trade signal for a single follower."""
        trade_id = f"ct-{uuid.uuid4().hex[:12]}"
        start_time = time.monotonic()
        tg_id = user.telegram_id

        try:
            async with async_session() as session:
                from bot.services.user_service import get_user_by_telegram_id
                user = await get_user_by_telegram_id(session, user.telegram_id)
                if not user:
                    logger.warning(f"User {tg_id} not found in DB — skipping")
                    return
                if not user.is_active:
                    logger.info(f"User {tg_id} is_active=False — skipping")
                    return
                if user.is_paused:
                    logger.info(f"User {tg_id} is_paused=True — skipping")
                    return
                if not user.encrypted_private_key:
                    logger.warning(f"User {tg_id} has no encrypted_private_key — skipping")
                    await self._notify_error(user, signal, "Clé privée non configurée. Réimportez votre wallet.")
                    return

                user_settings = await get_or_create_settings(session, user)

                if not await self._passes_filters(user_settings, signal):
                    logger.info(f"User {tg_id}: signal filtered out by settings")
                    return

                if user_settings.copy_delay_seconds > 0:
                    logger.debug(f"User {tg_id}: delaying {user_settings.copy_delay_seconds}s")
                    await asyncio.sleep(user_settings.copy_delay_seconds)

                # ── SPEED: decrypt PK once, reuse everywhere ──
                pk = decrypt_private_key(
                    user.encrypted_private_key,
                    settings.encryption_key,
                    user.uuid,
                )

                # Derive signing address from PK — never modify DB mid-flow
                from eth_account import Account as _Acct
                pk_addr = _Acct.from_key(pk).address
                if pk_addr.lower() != (user.wallet_address or "").lower():
                    logger.warning(
                        f"[{tg_id}] PK/wallet mismatch (no auto-fix): "
                        f"db={user.wallet_address[:10]}... pk={pk_addr[:10]}... "
                        f"— using PK address for tx"
                    )

                # ── SPEED: fetch balances in parallel ──
                if user.paper_trading:
                    onchain_balance = user.paper_balance
                    matic_balance = 1.0  # not needed for paper
                else:
                    usdc_task = polygon_client.get_usdc_balance(pk_addr)
                    matic_task = polygon_client.get_matic_balance(pk_addr)
                    onchain_balance, matic_balance = await asyncio.gather(
                        usdc_task, matic_task
                    )

                balance = onchain_balance

                logger.info(
                    f"[{tg_id}] ✅ Checks passed — paper={user.paper_trading}, "
                    f"balance={balance:.2f} USDC, matic={matic_balance:.4f}, "
                    f"sizing_mode={user_settings.sizing_mode}, "
                    f"fixed_amount={user_settings.fixed_amount}"
                )

                try:
                    gross_amount = calculate_trade_size(
                        user_settings,
                        master_amount_usdc=signal.size * signal.price,
                        master_portfolio_usdc=master_portfolio_usdc,
                        current_balance_usdc=balance,
                    )
                except SizingError as e:
                    logger.warning(f"[{tg_id}] ❌ Sizing error: {e}")
                    await self._notify_error(user, signal, f"Erreur de sizing : {e}")
                    return

                logger.info(f"[{tg_id}] 💰 Sized at {gross_amount:.2f} USDC")

                # Daily limit check (paper mode = unlimited)
                if not user.paper_trading:
                    if user.daily_spent_usdc + gross_amount > user.daily_limit_usdc:
                        remaining = max(0, user.daily_limit_usdc - user.daily_spent_usdc)
                        await self._notify_error(
                            user,
                            signal,
                            f"Limite journalière atteinte ({user.daily_spent_usdc:.2f}/"
                            f"{user.daily_limit_usdc:.2f} USDC). "
                            f"Reste disponible : {remaining:.2f} USDC.",
                        )
                        return

                # ── SPEED: skip spread check for speed (market orders fill at best) ──
                # Spread check only logs a warning now instead of blocking
                if not user.paper_trading and signal.side == "BUY":
                    # Fire-and-forget spread log (don't block execution)
                    asyncio.create_task(self._log_spread(signal.token_id, user.telegram_id))

                # For real trading: balance checks + one-time Polymarket approval
                if not user.paper_trading:
                    if gross_amount > onchain_balance + 1e-6:
                        await self._notify_error(
                            user,
                            signal,
                            "Solde USDC insuffisant pour copier ce trade. "
                        "Déposez des fonds via le bouton « 💳 Déposer » du menu principal.",
                        )
                        return

                    if matic_balance < 0.01:
                        await self._notify_error(
                            user,
                            signal,
                            "Solde POL/MATIC trop faible pour payer les frais de gas "
                            "(min ~0.01). Déposez un peu de POL sur votre wallet.",
                        )
                        return

                    # One-time: approve USDC for Polymarket contracts
                    if not user.polymarket_approved:
                        approved = await polymarket_client.ensure_allowances(pk)
                        if not approved:
                            await self._notify_error(
                                user,
                                signal,
                                "Échec de l'approbation USDC pour Polymarket. "
                                "Vérifiez que vous avez assez de POL pour le gas.",
                            )
                            return
                        user.polymarket_approved = True
                        await session.commit()

                try:
                    fee_result = calculate_fee(gross_amount)
                except FeeCalculationError as e:
                    logger.error(f"Fee calculation error: {e}")
                    return

                if self._needs_confirmation(user_settings, gross_amount):
                    reason = (
                        "Confirmation manuelle activée"
                        if user_settings.manual_confirmation
                        else f"Trade de {gross_amount:.2f} USDC > seuil de {user_settings.confirmation_threshold_usdc:.2f} USDC"
                    )
                    logger.warning(
                        f"User {tg_id}: trade BLOQUÉ — {reason}"
                    )
                    await self._notify_error(
                        user,
                        signal,
                        f"⚠️ Trade bloqué : {reason}.\n"
                        "Désactivez la confirmation manuelle ou augmentez le seuil "
                        "dans « ⚙️ Paramètres ».",
                    )
                    return

                # Create trade record
                side = TradeSide.BUY if signal.side == "BUY" else TradeSide.SELL
                trade = Trade(
                    trade_id=trade_id,
                    user_id=user.id,
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    market_question=signal.market_question or signal.outcome,
                    master_wallet=signal.master_wallet,
                    side=side,
                    price=signal.price,
                    gross_amount_usdc=fee_result.gross_amount,
                    fee_amount_usdc=fee_result.fee_amount,
                    net_amount_usdc=fee_result.net_amount,
                    status=TradeStatus.PENDING,
                    is_paper=user.paper_trading,
                )
                session.add(trade)
                await session.flush()

                # Transfer / record platform fee
                fee_tx_hash = None
                if user.paper_trading:
                    # Simulated in paper mode
                    fee_tx_hash = "paper_fee_simulated"
                    trade.status = TradeStatus.FEE_PAID
                elif settings.collect_fees_onchain and settings.fees_wallet:
                    # Optional on-chain fee transfer (slower, can be toggled off for speed)
                    try:
                        transfer_result = await polygon_client.transfer_usdc(
                            from_address=pk_addr,
                            to_address=settings.fees_wallet,
                            amount_usdc=fee_result.fee_amount,
                            private_key=pk,
                        )

                        if not transfer_result.success:
                            trade.status = TradeStatus.FAILED
                            trade.error_message = (
                                f"Fee transfer failed: {transfer_result.error}"
                            )
                            await session.commit()
                            await self._notify_error(
                                user, signal, trade.error_message
                            )
                            return

                        fee_tx_hash = transfer_result.tx_hash
                        trade.fee_tx_hash = fee_tx_hash
                        trade.status = TradeStatus.FEE_PAID

                    except Exception as e:
                        trade.status = TradeStatus.FAILED
                        trade.error_message = f"Fee transfer error: {e}"
                        await session.commit()
                        await self._notify_error(user, signal, str(e))
                        return

                # Record fee
                fee_record = FeeRecord(
                    user_id=user.id,
                    trade_id=trade.id,
                    gross_amount=fee_result.gross_amount,
                    fee_rate=fee_result.fee_rate,
                    fee_amount=fee_result.fee_amount,
                    net_amount=fee_result.net_amount,
                    fees_wallet=fee_result.fees_wallet,
                    tx_hash=fee_tx_hash,
                    confirmed_on_chain=not user.paper_trading,
                    is_paper=user.paper_trading,
                )
                session.add(fee_record)

                # Execute trade on Polymarket
                trade.status = TradeStatus.EXECUTING
                logger.info(f"[{tg_id}] 🚀 Executing {'PAPER' if user.paper_trading else 'LIVE'} trade: {signal.side} {fee_result.net_amount:.2f} USDC on {signal.token_id[:12]}...")

                if user.paper_trading:
                    shares = fee_result.net_amount / signal.price if signal.price > 0 else 0
                    trade.shares = shares
                    trade.status = TradeStatus.FILLED
                    trade.tx_hash = "paper_trade_simulated"
                    if signal.side == "BUY":
                        # Debit paper balance on buy
                        user.paper_balance = max(0, user.paper_balance - fee_result.net_amount)
                    else:
                        # Credit paper balance on sell (proceeds = shares × price)
                        proceeds = fee_result.net_amount  # net after fee
                        user.paper_balance += proceeds
                else:
                    try:
                        order_result = await polymarket_client.place_market_order(
                            private_key=pk,
                            token_id=signal.token_id,
                            side=signal.side,
                            amount_usdc=fee_result.net_amount,
                        )

                        if order_result.success:
                            trade.shares = order_result.filled_size
                            trade.status = TradeStatus.FILLED
                            trade.tx_hash = order_result.order_id
                        else:
                            trade.status = TradeStatus.FAILED
                            trade.error_message = order_result.error
                            await session.commit()
                            await self._notify_error(
                                user, signal, order_result.error
                            )
                            return

                    except Exception as e:
                        trade.status = TradeStatus.FAILED
                        trade.error_message = str(e)
                        await session.commit()
                        await self._notify_error(user, signal, str(e))
                        return

                # Finalize
                elapsed = time.monotonic() - start_time
                trade.execution_time_ms = int(elapsed * 1000)
                trade.executed_at = datetime.utcnow()
                user.daily_spent_usdc += fee_result.gross_amount
                await session.commit()

                await self._notify_success(
                    user, trade, fee_result, elapsed, signal
                )

                logger.info(
                    f"Trade copied for user {user.telegram_id}: "
                    f"{trade.side.value} {trade.net_amount_usdc:.2f} USDC "
                    f"(master: {signal.master_wallet[:10]}...) "
                    f"in {elapsed:.1f}s"
                )

        except Exception as e:
            logger.error(
                f"❌ CRASH processing follower {tg_id}: {e}",
                exc_info=True,
            )
            # Try to notify user even on crash (user may be detached from session)
            try:
                if self._bot:
                    from bot.handlers.notifications import format_trade_error
                    text = format_trade_error(
                        market_question=signal.market_question or signal.outcome,
                        error_message=f"Erreur inattendue : {str(e)[:200]}",
                    )
                    await self._bot.send_message(
                        chat_id=tg_id, text=text, parse_mode="Markdown",
                    )
            except Exception:
                pass

    async def _passes_filters(self, user_settings, signal: TradeSignal) -> bool:
        """Check if a signal passes the user's filters (blacklist, categories, expiry)."""
        # Market blacklist
        if user_settings.blacklisted_markets:
            if signal.market_id in user_settings.blacklisted_markets:
                return False

        needs_market_meta = bool(
            (user_settings.categories and len(user_settings.categories) > 0)
            or user_settings.max_expiry_days
        )
        if not needs_market_meta:
            return True

        market = await polymarket_client.get_market_by_condition_id(signal.market_id)
        if not market:
            # If we can't resolve market metadata, don't block the trade
            logger.warning(
                f"Could not fetch market metadata for {signal.market_id[:10]}..., "
                "skipping category/expiry filters."
            )
            return True

        # Category filter
        if user_settings.categories:
            if not market.category or market.category not in user_settings.categories:
                return False

        # Max expiry filter
        if user_settings.max_expiry_days and market.end_date:
            try:
                end_str = market.end_date
                if end_str.endswith("Z"):
                    end_str = end_str[:-1] + "+00:00"
                end_dt = datetime.fromisoformat(end_str)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                delta_days = (end_dt - now).days
                if delta_days > user_settings.max_expiry_days:
                    return False
            except Exception as e:
                logger.warning(
                    f"Failed to parse market end_date '{market.end_date}' "
                    f"for {signal.market_id[:10]}...: {e}"
                )

        return True

    async def _compute_master_portfolio(self, master_wallet: str) -> float:
        """Estimate the master's portfolio value in USDC — cached for 30s."""
        cached = self._portfolio_cache.get(master_wallet)
        if cached:
            value, ts = cached
            if time.monotonic() - ts < 30:
                return value

        try:
            positions = await polymarket_client.get_positions_by_address(master_wallet)
            total = sum(p.size * p.current_price for p in positions)
            if total <= 0:
                total = self._master_portfolio_usdc
            self._portfolio_cache[master_wallet] = (total, time.monotonic())
            return total
        except Exception as e:
            logger.error(
                f"Failed to compute master portfolio for {master_wallet[:10]}...: {e}"
            )
            return self._master_portfolio_usdc

    async def _log_spread(self, token_id: str, tg_id: int) -> None:
        """Fire-and-forget spread logging — does NOT block trade execution."""
        try:
            spread = await self._check_spread(token_id)
            if spread is not None and spread > 0.05:
                logger.warning(
                    f"User {tg_id}: spread {spread:.1%} wide for "
                    f"{token_id[:12]}... (trade still executed)"
                )
        except Exception:
            pass  # Never block for spread logging

    async def _check_spread(self, token_id: str) -> Optional[float]:
        """Check the bid-ask spread for a token. Returns spread as a fraction (0.05 = 5%)."""
        try:
            book = await polymarket_client.get_order_book(token_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks:
                return None
            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 0))
            if best_bid <= 0 or best_ask <= 0:
                return None
            mid = (best_bid + best_ask) / 2
            return (best_ask - best_bid) / mid
        except Exception as e:
            logger.warning(f"Failed to check spread for {token_id[:12]}...: {e}")
            return None

    def _needs_confirmation(
        self, user_settings, amount: float
    ) -> bool:
        if user_settings.manual_confirmation:
            return True
        if amount > user_settings.confirmation_threshold_usdc:
            return True
        return False

    async def _notify_success(
        self,
        user: User,
        trade: Trade,
        fee_result: FeeResult,
        elapsed: float,
        signal: TradeSignal,
    ) -> None:
        """Send success notification via Telegram."""
        if not self._bot:
            return

        from bot.handlers.notifications import format_trade_notification
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        text = format_trade_notification(
            trade=trade,
            fee_result=fee_result,
            execution_time_s=elapsed,
            bridge_used=trade.bridge_used,
            master_pnl=signal.master_pnl_pct,
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📊 Positions", callback_data="menu_positions"),
                InlineKeyboardButton("⚙️ Paramètres", callback_data="menu_settings"),
            ],
        ])

        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"Failed to send notification to {user.telegram_id}: {e}")

    async def _notify_error(
        self,
        user: User,
        signal: TradeSignal,
        error: str,
    ) -> None:
        """Send error notification via Telegram."""
        if not self._bot:
            return

        from bot.handlers.notifications import format_trade_error

        text = format_trade_error(
            market_question=signal.market_question or signal.outcome,
            error_message=error,
        )

        try:
            await self._bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Failed to send error notification: {e}")
