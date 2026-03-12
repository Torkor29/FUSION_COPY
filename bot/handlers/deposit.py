"""Deposit helper — /deposit with context-aware guides for bot-created and imported wallets."""

import logging
import urllib.parse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from bot.config import settings
from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id

logger = logging.getLogger(__name__)

MOONPAY_BUY_URL = "https://www.moonpay.com/buy/usdc-polygon"
TRANSAK_BASE_URL = "https://global.transak.com/"


def _build_transak_url(wallet_address: str) -> str | None:
    if not settings.transak_api_key:
        return None
    params = urllib.parse.urlencode({
        "apiKey": settings.transak_api_key,
        "cryptoCurrencyCode": "USDC",
        "network": "polygon",
        "walletAddress": wallet_address,
        "disableWalletAddressForm": "true",
        "defaultPaymentMethod": "credit_debit_card",
    })
    return f"{TRANSAK_BASE_URL}?{params}"


async def deposit_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show deposit options adapted to the user's wallet type."""
    tg_user = update.effective_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user or not user.wallet_address:
            await update.message.reply_text(
                "❌ Wallet non configuré. Utilisez /start pour vous inscrire."
            )
            return

        wallet = user.wallet_address
        auto = user.wallet_auto_created

    if auto:
        intro = (
            "💰 **DÉPOSER DES USDC**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🏷️ *Wallet créé par le bot* — il est vide au départ.\n"
            "Vous devez y envoyer des USDC pour pouvoir copier des trades.\n\n"
            f"📬 Votre adresse Polygon :\n`{wallet}`\n\n"
            "Comment souhaitez-vous ajouter des fonds ?"
        )
    else:
        intro = (
            "💰 **DÉPOSER DES USDC**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🏷️ *Wallet importé* — vos USDC existants sont déjà utilisables.\n"
            "Besoin de fonds supplémentaires ? Voici vos options.\n\n"
            f"📬 Votre adresse Polygon :\n`{wallet}`\n\n"
            "Comment souhaitez-vous ajouter des fonds ?"
        )

    keyboard = [
        [InlineKeyboardButton(
            "💳 Acheter par carte bancaire", callback_data="dep_card"
        )],
        [InlineKeyboardButton(
            "🏦 Envoyer depuis un exchange", callback_data="dep_exchange"
        )],
        [InlineKeyboardButton(
            "🌉 Bridge (SOL, ETH → Polygon)", callback_data="dep_bridge"
        )],
        [InlineKeyboardButton(
            "📋 Copier mon adresse Polygon", callback_data="deposit_copy_address"
        )],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    await update.message.reply_text(
        intro,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _deposit_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        wallet = user.wallet_address if user else ""

    transak_url = _build_transak_url(wallet) if wallet else None

    text = (
        "💳 **ACHETER USDC PAR CARTE BANCAIRE**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Le plus simple si vous n'avez pas encore de crypto.\n\n"
        "**Comment faire :**\n"
        "1. Cliquez sur un lien ci-dessous\n"
        "2. Sélectionnez **USDC** sur le réseau **Polygon**\n"
        "3. Comme adresse de réception, collez :\n"
        f"   `{wallet}`\n"
        "4. Payez par carte — USDC reçus en ~5 min\n\n"
        "💡 *Frais : 2-4% selon le service et votre pays.*\n\n"
        "⚠️ Pensez aussi à acheter ~0.5 POL/MATIC pour les frais "
        "de gas (quelques centimes suffisent). Certains services "
        "permettent d'en acheter directement."
    )

    buttons = []
    if transak_url:
        buttons.append([InlineKeyboardButton(
            "🟢 Transak (adresse pré-remplie)", url=transak_url
        )])
    buttons.append([InlineKeyboardButton("🌙 MoonPay", url=MOONPAY_BUY_URL)])
    buttons.append([InlineKeyboardButton(
        "📋 Copier mon adresse", callback_data="deposit_copy_address"
    )])
    buttons.append([InlineKeyboardButton("⬅️ Retour", callback_data="dep_back")])

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def _deposit_exchange(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        wallet = user.wallet_address if user else ""

    text = (
        "🏦 **ENVOYER DEPUIS UN EXCHANGE**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Vous avez un compte Binance, Coinbase, OKX, Bybit… ?\n\n"
        "**Étapes :**\n"
        "1. Achetez des **USDC** sur l'exchange (carte, virement…)\n"
        "2. Allez dans **Retrait / Withdraw**\n"
        "3. Sélectionnez **USDC**\n"
        "4. Collez cette adresse comme destination :\n"
        f"   `{wallet}`\n"
        "5. **IMPORTANT** — réseau : **Polygon**\n"
        "   ⚠️ Pas Ethereum, pas Arbitrum → **Polygon** uniquement !\n"
        "6. Confirmez le retrait\n\n"
        "⏱️ ~2-5 min • Frais : ~0.1 USDC\n\n"
        "💡 *Envoyez aussi ~0.2 POL/MATIC pour le gas.*"
    )

    buttons = [
        [InlineKeyboardButton(
            "📋 Copier mon adresse", callback_data="deposit_copy_address"
        )],
        [InlineKeyboardButton("⬅️ Retour", callback_data="dep_back")],
    ]

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def _deposit_bridge(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        wallet = user.wallet_address if user else ""
        auto = user.wallet_auto_created if user else False

    if auto:
        wallet_note = (
            "📬 Les fonds arriveront sur **le wallet créé par le bot** :\n"
            f"`{wallet}`\n"
            "Vous n'avez pas besoin de MetaMask ni d'un wallet Polygon séparé."
        )
    else:
        wallet_note = (
            "📬 Les fonds arriveront sur **votre wallet importé** :\n"
            f"`{wallet}`\n"
            "C'est le même que celui que vous utilisez déjà (ex: MetaMask)."
        )

    text = (
        "🌉 **BRIDGE — CONVERTIR ET ENVOYER SUR POLYGON**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Vous avez de la crypto sur une autre blockchain ?\n"
        "Un bridge la convertit en USDC Polygon en une étape.\n\n"
        "**Ça marche dans les deux cas** :\n"
        "• Wallet créé par le bot → le bridge envoie les USDC dessus\n"
        "• Wallet importé (MetaMask, etc.) → même chose\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{wallet_note}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "**Scénarios courants :**\n\n"
        "☀️ **J'ai du SOL sur Phantom (mobile)**\n"
        "→ Cliquez sur « ☀️ Ouvrir le guide Bridge » ci-dessous\n"
        "→ Suivez les étapes pour ouvrir Jumper **depuis le navigateur intégré "
        "de Phantom** (ou utilisez la section Bridge de Phantom)\n"
        "→ Phantom signe la transaction Solana\n"
        "→ Le bridge envoie les USDC à l'adresse Polygon ci-dessus\n\n"
        "🔷 **J'ai de l'ETH ou de l'USDC sur Ethereum/Arbitrum**\n"
        "→ Ouvrez Jumper Exchange (lien ci-dessous)\n"
        "→ Connectez votre wallet (MetaMask, etc.)\n"
        "→ Bridgez vers USDC Polygon\n"
        "→ Mettez l'adresse ci-dessus comme destination\n\n"
        "💡 *Tous les bridges permettent d'indiquer une adresse de "
        "destination différente du wallet source. C'est comme ça qu'on "
        "envoie d'une chain à l'autre sans avoir besoin d'un wallet "
        "sur la chain d'arrivée.*"
    )

    buttons = [
        [InlineKeyboardButton("☀️ Ouvrir le guide Bridge", callback_data="dep_go_bridge")],
        [InlineKeyboardButton(
            "🔗 Jumper Exchange (ETH/autre)", url="https://jumper.exchange"
        )],
        [InlineKeyboardButton(
            "📋 Copier adresse Polygon", callback_data="deposit_copy_address"
        )],
        [InlineKeyboardButton("⬅️ Retour", callback_data="dep_back")],
    ]

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def _deposit_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        wallet = user.wallet_address if user else ""
        auto = user.wallet_auto_created if user else False

    if auto:
        tag = "🏷️ *Wallet créé par le bot*"
    else:
        tag = "🏷️ *Wallet importé*"

    text = (
        "💰 **DÉPOSER DES USDC**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{tag}\n"
        f"📬 `{wallet}`\n\n"
        "Comment souhaitez-vous ajouter des fonds ?"
    )

    keyboard = [
        [InlineKeyboardButton(
            "💳 Acheter par carte bancaire", callback_data="dep_card"
        )],
        [InlineKeyboardButton(
            "🏦 Envoyer depuis un exchange", callback_data="dep_exchange"
        )],
        [InlineKeyboardButton(
            "🌉 Bridge (SOL, ETH → Polygon)", callback_data="dep_bridge"
        )],
        [InlineKeyboardButton(
            "📋 Copier mon adresse Polygon", callback_data="deposit_copy_address"
        )],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _deposit_copy_address(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if user and user.wallet_address:
            await query.message.reply_text(
                f"`{user.wallet_address}`", parse_mode="Markdown"
            )
        else:
            await query.answer("Wallet non configuré", show_alert=True)


async def _deposit_go_bridge(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Ouvrir directement le guide / flux bridge depuis le menu dépôt."""
    from bot.handlers.bridge import bridge_command

    await bridge_command(update, context)


def get_deposit_handlers() -> list:
    return [
        CommandHandler("deposit", deposit_command),
        CallbackQueryHandler(_deposit_card, pattern="^dep_card$"),
        CallbackQueryHandler(_deposit_exchange, pattern="^dep_exchange$"),
        CallbackQueryHandler(_deposit_bridge, pattern="^dep_bridge$"),
        CallbackQueryHandler(_deposit_go_bridge, pattern="^dep_go_bridge$"),
        CallbackQueryHandler(_deposit_back, pattern="^dep_back$"),
        CallbackQueryHandler(
            _deposit_copy_address, pattern="^deposit_copy_address$"
        ),
    ]
