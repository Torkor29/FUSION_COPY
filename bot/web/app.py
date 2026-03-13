"""FastAPI dashboard — read-only web interface for trade monitoring."""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from bot.db.session import async_session
from bot.models.trade import Trade, TradeStatus, TradeSide
from bot.models.fee import FeeRecord
from bot.models.user import User, UserRole
from bot.models.settings import UserSettings

logger = logging.getLogger(__name__)

app = FastAPI(title="WENPOLYMARKET Dashboard", docs_url=None, redoc_url=None)

TEMPLATE_DIR = Path(__file__).parent / "templates"


@app.get("/", response_class=HTMLResponse)
async def dashboard_page():
    """Serve the main dashboard HTML page."""
    html_path = TEMPLATE_DIR / "dashboard.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/dashboard")
async def api_dashboard():
    """Global stats: trades today/week, volume, fees, win rate."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    async with async_session() as session:
        # Trades today
        trades_today = await session.scalar(
            select(func.count(Trade.id)).where(
                Trade.status == TradeStatus.FILLED,
                Trade.created_at >= today_start,
            )
        ) or 0

        # Trades this week
        trades_week = await session.scalar(
            select(func.count(Trade.id)).where(
                Trade.status == TradeStatus.FILLED,
                Trade.created_at >= week_start,
            )
        ) or 0

        # Total trades all time
        trades_total = await session.scalar(
            select(func.count(Trade.id)).where(
                Trade.status == TradeStatus.FILLED,
            )
        ) or 0

        # Volume today
        volume_today = await session.scalar(
            select(func.sum(Trade.gross_amount_usdc)).where(
                Trade.status == TradeStatus.FILLED,
                Trade.created_at >= today_start,
            )
        ) or 0.0

        # Volume this week
        volume_week = await session.scalar(
            select(func.sum(Trade.gross_amount_usdc)).where(
                Trade.status == TradeStatus.FILLED,
                Trade.created_at >= week_start,
            )
        ) or 0.0

        # Total fees
        total_fees = await session.scalar(
            select(func.sum(FeeRecord.fee_amount))
        ) or 0.0

        # Active followers
        active_followers = await session.scalar(
            select(func.count(User.id)).where(
                User.role == UserRole.FOLLOWER,
                User.is_active == True,
            )
        ) or 0

        # Win rate (from closed trades)
        buy_trades = (await session.execute(
            select(Trade).where(
                Trade.status == TradeStatus.FILLED,
                Trade.side == TradeSide.BUY,
            )
        )).scalars().all()

        sell_trades = (await session.execute(
            select(Trade).where(
                Trade.status == TradeStatus.FILLED,
                Trade.side == TradeSide.SELL,
            )
        )).scalars().all()

        buy_avg: dict[str, float] = {}
        for t in buy_trades:
            if t.token_id not in buy_avg:
                buy_avg[t.token_id] = t.price
            else:
                buy_avg[t.token_id] = (buy_avg[t.token_id] + t.price) / 2

        wins = 0
        total_closed = 0
        total_pnl = 0.0
        for t in sell_trades:
            avg_buy = buy_avg.get(t.token_id)
            if avg_buy is not None and avg_buy > 0:
                pnl = (t.price - avg_buy) * t.shares
                total_pnl += pnl
                total_closed += 1
                if pnl > 0:
                    wins += 1

        win_rate = round((wins / total_closed) * 100, 1) if total_closed > 0 else None

    return {
        "trades_today": trades_today,
        "trades_week": trades_week,
        "trades_total": trades_total,
        "volume_today": round(volume_today, 2),
        "volume_week": round(volume_week, 2),
        "total_fees": round(total_fees, 2),
        "active_followers": active_followers,
        "win_rate": win_rate,
        "total_pnl": round(total_pnl, 2),
        "total_closed": total_closed,
    }


@app.get("/api/traders")
async def api_traders():
    """List followed traders with their performance stats."""
    async with async_session() as session:
        # Get all unique followed wallets
        all_settings = (await session.execute(
            select(UserSettings)
        )).scalars().all()

        wallet_set: set[str] = set()
        for s in all_settings:
            if s.followed_wallets:
                for w in s.followed_wallets:
                    if w:
                        wallet_set.add(w.lower())

        traders = []
        for wallet in wallet_set:
            # Count followers
            follower_count = 0
            for s in all_settings:
                if s.followed_wallets and wallet in [
                    w.lower() for w in s.followed_wallets
                ]:
                    follower_count += 1

            # Trade stats for this master wallet
            trade_count = await session.scalar(
                select(func.count(Trade.id)).where(
                    Trade.master_wallet == wallet,
                    Trade.status == TradeStatus.FILLED,
                )
            ) or 0

            volume = await session.scalar(
                select(func.sum(Trade.gross_amount_usdc)).where(
                    Trade.master_wallet == wallet,
                    Trade.status == TradeStatus.FILLED,
                )
            ) or 0.0

            traders.append({
                "wallet": wallet,
                "wallet_short": f"{wallet[:6]}...{wallet[-4:]}",
                "follower_count": follower_count,
                "trade_count": trade_count,
                "volume": round(volume, 2),
            })

        # Sort by trade count descending
        traders.sort(key=lambda x: x["trade_count"], reverse=True)

    return {"traders": traders}


@app.get("/api/trades")
async def api_trades(
    period: str = Query("today", regex="^(today|week|all)$"),
    master: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    """Recent trades, filterable by period and master wallet."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    async with async_session() as session:
        query = select(Trade).where(
            Trade.status == TradeStatus.FILLED,
        )

        if period == "today":
            query = query.where(Trade.created_at >= today_start)
        elif period == "week":
            query = query.where(Trade.created_at >= week_start)

        if master:
            query = query.where(Trade.master_wallet == master.lower())

        query = query.order_by(Trade.created_at.desc()).limit(limit)

        result = await session.execute(query)
        trades = result.scalars().all()

    return {
        "trades": [
            {
                "trade_id": t.trade_id,
                "market_question": t.market_question or t.market_id[:20],
                "side": t.side.value.upper(),
                "price": round(t.price, 4),
                "amount": round(t.net_amount_usdc, 2),
                "fee": round(t.fee_amount_usdc, 2),
                "shares": round(t.shares, 4),
                "master_wallet": t.master_wallet or "",
                "master_short": (
                    f"{t.master_wallet[:6]}...{t.master_wallet[-4:]}"
                    if t.master_wallet else ""
                ),
                "is_paper": t.is_paper,
                "execution_time_ms": t.execution_time_ms,
                "created_at": t.created_at.isoformat() if t.created_at else "",
            }
            for t in trades
        ],
        "count": len(trades),
        "period": period,
    }
