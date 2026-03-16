"""APScheduler tasks — periodic maintenance jobs."""

import logging
from datetime import datetime

from sqlalchemy import update, select

from bot.db.session import async_session
from bot.models.user import User
from bot.models.trade import Trade, TradeStatus, TradeSide
from bot.services.otp import otp_service

logger = logging.getLogger(__name__)


async def reset_daily_limits() -> None:
    """Reset all users' daily spend counters. Runs at midnight UTC."""
    async with async_session() as session:
        await session.execute(
            update(User).values(daily_spent_usdc=0.0)
        )
        await session.commit()

    logger.info("Daily spending limits reset for all users")


async def cleanup_expired_otps() -> None:
    """Remove expired OTP challenges. Runs every 10 minutes."""
    count = otp_service.cleanup_expired()
    if count > 0:
        logger.info(f"Cleaned up {count} expired OTP challenges")


async def settle_trades(bot=None) -> None:
    """Settle ALL trades (paper + live) whose markets have resolved.

    Runs every 2 minutes. For each unsettled FILLED BUY trade:
    - Check market resolution via Polymarket API
    - Calculate PNL: winner gets shares × $1, loser gets $0
    - Store market_outcome and settlement_pnl
    - Paper mode: update paper_balance
    - Notify user of result (auto-delete after 120s)
    """
    from bot.services.polymarket import polymarket_client

    try:
        async with async_session() as session:
            # Find ALL unsettled trades (paper + live)
            result = await session.execute(
                select(Trade).where(
                    Trade.is_settled == False,  # noqa: E712
                    Trade.status == TradeStatus.FILLED,
                    Trade.side == TradeSide.BUY,
                )
            )
            unsettled = list(result.scalars().all())

            if not unsettled:
                return

            # Group by market_id to avoid duplicate API calls
            by_market: dict[str, list[Trade]] = {}
            for trade in unsettled:
                by_market.setdefault(trade.market_id, []).append(trade)

            logger.info(
                f"Checking {len(by_market)} market(s) for "
                f"{len(unsettled)} unsettled trade(s)"
            )

            settled_count = 0
            checked_count = 0
            for market_id, trades in by_market.items():
                try:
                    resolution = await polymarket_client.check_market_resolution(market_id)
                except Exception as e:
                    logger.warning(f"Failed to check resolution for {market_id[:16]}...: {e}")
                    continue
                checked_count += 1
                if resolution is None:
                    continue  # Market still open

                winning_token = resolution.get("winning_token_id", "")
                winning_outcome = resolution.get("winning_outcome", "")

                for trade in trades:
                    shares = trade.shares or (
                        trade.net_amount_usdc / trade.price
                        if trade.price > 0 else 0
                    )
                    invested = trade.net_amount_usdc

                    won = trade.token_id == winning_token
                    if won:
                        payout = shares * 1.0
                        pnl = payout - invested
                    else:
                        payout = 0.0
                        pnl = -invested

                    trade.is_settled = True
                    trade.settlement_pnl = pnl
                    trade.market_outcome = winning_outcome

                    # Credit payout to paper balance
                    if trade.is_paper:
                        user = await session.get(User, trade.user_id)
                        if user:
                            user.paper_balance = (user.paper_balance or 0) + payout

                    settled_count += 1
                    logger.info(
                        f"Settled {'paper' if trade.is_paper else 'live'} "
                        f"trade {trade.trade_id}: "
                        f"{'WIN' if won else 'LOSS'} "
                        f"pnl={pnl:+.2f} payout={payout:.2f}"
                    )

                    # Notify user
                    if bot:
                        await _notify_settlement(
                            bot, session, trade, won, pnl, payout
                        )

            if settled_count > 0:
                await session.commit()
                logger.info(f"Settled {settled_count} trade(s) (checked {checked_count}/{len(by_market)} markets)")
            elif checked_count > 0:
                logger.debug(f"Checked {checked_count}/{len(by_market)} markets — none resolved yet")

    except Exception as e:
        logger.error(f"Error settling trades: {e}", exc_info=True)


# Keep old name as alias for backward compat with main.py
settle_paper_trades = settle_trades


async def _notify_settlement(bot, session, trade, won, pnl, payout):
    """Send settlement notification to user."""
    import asyncio

    try:
        user = await session.get(User, trade.user_id)
        if not user or not user.telegram_id:
            return

        emoji = "🟢" if won else "🔴"
        result_text = "GAGNÉ" if won else "PERDU"
        paper = " 📝 PAPER" if trade.is_paper else ""
        q = trade.market_question or trade.market_id[:20]
        outcome = trade.market_outcome or "?"

        text = (
            f"{emoji} **MARCHÉ RÉSOLU**{paper}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 {q}\n"
            f"🏆 Résultat : **{outcome}** → **{result_text}**\n"
            f"💰 Mise : {trade.net_amount_usdc:.2f} USDC\n"
            f"💵 Payout : {payout:.2f} USDC\n"
            f"📈 P&L : **{pnl:+.2f} USDC**"
        )

        msg = await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            parse_mode="Markdown",
        )

        # Auto-delete after 120s
        async def _auto_del():
            await asyncio.sleep(120)
            try:
                await msg.delete()
            except Exception:
                pass
        asyncio.create_task(_auto_del())

    except Exception as e:
        logger.error(f"Settlement notification error: {e}")


async def health_check() -> None:
    """Periodic health check — verify DB and services. Runs every 5 minutes."""
    try:
        async with async_session() as session:
            from sqlalchemy import text
            await session.execute(text("SELECT 1"))
        logger.debug("Health check: DB OK")
    except Exception as e:
        logger.error(f"Health check failed: {e}")
