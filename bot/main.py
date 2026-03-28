"""Main entry point for the WENPOLYMARKET V3 Smart CopyTrading Bot."""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

from bot.config import settings
from bot.db.session import init_db
from bot.handlers.start import get_start_handler, get_setup_group_handler
from bot.handlers.settings import get_settings_handler
from bot.handlers.balance import get_balance_handlers
from bot.handlers.controls import get_control_handlers
from bot.handlers.admin import get_admin_handlers
from bot.handlers.bridge import get_bridge_handler, get_bridge_callbacks
from bot.handlers.deposit import get_deposit_handlers
from bot.handlers.menu import get_menu_handlers
from bot.handlers.withdraw import get_withdraw_handler
from bot.handlers.analytics import get_analytics_handlers
from bot.handlers.group_setup import get_group_setup_handler
from bot.handlers.mygroup import get_mygroup_handlers
from bot.handlers.group_actions import get_group_action_handlers
from bot.services.monitor import MultiMasterMonitor
from bot.services.clob_ws_monitor import ClobWsMonitor, RawWsEvent
from bot.services.copytrade import CopyTradeEngine
from bot.services.rate_limiter import init_rate_limiter
from bot.services.scheduler import (
    reset_daily_limits,
    cleanup_expired_otps,
    health_check,
    settle_trades,
)

# V3 — Smart Analysis imports
from bot.services.topic_router import TopicRouter
from bot.services.signal_scorer import SignalScorer
from bot.services.trader_tracker import TraderTracker
from bot.services.market_intel import MarketIntelService
from bot.services.position_manager import PositionManager
from bot.services.portfolio_manager import PortfolioManager
from bot.services.smart_filter import SmartFilter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def build_application() -> Application:
    """Build and configure the Telegram bot application."""
    app = Application.builder().token(settings.telegram_token).build()

    # ── Group action interceptor (MUST be group=-1, before ConversationHandler) ──
    # Intercepts set_* / menu_* callbacks in group context:
    #   • Toggles → flip in DB + refresh topic menu
    #   • Value inputs / complex flows → redirect to DM
    # Raises ApplicationHandlerStop so ConversationHandler (group=0) never fires in groups.
    for handler in get_group_action_handlers():
        app.add_handler(handler, group=-1)

    app.add_handler(get_start_handler())
    app.add_handler(get_setup_group_handler())  # setup_my_group from any screen
    app.add_handler(get_settings_handler())
    app.add_handler(get_bridge_handler())
    for handler in get_bridge_callbacks():
        app.add_handler(handler)
    app.add_handler(get_withdraw_handler())
    for handler in get_deposit_handlers():
        app.add_handler(handler)

    for handler in get_menu_handlers():
        app.add_handler(handler)
    for handler in get_balance_handlers():
        app.add_handler(handler)
    for handler in get_control_handlers():
        app.add_handler(handler)
    for handler in get_admin_handlers():
        app.add_handler(handler)

    # V3 — Analytics handlers (/analytics, trader stats, portfolio, etc.)
    for handler in get_analytics_handlers():
        app.add_handler(handler)

    # V3 — Auto-setup: creates forum topics when bot is added as admin to a group
    app.add_handler(get_group_setup_handler())

    # /mygroup — show / manage user's linked group
    for handler in get_mygroup_handlers():
        app.add_handler(handler)

    return app


