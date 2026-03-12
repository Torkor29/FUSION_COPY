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
            await update.message.reply_text("⏸️ Le copytrading est déjà en pause.")
            return

        user.is_paused = True
        await session.commit()

    await update.message.reply_text(
        "⏸️ **Copytrading mis en pause**\n\n"
        "Les trades du master ne seront plus copiés.\n"
        "Vos positions ouvertes restent actives.\n\n"
        "Utilisez /resume pour reprendre.",
        parse_mode="Markdown",
    )


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume copytrading."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("❌ Compte non trouvé. /start")
            return

        if not user.is_paused:
            await update.message.reply_text("▶️ Le copytrading est déjà actif.")
            return

        user.is_paused = False
        await session.commit()

    await update.message.reply_text(
        "▶️ **Copytrading repris !**\n\n"
        "Les prochains trades du master seront copiés automatiquement.",
        parse_mode="Markdown",
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

    await query.edit_message_text(
        "🛑 **Copytrading arrêté.**\n\n"
        "Votre compte est désactivé. Utilisez /start pour réactiver.",
        parse_mode="Markdown",
    )


async def stop_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel stop."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Arrêt annulé. Le copytrading continue.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help."""
    await update.message.reply_text(
        "📖 **AIDE — Polymarket CopyTrader**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "**Commandes disponibles :**\n\n"
        "🚀 /start — Inscription / statut (ou utilisez le menu principal)\n"
        "⚙️ Paramètres de copie : bouton « ⚙️ Paramètres » dans le menu\n"
        "💰 /balance — Soldes et wallet\n"
        "📊 /positions — Positions ouvertes\n"
        "📜 /history — Historique des trades\n"
        "🌉 Bridge SOL → USDC Polygon : bouton « 🌉 Bridge » dans le menu\n"
        "📈 /stats — Statistiques de performance\n"
        "⏸️ /pause — Mettre en pause\n"
        "▶️ /resume — Reprendre\n"
        "🛑 /stop — Arrêter le copytrading\n"
        "📖 /help — Cette aide\n\n"
        "**Comment ça marche :**\n"
        "1. Le master trader passe un trade sur Polymarket\n"
        "2. Le bot détecte et copie automatiquement\n"
        "3. La taille est adaptée selon vos paramètres\n"
        "4. Un frais de 1% est prélevé sur chaque trade\n"
        "5. Vous recevez une notification instantanée\n\n"
        "🔒 **Sécurité :** Clés chiffrées AES-256, jamais exposées\n"
        "📝 **Paper Trading :** Activé par défaut (pas de fonds réels)",
        parse_mode="Markdown",
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user performance stats."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("❌ Compte non trouvé. /start")
            return

        from sqlalchemy import func
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

    win_rate = "N/A"  # TODO: calculate from resolved markets

    await update.message.reply_text(
        "📈 **VOS STATISTIQUES**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔄 Total trades     : **{total_trades}**\n"
        f"✅ Trades exécutés  : **{filled_trades}**\n"
        f"💰 Volume total     : **{total_volume:.2f} USDC**\n"
        f"💸 Frais payés      : **{total_fees:.2f} USDC**\n"
        f"📊 Win rate         : **{win_rate}**\n"
        f"📝 Mode             : **{'Paper' if user.paper_trading else 'Réel'}**",
        parse_mode="Markdown",
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
