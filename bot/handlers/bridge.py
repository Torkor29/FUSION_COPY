"""Bridge handler — /bridge command for SOL → USDC Polygon bridging.

Scénario A : le SOL reste sur le wallet Solana de l'utilisateur.
Le bot ne signe aucune transaction Solana. Il :
- enregistre l'adresse Solana de l'utilisateur,
- récupère un devis SOL → USDC (Polygon),
- explique comment exécuter le bridge via une interface externe (Li.Fi, etc.),
- rappelle l'adresse Polygon cible pour les USDC.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id
from bot.services.bridge import get_best_quote, BridgeProvider
from bot.config import settings

logger = logging.getLogger(__name__)

SOL_ADDRESS, AMOUNT_INPUT = range(2)


async def bridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start bridge flow — /bridge."""
    tg_user = update.effective_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user:
            await update.message.reply_text("❌ Compte non trouvé. /start")
            return ConversationHandler.END

        sol_wallet = user.solana_wallet_address

    if not sol_wallet:
        await update.message.reply_text(
            "☀️ **Bridge SOL → USDC (Polygon)**\n\n"
            "Pour commencer, envoyez votre **adresse Solana** (celle où vous avez vos SOL).\n\n"
            "Exemple : `5F2h...xyz`.\n\n"
            "Le bot utilisera cette adresse uniquement pour calculer des devis et "
            "vous aider à configurer le bridge. Le SOL restera toujours sur votre propre wallet.",
            parse_mode="Markdown",
        )
        return SOL_ADDRESS

    await update.message.reply_text(
        "🌉 **Bridge SOL → USDC Polygon**\n\n"
        "Combien de SOL voulez-vous bridger ?\n\n"
        "Envoyez le montant (ex: `1.5`) :",
        parse_mode="Markdown",
    )
    return AMOUNT_INPUT


async def receive_solana_address(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store user's Solana address and ask for amount."""
    address = update.message.text.strip()

    # Validation très basique : longueur raisonnable, pas vide
    if len(address) < 20 or len(address) > 60:
        await update.message.reply_text(
            "❌ Adresse Solana invalide. Vérifiez et renvoyez-la."
        )
        return SOL_ADDRESS

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("❌ Compte non trouvé. /start")
            return ConversationHandler.END

        user.solana_wallet_address = address
        await session.commit()

    await update.message.reply_text(
        "✅ Adresse Solana enregistrée.\n\n"
        "Maintenant, envoyez le **montant de SOL** que vous souhaitez bridger "
        "(ex: `1.5`).",
        parse_mode="Markdown",
    )
    return AMOUNT_INPUT


async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive SOL amount and get quote."""
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Montant invalide. Envoyez un nombre (ex: `1.5`).")
        return AMOUNT_INPUT

    if amount <= 0 or amount > 1000:
        await update.message.reply_text("❌ Montant entre 0.01 et 1000 SOL.")
        return AMOUNT_INPUT

    # Show typing indicator
    await update.effective_chat.send_action("typing")

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("❌ Compte non trouvé. /start")
            return ConversationHandler.END

        sol_wallet = user.solana_wallet_address
        poly_wallet = user.wallet_address or ""

    if not sol_wallet:
        await update.message.reply_text(
            "❌ Adresse Solana manquante. Relancez /bridge.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Get best quote
    quote = await get_best_quote(amount, sol_wallet, poly_wallet)

    if not quote:
        await update.message.reply_text(
            "❌ **Impossible d'obtenir un devis.**\n\n"
            "Les providers de bridge sont indisponibles. Réessayez plus tard.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    provider_name = "Li.Fi" if quote.provider == BridgeProvider.LIFI else "Across"
    lifi_url = "https://li.quest/"

    keyboard = [
        [
            InlineKeyboardButton("🔗 Ouvrir Li.Fi (bridge)", url=lifi_url),
        ],
        [
            InlineKeyboardButton("❌ Fermer", callback_data="bridge_cancel"),
        ],
    ]

    poly_display = poly_wallet or "votre wallet Polygon (celui utilisé par le bot)"

    await update.message.reply_text(
        "🌉 **Devis Bridge SOL → USDC (Polygon)**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"☀️ Envoi      : **{amount:.4f} SOL** (depuis votre wallet Solana)\n"
        f"💵 Réception  : **~{quote.output_amount:.2f} USDC** sur Polygon\n"
        f"🔄 Provider   : **{provider_name}**\n"
        f"💸 Frais      : **~{quote.fee_usd:.2f} USD**\n"
        f"⏱️ Estimé     : **~{quote.estimated_time_seconds // 60} min**\n"
        f"📊 Slippage   : **{settings.bridge_slippage * 100:.1f}%**\n\n"
        f"📬 Adresse Polygon cible : `{poly_display}`\n\n"
        "➡️ Étapes recommandées :\n"
        "1. Cliquez sur **\"Ouvrir Li.Fi (bridge)\"**.\n"
        "2. Connectez **votre wallet Solana** (celui dont vous avez l'adresse ci‑dessus).\n"
        "3. Configurez le bridge vers **USDC sur Polygon** avec l'adresse cible affichée.\n"
        "4. Validez la transaction depuis votre wallet.\n\n"
        "Le bot ne signe aucune transaction Solana : le SOL reste toujours sur votre propre wallet.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def bridge_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel bridge."""
    query = update.callback_query
    await query.answer()

    context.user_data.pop("bridge_quote", None)
    context.user_data.pop("bridge_amount", None)

    await query.edit_message_text("❌ Bridge annulé.")
    return ConversationHandler.END


def get_bridge_handler() -> ConversationHandler:
    """Build the /bridge conversation handler."""
    return ConversationHandler(
        entry_points=[CommandHandler("bridge", bridge_command)],
        states={
            SOL_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_solana_address),
            ],
            AMOUNT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount),
            ],
        },
        fallbacks=[CommandHandler("bridge", bridge_command)],
        per_user=True,
    )
