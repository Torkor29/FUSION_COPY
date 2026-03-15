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


async def settle_paper_trades() -> None:
    """Settle paper trades whose markets have resolved. Runs every 5 minutes.

    Logic:
    - Find all FILLED paper trades that are NOT settled
    - Group by market_id (conditionId)
    - Check each market for resolution via Gamma API
    - If resolved:
        - Winning token → shares × $1.00 credited to paper_balance
        - Losing token → $0.00 (loss already debited at buy time)
        - Mark trade as is_settled=True with settlement_pnl
    """
    from bot.services.polymarket import polymarket_client

    try:
        async with async_session() as session:
            # Find all unsettled paper trades
            result = await session.execute(
                select(Trade).where(
                    Trade.is_paper == True,  # noqa: E712
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
                f"{len(unsettled)} unsettled paper trade(s)"
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

                for trade in trades:
                    shares = trade.shares or 0
                    invested = trade.net_amount_usdc

                    if trade.token_id == winning_token:
                        # Winner: shares × $1.00
                        payout = shares * 1.0
                        pnl = payout - invested
                    else:
                        # Loser: $0.00 payout
                        payout = 0.0
                        pnl = -invested

                    trade.is_settled = True
                    trade.settlement_pnl = pnl

                    # Credit payout to user's paper balance
                    user = await session.get(User, trade.user_id)
                    if user and payout > 0:
                        user.paper_balance += payout

                    settled_count += 1
                    logger.info(
                        f"Settled paper trade {trade.trade_id}: "
                        f"{'WIN' if pnl >= 0 else 'LOSS'} "
                        f"pnl={pnl:+.2f} payout={payout:.2f}"
                    )

            if settled_count > 0:
                await session.commit()
                logger.info(f"Settled {settled_count} paper trade(s) (checked {checked_count}/{len(by_market)} markets)")
            elif checked_count > 0:
                logger.debug(f"Checked {checked_count}/{len(by_market)} markets — none resolved yet")

    except Exception as e:
        logger.error(f"Error settling paper trades: {e}", exc_info=True)


async def health_check() -> None:
    """Periodic health check — verify DB and services. Runs every 5 minutes."""
    try:
        async with async_session() as session:
            from sqlalchemy import text
            await session.execute(text("SELECT 1"))
        logger.debug("Health check: DB OK")
    except Exception as e:
        logger.error(f"Health check failed: {e}")
