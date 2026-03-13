"""Main entry point for the WENPOLYMARKET copytrading bot."""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

from bot.config import settings
from bot.db.session import init_db
from bot.handlers.start import get_start_handler
from bot.handlers.settings import get_settings_handler
from bot.handlers.balance import get_balance_handlers
from bot.handlers.controls import get_control_handlers
from bot.handlers.admin import get_admin_handlers
from bot.handlers.bridge import get_bridge_handler, get_bridge_callbacks
from bot.handlers.deposit import get_deposit_handlers
from bot.handlers.menu import get_menu_handlers
from bot.handlers.withdraw import get_withdraw_handler
from bot.services.monitor import MultiMasterMonitor
from bot.services.clob_ws_monitor import ClobWsMonitor, RawWsEvent
from bot.services.copytrade import CopyTradeEngine
from bot.services.rate_limiter import init_rate_limiter
from bot.services.scheduler import (
    reset_daily_limits,
    cleanup_expired_otps,
    health_check,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def build_application() -> Application:
    """Build and configure the Telegram bot application."""
    app = Application.builder().token(settings.telegram_token).build()

    app.add_handler(get_start_handler())
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

    return app


def setup_scheduler(monitor: MultiMasterMonitor) -> AsyncIOScheduler:
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

    # Refresh watched wallets every 60s so new follows are picked up quickly
    scheduler.add_job(
        monitor.refresh_watched_wallets,
        "interval", seconds=60,
        id="refresh_watched_wallets",
    )

    return scheduler


async def main() -> None:
    """Initialize database and start the bot."""
    logger.info("Starting WENPOLYMARKET CopyTrading Bot...")

    await init_db()
    logger.info("Database initialized.")

    await init_rate_limiter(settings.redis_url)

    app = build_application()
    logger.info("Bot handlers registered.")

    engine = CopyTradeEngine(telegram_bot=app.bot)

    # Monitor Gamma (positions) — conservé comme source principale
    monitor = MultiMasterMonitor(
        poll_interval=settings.monitor_poll_interval,
        on_signal=engine.handle_signal,
    )

    # Callback WebSocket : sur chaque trade CLOB, on déclenche un check Gamma
    # immédiat pour réduire la latence de détection des mouvements des masters.

    async def handle_ws_event(evt: RawWsEvent) -> None:
        if evt.type == "last_trade_price":
            await monitor.fast_check_all_wallets()

    # Monitor WebSocket CLOB — fondation pour le temps réel
    clob_ws_monitor = ClobWsMonitor(on_event=handle_ws_event)

    scheduler = setup_scheduler(monitor)
    scheduler.start()
    logger.info("Scheduler started.")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Telegram bot polling started.")

    await monitor.start()
    logger.info(
        f"Multi-master monitor started "
        f"({len(monitor.watched_wallets)} wallet(s) watched)."
    )

    # Démarrer le monitor WebSocket en parallèle (ne génère pour l'instant
    # que des logs, sans impacter le moteur de copie).
    await clob_ws_monitor.start()

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

    logger.info("Bot is fully running. Press Ctrl+C to stop.")

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
        await monitor.stop()
        await clob_ws_monitor.stop()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