def setup_scheduler(
    monitor: MultiMasterMonitor,
    bot=None,
    trader_tracker: TraderTracker = None,
    position_manager: PositionManager = None,
    portfolio_manager: PortfolioManager = None,
    topic_router: TopicRouter = None,
) -> AsyncIOScheduler:
    """Configure periodic background tasks."""
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        reset_daily_limits,
        "cron", hour=0, minute=0,
        id="reset_daily_limits",
    )

    scheduler.add_job(
        cleanup_expired_otps,
        "interval", minutes=10,
        id="cleanup_otps",
    )

    scheduler.add_job(
        health_check,
        "interval", minutes=5,
        id="health_check",
    )

    # Settle resolved trades (paper + live) every 2 minutes
    # V3: passes topic_router + trader_tracker for routing + performance tracking
    scheduler.add_job(
        lambda: settle_trades(
            bot=bot,
            topic_router=topic_router,
            trader_tracker=trader_tracker,
        ),
        "interval", minutes=2,
        id="settle_trades",
    )

    # Refresh watched wallets every 60s so new follows are picked up quickly
    scheduler.add_job(
        monitor.refresh_watched_wallets,
        "interval", seconds=60,
        id="refresh_watched_wallets",
    )

    # ── V3 Scheduled Jobs ──────────────────────────────────────

    # Recalculate trader stats every 15 minutes
    if trader_tracker:
        async def refresh_all_trader_stats():
            """Recalculate stats for all watched wallets.

            On auto-pause, alert each subscriber who follows that trader
            via their own group (multi-tenant).
            """
            try:
                from bot.db.session import async_session as _as
                from bot.models.user import User
                from bot.services.topic_router import TopicRouter
                from bot.services.user_service import get_or_create_settings
                from sqlalchemy import select

                for wallet in monitor.watched_wallets:
                    await trader_tracker.recalculate_stats(wallet)
                    if await trader_tracker.check_auto_pause(wallet):
                        short = f"{wallet[:6]}...{wallet[-4:]}"
                        alert_text = (
                            f"⚠️ *Trader auto-pausé*\n\n"
                            f"`{short}` a été mis en pause automatiquement "
                            f"(win rate en dessous du seuil)."
                        )
                        # Alert each subscriber who follows this wallet
                        try:
                            async with _as() as session:
                                users = (await session.execute(select(User).where(
                                    User.is_active == True  # noqa: E712
                                ))).scalars().all()
                            for u in users:
                                try:
                                    us = None
                                    async with _as() as s2:
                                        us = await get_or_create_settings(s2, u)
                                    if us and wallet in (us.followed_wallets or []):
                                        ur = await TopicRouter.for_user(u.id, topic_router._bot)
                                        eff = ur or topic_router
                                        if eff:
                                            await eff.send_alert(alert_text)
                                except Exception:
                                    pass
                        except Exception:
                            # Fallback: send to global admin topic
                            if topic_router:
                                await topic_router.send_alert(alert_text)

                logger.info(
                    "Trader stats refreshed for %d wallets",
                    len(monitor.watched_wallets),
                )
            except Exception as e:
                logger.error("Trader stats refresh failed: %s", e)

        scheduler.add_job(
            refresh_all_trader_stats,
            "interval", minutes=15,
            id="refresh_trader_stats",
        )

    # Check time-based exits every 5 minutes
    if position_manager:
        async def check_time_exits():
            try:
                await position_manager.check_time_exits(time_exit_hours=24)
            except Exception as e:
                logger.error("Time exit check failed: %s", e)

        scheduler.add_job(
            check_time_exits,
            "interval", minutes=5,
            id="check_time_exits",
        )

    # Daily portfolio report at 8:00 UTC — sends to each user's own group
    if portfolio_manager and topic_router:
        async def daily_portfolio_report():
            try:
                from bot.db.session import async_session
                from bot.models.user import User
                from bot.services.topic_router import TopicRouter
                from sqlalchemy import select

                async with async_session() as session:
                    users = (
                        await session.execute(
                            select(User).where(User.is_active == True)  # noqa: E712
                        )
                    ).scalars().all()

                for user in users:
                    report = await portfolio_manager.format_portfolio_report(user.id)
                    # Use user's own group router if available, else global
                    user_router = await TopicRouter.for_user(user.id, topic_router._bot)
                    effective = user_router or topic_router
                    await effective.send_portfolio(report)
            except Exception as e:
                logger.error("Daily portfolio report failed: %s", e)

        scheduler.add_job(
            daily_portfolio_report,
            "cron", hour=8, minute=0,
            id="daily_portfolio_report",
        )

    return scheduler


