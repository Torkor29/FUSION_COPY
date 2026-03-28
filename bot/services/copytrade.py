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
    """Main copytrade orchestrator — optimized for sub-second execution.

    V3: Now supports optional smart analysis services:
    - signal_scorer: Scores signals 0-100 before execution
    - smart_filter: Pattern-based trade filtering
    - portfolio_manager: Portfolio-level risk controls
    - position_manager: Active SL/TP enforcement
    - trader_tracker: Performance tracking for sizing adjustments
    - topic_router: Routes notifications to Telegram group topics
    """

    def __init__(
        self,
        telegram_bot=None,
        signal_scorer=None,
        smart_filter=None,
        portfolio_manager=None,
        position_manager=None,
        trader_tracker=None,
        topic_router=None,
    ):
        self._bot = telegram_bot
        self._master_portfolio_usdc: float = 10000.0
        # Cache master portfolio values (refreshed each signal)
        self._portfolio_cache: dict[str, tuple[float, float]] = {}  # wallet -> (value, timestamp)
        _PORTFOLIO_CACHE_TTL = 30  # seconds

        # V3 — Smart Analysis services (all optional for backward compat)
        self._signal_scorer = signal_scorer
        self._smart_filter = smart_filter
        self._portfolio_manager = portfolio_manager
        self._position_manager = position_manager
        self._trader_tracker = trader_tracker
        self._topic_router = topic_router

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

        # ── V3: Score signal ONCE before processing followers ──
        signal_score = None
        if self._signal_scorer:
            try:
                signal_score = await self._signal_scorer.score_signal(signal)
                # Attach score to signal for per-follower threshold check
                signal._v3_score = signal_score
                # Post scored signal to group topic
                if self._topic_router:
                    from bot.services.signal_scorer import SignalScorer
                    score_text = SignalScorer.format_score(signal_score, signal)
                    await self._topic_router.send_signal(score_text)
            except Exception as e:
                logger.warning("Signal scoring failed (continuing): %s", e)
                signal._v3_score = None

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

                # ══════════════════════════════════════════════
                # SAFETY GATE: triple-check before allowing live trades
                # ══════════════════════════════════════════════
                is_paper = user.paper_trading  # snapshot for entire flow

                if not is_paper:
                    # Check 1: live_mode_confirmed must be True
                    live_confirmed = getattr(user, "live_mode_confirmed", False)
                    if not live_confirmed:
                        logger.warning(
                            f"[{tg_id}] ⚠️ SAFETY: paper=False but "
                            f"live_mode_confirmed=False — forcing paper"
                        )
                        user.paper_trading = True
                        is_paper = True
                        await session.commit()

                    # Check 2: must have encrypted PK for live
                    if not is_paper and not user.encrypted_private_key:
                        logger.warning(
                            f"[{tg_id}] ⚠️ SAFETY: live mode but no PK — forcing paper"
                        )
                        user.paper_trading = True
                        is_paper = True
                        await session.commit()

                if not await self._passes_filters(user_settings, signal):
                    logger.info(f"User {tg_id}: signal filtered out by settings")
                    return

                # ── V3 Checkpoint 1: Signal Score threshold ──
                v3_score = getattr(signal, "_v3_score", None)
                if (
                    v3_score
                    and getattr(user_settings, "signal_scoring_enabled", False)
                ):
                    min_score = getattr(user_settings, "min_signal_score", 40.0)

                    # Recalculate score with user's per-criteria config
                    # (reuses raw scores from central scoring, just reweights)
                    user_criteria = getattr(user_settings, "scoring_criteria", None)
                    if user_criteria and v3_score.components:
                        from bot.services.signal_scorer import compute_weights
                        user_weights = compute_weights(user_criteria)
                        user_total = 0
                        for k, w in user_weights.items():
                            comp = v3_score.components.get(k, {})
                            raw_score = comp.get("score", 50) if isinstance(comp, dict) else float(comp)
                            user_total += raw_score * w
                        user_total = round(min(100, max(0, user_total)), 1)
                    else:
                        user_total = v3_score.total_score

                    if user_total < min_score:
                        logger.info(
                            f"User {tg_id}: signal score {user_total:.0f} "
                            f"< threshold {min_score:.0f} — skipping"
                        )
                        return

                # ── V3 Checkpoint 2: Smart Filter ──
                if self._smart_filter:
                    try:
                        should_copy, reason = await self._smart_filter.should_copy(
                            signal, user_settings
                        )
                        if not should_copy:
                            logger.info(
                                f"User {tg_id}: smart filter blocked — {reason}"
                            )
                            # Notify user that signal was filtered
                            if self._topic_router:
                                from bot.handlers.notifications import format_signal_blocked
                                v3s = getattr(signal, "_v3_score", None)
                                blocked_text = format_signal_blocked(
                                    market_question=signal.market_question or signal.market_id[:20],
                                    reason=reason,
                                    score=v3s.total_score if v3s else 0,
                                )
                                notif_mode = getattr(user_settings, "notification_mode", "dm")
                                await self._topic_router.notify_user(
                                    user_telegram_id=tg_id,
                                    text=blocked_text,
                                    notification_mode=notif_mode,
                                    topic="signals",
                                )
                            return
                    except Exception as e:
                        logger.warning("Smart filter error (allowing): %s", e)

                # ── V3 Checkpoint 3: Portfolio risk check ──
                if self._portfolio_manager and signal.side == "BUY":
                    try:
                        from bot.services.market_categories import categorize_market
                        market_cat = categorize_market(
                            title=signal.market_question or "",
                            slug="",
                            api_category="",
                        )
                        allowed, reason = await self._portfolio_manager.check_can_open(
                            user.id,
                            signal.market_id,
                            market_cat.category if market_cat else "Other",
                            signal.side,
                            max_positions=getattr(user_settings, "max_positions", 15),
                            max_category_exposure_pct=getattr(
                                user_settings, "max_category_exposure_pct", 30.0
                            ),
                            max_direction_bias_pct=getattr(
                                user_settings, "max_direction_bias_pct", 70.0
                            ),
                        )
                        if not allowed:
                            logger.info(
                                f"User {tg_id}: portfolio blocked — {reason}"
                            )
                            if self._topic_router:
                                await self._topic_router.send_alert(
                                    f"⚠️ Trade blocked for user {tg_id}: {reason}"
                                )
                            return
                    except Exception as e:
                        logger.warning("Portfolio check error (allowing): %s", e)

                if user_settings.copy_delay_seconds > 0:
                    logger.debug(f"User {tg_id}: delaying {user_settings.copy_delay_seconds}s")
                    await asyncio.sleep(user_settings.copy_delay_seconds)

                # ── Decrypt PK only for LIVE mode ──
                pk = None
                pk_addr = user.wallet_address or ""

                if not is_paper:
                    pk = decrypt_private_key(
                        user.encrypted_private_key,
                        settings.encryption_key,
                        user.uuid,
                    )
                    from eth_account import Account as _Acct
                    pk_addr = _Acct.from_key(pk).address
                    if pk_addr.lower() != (user.wallet_address or "").lower():
                        logger.warning(
                            f"[{tg_id}] PK/wallet mismatch (no auto-fix): "
                            f"db={user.wallet_address[:10]}... pk={pk_addr[:10]}... "
                            f"— using PK address for tx"
                        )

                # ── Fetch balances ──
                if is_paper:
                    onchain_balance = user.paper_balance
                    matic_balance = 1.0  # not needed for paper
                else:
                    usdc_task = polygon_client.get_usdc_balance(pk_addr)
                    matic_task = polygon_client.get_matic_balance(pk_addr)
                    onchain_balance, matic_balance = await asyncio.gather(
                        usdc_task, matic_task
                    )

                balance = onchain_balance

                # C1 FIX: Reject paper trade if insufficient balance (don't clamp to 0)
                if is_paper and balance <= 0:
                    await self._notify_error(
                        user, signal,
                        "Solde paper insuffisant (0 USDC). "
                        "Votre portefeuille paper est vide.",
                    )
                    return

                logger.info(
                    f"[{tg_id}] ✅ Checks passed — paper={is_paper}, "
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

                # C2 FIX: Atomic daily limit check with row-level lock
                if not is_paper:
                    from sqlalchemy import select, text as sa_text
                    locked_row = await session.execute(
                        select(User).where(User.id == user.id).with_for_update()
                    )
                    user = locked_row.scalar_one()
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
                if not is_paper and signal.side == "BUY":
                    # Fire-and-forget spread log (don't block execution)
                    asyncio.create_task(self._log_spread(signal.token_id, user.telegram_id))

                # For real trading: balance checks + one-time Polymarket approval
                if not is_paper:
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
                    is_paper=is_paper,
                )
                session.add(trade)
                await session.flush()

                # ── Execute trade FIRST, collect fee AFTER (C3 FIX) ──
                trade.status = TradeStatus.EXECUTING
                logger.info(f"[{tg_id}] 🚀 Executing {'PAPER' if is_paper else 'LIVE'} trade: {signal.side} {fee_result.net_amount:.2f} USDC on {signal.token_id[:12]}...")

                if is_paper:
                    shares = fee_result.net_amount / signal.price if signal.price > 0 else 0
                    trade.shares = shares
                    trade.status = TradeStatus.FILLED
                    trade.tx_hash = "paper_trade_simulated"
                    if signal.side == "BUY":
                        # C1 FIX: Reject if paper balance insufficient (no clamping)
                        if fee_result.gross_amount > user.paper_balance + 1e-6:
                            trade.status = TradeStatus.FAILED
                            trade.error_message = "Solde paper insuffisant"
                            await session.commit()
                            await self._notify_error(
                                user, signal,
                                f"Solde paper insuffisant : {user.paper_balance:.2f} USDC "
                                f"disponible, {fee_result.gross_amount:.2f} USDC requis.",
                            )
                            return
                        user.paper_balance -= fee_result.gross_amount
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

                # C3 FIX: Fee transfer AFTER successful trade execution
                fee_tx_hash = None
                if is_paper:
                    fee_tx_hash = "paper_fee_simulated"
                elif settings.collect_fees_onchain and settings.fees_wallet:
                    # Use user's gas priority setting for faster confirmation
                    from bot.models.settings import GasMode, GAS_PRIORITY_FEES
                    user_gas_mode = getattr(user_settings, "gas_mode", GasMode.FAST)
                    pf_gwei = GAS_PRIORITY_FEES.get(user_gas_mode, 30)

                    try:
                        transfer_result = await polygon_client.transfer_usdc(
                            from_address=pk_addr,
                            to_address=settings.fees_wallet,
                            amount_usdc=fee_result.fee_amount,
                            private_key=pk,
                            priority_fee_gwei=pf_gwei,
                        )
                        if transfer_result.success:
                            fee_tx_hash = transfer_result.tx_hash
                            trade.fee_tx_hash = fee_tx_hash
                        else:
                            # Trade succeeded but fee failed — log but don't fail the trade
                            logger.error(
                                f"[{tg_id}] Fee transfer failed AFTER trade: "
                                f"{transfer_result.error} — trade still valid"
                            )
                    except Exception as e:
                        logger.error(
                            f"[{tg_id}] Fee transfer error AFTER trade: {e} "
                            f"— trade still valid"
                        )

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
                    confirmed_on_chain=bool(fee_tx_hash and fee_tx_hash != "paper_fee_simulated"),
                    is_paper=is_paper,
                )
                session.add(fee_record)

                # Finalize
                elapsed = time.monotonic() - start_time
                trade.execution_time_ms = int(elapsed * 1000)
                trade.executed_at = datetime.utcnow()
                # Only count daily spending for LIVE trades (paper has no limit)
                if not is_paper:
                    user.daily_spent_usdc += fee_result.gross_amount
                await session.commit()

                # M3 FIX: Clear PK from memory after use
                if pk is not None:
                    del pk
                    pk = None

                await self._notify_success(
                    user, trade, fee_result, elapsed, signal
                )

                # ── V3: Register position for active SL/TP monitoring ──
                if (
                    self._position_manager
                    and signal.side == "BUY"
                    and trade.status == TradeStatus.FILLED
                ):
                    try:
                        sl_pct = (
                            user_settings.stop_loss_pct
                            if getattr(user_settings, "stop_loss_enabled", False)
                            else None
                        )
                        tp_pct = (
                            user_settings.take_profit_pct
                            if getattr(user_settings, "take_profit_enabled", False)
                            else None
                        )
                        trailing = (
                            getattr(user_settings, "trailing_stop_pct", None)
                            if getattr(user_settings, "trailing_stop_enabled", False)
                            else None
                        )
                        await self._position_manager.register_position(
                            user_id=user.id,
                            trade_id=trade.trade_id,
                            market_id=signal.market_id,
                            token_id=signal.token_id,
                            outcome=signal.outcome or "YES",
                            entry_price=signal.price,
                            shares=trade.shares or 0,
                            market_question=signal.market_question or "",
                            sl_pct=sl_pct,
                            tp_pct=tp_pct,
                            trailing_stop_pct=trailing,
                        )
                    except Exception as e:
                        logger.warning("Failed to register position for monitoring: %s", e)

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
                    # V3: Route crash errors to Alerts topic
                    if self._topic_router:
                        await self._topic_router.notify_user(
                            user_telegram_id=tg_id,
                            text=text,
                            notification_mode="both",  # Crash = always DM + group
                            topic="alerts",
                        )
                    else:
                        await self._bot.send_message(
                            chat_id=tg_id, text=text, parse_mode="Markdown",
                        )
            except Exception:
                pass

    async def _passes_filters(self, user_settings, signal: TradeSignal) -> bool:
        """Check if a signal passes the user's filters (blacklist, categories, expiry,
        and per-trader category exclusions)."""
        # Market blacklist
        if user_settings.blacklisted_markets:
            if signal.market_id in user_settings.blacklisted_markets:
                return False

        # Check if we need market metadata for any filter
        has_global_cat_filter = bool(
            user_settings.categories and len(user_settings.categories) > 0
        )
        has_per_trader_filter = False
        trader_excluded = []
        if user_settings.trader_filters and signal.master_wallet:
            wallet_lower = signal.master_wallet.lower()
            tf = user_settings.trader_filters.get(wallet_lower, {})
            trader_excluded = tf.get("excluded_categories", [])
            if trader_excluded:
                has_per_trader_filter = True

        needs_market_meta = bool(
            has_global_cat_filter
            or has_per_trader_filter
            or user_settings.max_expiry_days
        )
        if not needs_market_meta:
            return True

        market = await polymarket_client.get_market_by_condition_id(signal.market_id)
        if not market:
            logger.warning(
                f"Could not fetch market metadata for {signal.market_id[:10]}..., "
                "skipping category/expiry filters."
            )
            return True

        # Global category filter (whitelist)
        if has_global_cat_filter:
            if not market.category or market.category not in user_settings.categories:
                return False

        # Per-trader category exclusion filter
        if has_per_trader_filter and trader_excluded:
            from bot.services.market_categories import categorize_market

            market_cat = categorize_market(
                title=signal.market_question or market.question or "",
                slug=market.slug or "",
                api_category=market.category or "",
            )
            # Check both exact tag ("Crypto/BTC") and top-level ("Crypto")
            if market_cat.tag in trader_excluded or market_cat.category in trader_excluded:
                logger.info(
                    f"Trade blocked by per-trader filter: "
                    f"{signal.master_wallet[:10]}... → {market_cat.tag} "
                    f"(excluded: {trader_excluded})"
                )
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
        """Send success notification via Telegram.

        V3: Routes to Signals topic + DM based on user notification_mode.
        """
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
            import asyncio as _asyncio

            # V3: Route via TopicRouter based on user preference
            if self._topic_router:
                async with async_session() as session:
                    us = await get_or_create_settings(session, user)
                    notif_mode = getattr(us, "notification_mode", "dm")

                await self._topic_router.notify_user(
                    user_telegram_id=user.telegram_id,
                    text=text,
                    notification_mode=notif_mode,
                    topic="signals",
                    reply_markup=keyboard,
                )
            else:
                # Fallback: DM only (V1 compat)
                msg = await self._bot.send_message(
                    chat_id=user.telegram_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )

                async def _auto_del():
                    await _asyncio.sleep(60)
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                _asyncio.create_task(_auto_del())

        except Exception as e:
            logger.error(f"Failed to send notification to {user.telegram_id}: {e}")

    # Rate-limit: track last error notification per user to avoid spam
    _last_error_notify: dict[int, float] = {}
    _ERROR_COOLDOWN = 300  # 5 minutes between identical error types

    async def _notify_error(
        self,
        user: User,
        signal: TradeSignal,
        error: str,
    ) -> None:
        """Send error notification via Telegram (rate-limited).

        V3: Routes to Alerts topic + DM based on user notification_mode.
        """
        if not self._bot:
            return

        import time as _time
        import asyncio as _asyncio

        # Rate-limit: max 1 error notification per 5 min per user
        now = _time.time()
        last = self._last_error_notify.get(user.telegram_id, 0)
        if now - last < self._ERROR_COOLDOWN:
            logger.debug(
                f"Skipping error notification for {user.telegram_id} "
                f"(cooldown {self._ERROR_COOLDOWN}s)"
            )
            return
        self._last_error_notify[user.telegram_id] = now

        from bot.handlers.notifications import format_trade_error

        text = format_trade_error(
            market_question=signal.market_question or signal.outcome,
            error_message=error,
        )

        try:
            # V3: Route to Alerts topic
            if self._topic_router:
                async with async_session() as session:
                    us = await get_or_create_settings(session, user)
                    notif_mode = getattr(us, "notification_mode", "dm")

                await self._topic_router.notify_user(
                    user_telegram_id=user.telegram_id,
                    text=text,
                    notification_mode=notif_mode,
                    topic="alerts",
                )
            else:
                # Fallback: DM only (V1 compat)
                msg = await self._bot.send_message(
                    chat_id=user.telegram_id,
                    text=text,
                    parse_mode="Markdown",
                )

                async def _auto_del():
                    await _asyncio.sleep(30)
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                _asyncio.create_task(_auto_del())

        except Exception as e:
            logger.error(f"Failed to send error notification: {e}")
