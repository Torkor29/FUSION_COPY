"""Bridge handler — /bridge command for SOL → USDC Polygon bridging.

Flux simplifié : plus de devis on-chain.
Le bot :
- rappelle sur quel wallet Polygon les USDC vont arriver,
- explique comment utiliser un bridge externe (Jumper Exchange),
- envoie l'adresse Polygon dans un message séparé pour faciliter le copier/coller.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id

logger = logging.getLogger(__name__)


async def bridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Simplified bridge flow — no quote, direct redirect to Jumper.

    Fonctionne à la fois via /bridge et via le bouton « 🌉 Bridge » du menu.
    """
    tg_user = update.effective_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user:
            if update.message:
                await update.message.reply_text("❌ Compte non trouvé. /start")
            else:
                query = update.callback_query
                if query:
                    await query.answer()
                    await query.edit_message_text("❌ Compte non trouvé. /start")
            return

        poly_wallet = user.wallet_address or ""
        auto_created = user.wallet_auto_created

    if not poly_wallet:
        if update.message:
            await update.message.reply_text(
                "❌ Wallet Polygon non configuré. Utilisez /start pour terminer l'inscription."
            )
        else:
            query = update.callback_query
            if query:
                await query.answer()
                await query.edit_message_text(
                    "❌ Wallet Polygon non configuré. Utilisez /start pour terminer l'inscription."
                )
        return

    if auto_created:
        wallet_note = (
            "📬 Les USDC arriveront sur le **wallet créé par le bot**.\n"
            "Vous n'avez pas besoin de MetaMask sur Polygon."
        )
    else:
        wallet_note = (
            "📬 Les USDC arriveront sur **votre wallet importé**.\n"
            "C'est le même que vous utilisez déjà (ex : MetaMask)."
        )

    jumper_url = "https://jumper.exchange/"

    keyboard = [
        [
            InlineKeyboardButton("🔗 Ouvrir Jumper Exchange", url=jumper_url),
        ],
        [
            InlineKeyboardButton(
                "📋 Copier adresse Polygon", callback_data="bridge_copy_poly"
            ),
        ],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    # On répond soit à la commande, soit on remplace le message du menu
    if update.message:
        target = update.message
    else:
        query = update.callback_query
        await query.answer()
        target = query.message

    await target.reply_text(
        "🌉 **Bridge SOL → USDC (Polygon)**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{wallet_note}\n\n"
        "📝 **Comment faire avec Phantom (mobile)**\n\n"
        "1. Ouvrez l'app **Phantom** sur votre téléphone\n"
        "2. Allez dans l'onglet **Navigateur / Browser** de Phantom\n"
        "3. Dans la barre d'adresse de Phantom, collez : `https://jumper.exchange`\n"
        "   ⚠️ Si vous ouvrez le lien depuis Telegram/Safari/Chrome, Phantom ne\n"
        "   sera pas proposé : il faut l'ouvrir **depuis Phantom lui-même**.\n"
        "4. Une fois Jumper ouvert dans Phantom, connectez votre wallet Solana\n\n"
        "💡 Alternative si vous ne trouvez pas Jumper :\n"
        "   • Dans Phantom, ouvrez la section **Bridge / Swap** intégrée\n"
        "   • Choisissez **SOL (Solana)** → **USDC sur Polygon**\n"
        "   • Quand Phantom demande l'adresse de destination sur Polygon,\n"
        "     collez l'adresse envoyée dans le message séparé ci-dessous\n\n"
        "🖥️ **Depuis un ordinateur** :\n"
        "1. Ouvrez `https://jumper.exchange` dans un navigateur où votre wallet\n"
        "   (Phantom extension ou autre) est installé\n"
        "2. Connectez votre wallet Solana\n\n"
        "Dans tous les cas :\n"
        "3. Sélectionnez : **SOL (Solana)** → **USDC (Polygon)**\n"
        "4. Quand le site ou Phantom demande une **adresse de destination Polygon**,\n"
        "   collez l'adresse envoyée dans le message séparé ci-dessous\n"
        "5. Entrez le montant à bridger et **signez depuis votre wallet**\n"
        "6. Les USDC arrivent en quelques minutes sur votre wallet Polygon\n\n"
        + (
            "💡 *Wallet créé par le bot : Phantom signe le départ, le bridge "
            "envoie directement les USDC sur le wallet du bot.*"
            if auto_created
            else "💡 *Wallet importé : les USDC arriveront sur le même wallet "
            "Polygon que vous voyez dans MetaMask / Polymarket.*"
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    # Adresse Polygon en message séparé pour faciliter le copier/coller
    await target.reply_text(
        f"📬 **Adresse Polygon de destination :**\n`{poly_wallet}`",
        parse_mode="Markdown",
    )


async def bridge_copy_poly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the Polygon wallet address as a separate copyable message."""
    query = update.callback_query
    await query.answer()

    tg_user = update.effective_user
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if user and user.wallet_address:
            await query.message.reply_text(
                f"`{user.wallet_address}`",
                parse_mode="Markdown",
            )
        else:
            await query.answer("Wallet Polygon non configuré", show_alert=True)


async def bridge_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel bridge (simply closes the helper message)."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("❌ Bridge annulé.")


def get_bridge_handler() -> CommandHandler:
    """Build the /bridge handler (simple command, no conversation)."""
    return CommandHandler("bridge", bridge_command)


def get_bridge_callbacks() -> list:
    """Extra callback handlers used outside the conversation (after END)."""
    return [
        CallbackQueryHandler(bridge_copy_poly, pattern="^bridge_copy_poly$"),
        CallbackQueryHandler(bridge_cancel, pattern="^bridge_cancel$"),
    ]