async def main() -> None:
    """Initialize database and start the bot."""
    logger.info("Starting WENPOLYMARKET V3 Smart CopyTrading Bot...")

    await init_db()
    logger.info("Database initialized.")

    await init_rate_limiter(settings.redis_url)

    app = build_application()
    logger.info("Bot handlers registered.")

    # ── V3: Initialize Smart Analysis services ──────────────────
    from bot.services.polymarket import polymarket_client

    # Topic Router (Telegram group topics)
    # Loads from .env first, then tries DB (auto-setup config)
    topic_router = TopicRouter(bot=app.bot)
    await topic_router.try_load_from_db()  # Override .env with DB if available

    # Trader performance tracker
    trader_tracker = TraderTracker(topic_router=topic_router)

    # Market intelligence
    market_intel = MarketIntelService(polymarket_client=polymarket_client)

    # Position manager (active SL/TP enforcement)
    position_manager = PositionManager(
        polymarket_client=polymarket_client,
        topic_router=topic_router,
        check_interval=15,
    )

    # Portfolio manager (risk controls)
    portfolio_manager = PortfolioManager(
        position_manager=position_manager,
        market_intel_service=market_intel,
    )

    # Signal scorer
    signal_scorer = SignalScorer(
        polymarket_client=polymarket_client,
        trader_tracker=trader_tracker,
        market_intel_service=market_intel,
    )

    # Smart filter
    smart_filter = SmartFilter(
        market_intel_service=market_intel,
        trader_tracker=trader_tracker,
        polymarket_client=polymarket_client,
    )

    # Create CopyTradeEngine with all V3 services injected
    engine = CopyTradeEngine(
        telegram_bot=app.bot,
        signal_scorer=signal_scorer,
        smart_filter=smart_filter,
        portfolio_manager=portfolio_manager,
        position_manager=position_manager,
        trader_tracker=trader_tracker,
        topic_router=topic_router,
    )

    # Wire position manager exit callback to engine
    async def on_position_exit(user_id, position, reason):
        """Called by PositionManager when SL/TP triggers an exit."""
        logger.info(
            "Position exit callback: user=%d reason=%s market=%s",
            user_id, reason, position.market_id[:20],
        )
        # The actual sell is handled by PositionManager's alert
        # In a full implementation, this would create a SELL signal
        # and push it through the engine

    position_manager.set_exit_callback(on_position_exit)

    # Monitor Data API (positions) — poll every N seconds
    monitor = MultiMasterMonitor(
        poll_interval=settings.monitor_poll_interval,
        on_signal=engine.handle_signal,
    )

    # Give signal scorer access to monitor for consensus scoring
    signal_scorer._monitor = monitor

    # Callback WebSocket: on each CLOB trade, trigger immediate check
    async def handle_ws_event(evt: RawWsEvent) -> None:
        if evt.type in ("last_trade_price", "trade"):
            await monitor.fast_check_all_wallets()

    # Monitor WebSocket CLOB — real-time
    clob_ws_monitor = ClobWsMonitor(on_event=handle_ws_event)

    # Periodic job: sync WS subscriptions with tracked positions
    async def sync_ws_subscriptions():
        """Gather all token_ids from followed wallets' positions for WS."""
        try:
            token_ids: set[str] = set()
            for wallet in monitor.watched_wallets:
                positions = await polymarket_client.get_positions_by_address(wallet)
                for p in positions:
                    if p.token_id:
                        token_ids.add(p.token_id)
            if token_ids:
                await clob_ws_monitor.update_subscriptions(token_ids)
        except Exception as e:
            logger.warning(f"WS subscription sync failed: {e}")

    scheduler = setup_scheduler(
        monitor,
        bot=app.bot,
        trader_tracker=trader_tracker,
        position_manager=position_manager,
        portfolio_manager=portfolio_manager,
        topic_router=topic_router,
    )

    # Sync WS subscriptions every 2 minutes
    scheduler.add_job(
        sync_ws_subscriptions,
        "interval", seconds=120,
        id="sync_ws_subs",
    )

    scheduler.start()
    logger.info("Scheduler started.")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=["message", "callback_query", "my_chat_member", "chat_member"],
    )
    logger.info("Telegram bot polling started.")

    await monitor.start()
    logger.info(
        f"Multi-master monitor started "
        f"({len(monitor.watched_wallets)} wallet(s) watched)."
    )

    # Start WebSocket + initial token subscription
    await clob_ws_monitor.start()
    asyncio.create_task(sync_ws_subscriptions())

    # V3: Start position manager monitoring loop
    await position_manager.start()
    logger.info("Position manager started (checking every 15s).")

    # V3: Startup notification
    if topic_router.is_enabled:
        await topic_router.send_admin(
            "🟢 *Bot V3 Started*\n\n"
            f"Wallets watched: {len(monitor.watched_wallets)}\n"
            "Smart Analysis: ✅ Active\n"
            "Signal Scoring: ✅\n"
            "Trader Tracker: ✅\n"
            "Position Manager: ✅\n"
            "Portfolio Manager: ✅\n"
            "Smart Filter: ✅"
        )
    logger.info("V3 Smart Analysis services initialized.")

    # Start web dashboard (FastAPI) if enabled
    dashboard_server = None
    if settings.dashboard_enabled:
        import uvicorn
        from bot.web.app import app as dashboard_app

        config = uvicorn.Config(
            dashboard_app,
            host="0.0.0.0",
            port=settings.dashboard_port,
            log_level="warning",
        )
        dashboard_server = uvicorn.Server(config)
        asyncio.create_task(dashboard_server.serve())
        logger.info(f"Dashboard started on http://0.0.0.0:{settings.dashboard_port}")

    logger.info("Bot V3 is fully running. Press Ctrl+C to stop.")

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down...")
        if dashboard_server:
            dashboard_server.should_exit = True
        scheduler.shutdown(wait=False)
        await position_manager.stop()
        await monitor.stop()
        await clob_ws_monitor.stop()
        await polymarket_client.close()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
