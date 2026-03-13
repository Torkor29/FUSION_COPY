"""Control handlers — /pause, /resume, /stop, /help commands."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id

logger = logging.getLogger(__name__)


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause copytrading."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("❌ Compte non trouvé. /start")
            return

        if user.is_paused:
            keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
            await update.message.reply_text(
                "⏸️ Le copytrading est déjà en pause.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        user.is_paused = True
        await session.commit()

    keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
    await update.message.reply_text(
        "⏸️ **Copytrading mis en pause**\n\n"
        "Les trades du master ne seront plus copiés.\n"
        "Vos positions ouvertes restent actives.\n\n"
        "Utilisez /resume pour reprendre.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume copytrading."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("❌ Compte non trouvé. /start")
            return

        if not user.is_paused:
            keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
            await update.message.reply_text(
                "▶️ Le copytrading est déjà actif.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        user.is_paused = False
        await session.commit()

    keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
    await update.message.reply_text(
        "▶️ **Copytrading repris !**\n\n"
        "Les prochains trades du master seront copiés automatiquement.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop copytrading — ask for confirmation."""
    keyboard = [
        [
            InlineKeyboardButton("🛑 Confirmer l'arrêt", callback_data="stop_confirm"),
            InlineKeyboardButton("❌ Annuler", callback_data="stop_cancel"),
        ]
    ]
    await update.message.reply_text(
        "🛑 **Arrêter le copytrading ?**\n\n"
        "Cela va :\n"
        "• Désactiver la copie automatique\n"
        "• Vos positions ouvertes resteront actives\n\n"
        "⚠️ Pour fermer vos positions, faites-le manuellement sur Polymarket.\n\n"
        "Confirmer ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def stop_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm stop."""
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if user:
            user.is_active = False
            user.is_paused = True
            await session.commit()

    keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
    await query.edit_message_text(
        "🛑 **Copytrading arrêté.**\n\n"
        "Votre compte est désactivé. Utilisez /start pour réactiver.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def stop_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel stop."""
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
    await query.edit_message_text(
        "✅ Arrêt annulé. Le copytrading continue.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help."""
    keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
    await update.message.reply_text(
        "❓ **AIDE — WENPOLYMARKET**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "**Menu principal** (le plus simple) :\n"
        "Tapez /start puis cliquez sur « Accéder au menu principal ».\n"
        "Tout est accessible depuis les boutons du menu.\n\n"
        "**Commandes rapides :**\n"
        "⏸️ /pause — Mettre le copy-trading en pause\n"
        "▶️ /resume — Reprendre le copy-trading\n"
        "🛑 /stop — Arrêter complètement\n"
        "📈 /stats — Vos statistiques\n\n"
        "**Comment ça marche :**\n"
        "1. Configurez un wallet Polygon (menu)\n"
        "2. Déposez des USDC dessus\n"
        "3. Choisissez vos traders dans Paramètres\n"
        "4. Les trades sont copiés automatiquement\n"
        "5. Frais : 1% par trade copié\n\n"
        "🔒 Clés chiffrées AES-256 • Jamais exposées en clair\n"
        "📝 Paper Trading activé par défaut",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user performance stats."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("❌ Compte non trouvé. /start")
            return

        from sqlalchemy import func, select
        from bot.models.trade import Trade, TradeStatus
        from bot.models.fee import FeeRecord

        total_trades = await session.scalar(
            select(func.count(Trade.id)).where(Trade.user_id == user.id)
        ) or 0

        filled_trades = await session.scalar(
            select(func.count(Trade.id)).where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
            )
        ) or 0

        total_volume = await session.scalar(
            select(func.sum(Trade.gross_amount_usdc)).where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
            )
        ) or 0.0

        total_fees = await session.scalar(
            select(func.sum(FeeRecord.fee_amount)).where(
                FeeRecord.user_id == user.id,
            )
        ) or 0.0

        # Win rate: compare BUY avg price vs current SELL prices for same market
        # Simple approach: count SELL trades where price > avg BUY price
        from bot.models.trade import TradeSide
        buy_trades = (await session.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
                Trade.side == TradeSide.BUY,
            )
        )).scalars().all()

        sell_trades = (await session.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
                Trade.side == TradeSide.SELL,
            )
        )).scalars().all()

        # P&L estimation: for sells, profit = (sell_price - avg_buy_price) * shares
        total_pnl = 0.0
        wins = 0
        total_closed = 0
        # Build avg buy price per market
        buy_avg: dict[str, float] = {}
        for t in buy_trades:
            key = t.token_id
            if key not in buy_avg:
                buy_avg[key] = t.price
            else:
                buy_avg[key] = (buy_avg[key] + t.price) / 2

        for t in sell_trades:
            key = t.token_id
            avg_buy = buy_avg.get(key)
            if avg_buy is not None and avg_buy > 0:
                pnl = (t.price - avg_buy) * t.shares
                total_pnl += pnl
                total_closed += 1
                if pnl > 0:
                    wins += 1

        if total_closed > 0:
            win_rate = f"{(wins / total_closed) * 100:.0f}%"
            pnl_str = f"{total_pnl:+.2f} USDC"
        else:
            win_rate = "N/A"
            pnl_str = "N/A"

    keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
    await update.message.reply_text(
        "📈 **VOS STATISTIQUES**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔄 Total trades     : **{total_trades}**\n"
        f"✅ Trades exécutés  : **{filled_trades}**\n"
        f"💰 Volume total     : **{total_volume:.2f} USDC**\n"
        f"💸 Frais payés      : **{total_fees:.2f} USDC**\n"
        f"📊 Win rate         : **{win_rate}**\n"
        f"📈 P&L estimé       : **{pnl_str}**\n"
        f"📝 Mode             : **{'Paper' if user.paper_trading else 'Réel'}**",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def get_control_handlers() -> list:
    """Return all control command handlers."""
    return [
        CommandHandler("pause", pause_command),
        CommandHandler("resume", resume_command),
        CommandHandler("stop", stop_command),
        CallbackQueryHandler(stop_confirm, pattern="^stop_confirm$"),
        CallbackQueryHandler(stop_cancel, pattern="^stop_cancel$"),
        CommandHandler("help", help_command),
        CommandHandler("stats", stats_command),
    ]
