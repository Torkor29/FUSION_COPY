"""Withdraw handler — /withdraw to send USDC from bot wallet to any address."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.config import settings
from bot.db.session import async_session
from bot.services.crypto import decrypt_private_key
from bot.services.user_service import get_user_by_telegram_id
from bot.services.web3_client import polygon_client

logger = logging.getLogger(__name__)

DEST_ADDRESS, AMOUNT, CONFIRM = range(3)


async def withdraw_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Entry point — /withdraw.

    Fonctionne à la fois via la commande texte et le bouton « 💸 Retirer ».
    """
    tg_user = update.effective_user

    # On unifie la façon de répondre (message direct ou callback du menu)
    if update.message:
        # Appel via /withdraw
        message = update.message
        query = None
    else:
        # Appel via bouton « 💸 Retirer » (callback menu_withdraw)
        query = update.callback_query
        if query:
            await query.answer()
            message = query.message
        else:
            return ConversationHandler.END

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user or not user.wallet_address:
            await message.reply_text("❌ Wallet non configuré. Utilisez /start d'abord.")
            return ConversationHandler.END

        usdc_native, usdc_e = await polygon_client.get_usdc_balances(
            user.wallet_address
        )
        matic = await polygon_client.get_matic_balance(user.wallet_address)

    if usdc_native < 0.01:
        keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
        await message.reply_text(
            "💸 **Retrait USDC**\n\n"
            f"Votre solde USDC natif : **{usdc_native:.2f}**\n"
            f"Solde USDC.e (non utilisable) : **{usdc_e:.2f}**\n\n"
            "Rien à retirer pour le moment. Swappez d'abord vos USDC.e en USDC natif sur Polygon.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ConversationHandler.END

    context.user_data["withdraw_balance"] = usdc_native
    context.user_data["withdraw_matic"] = matic

    keyboard = [
        [InlineKeyboardButton("❌ Annuler", callback_data="withdraw_cancel")]
    ]

    await message.reply_text(
        "💸 **RETRAIT USDC**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Solde disponible (USDC natif) : **{usdc_native:.2f} USDC**\n"
        f"💵 USDC.e (non utilisable) : **{usdc_e:.2f}**\n"
        f"⛽ Gas disponible : **{matic:.4f} POL**\n\n"
        "Envoyez l'**adresse de destination** (0x…) où vous voulez "
        "recevoir vos USDC.\n\n"
        "💡 Ça peut être :\n"
        "• Votre adresse de dépôt **Polygon** sur un exchange (Binance, etc.)\n"
        "• Un autre wallet Polygon (MetaMask, etc.)\n\n"
        "⚠️ L'adresse doit être sur le **réseau Polygon**.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return DEST_ADDRESS


async def receive_dest_address(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Validate and store destination address."""
    address = update.message.text.strip()

    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text(
            "❌ Adresse invalide. Elle doit commencer par `0x` "
            "et faire 42 caractères.\n\nRéessayez :",
            parse_mode="Markdown",
        )
        return DEST_ADDRESS

    try:
        int(address, 16)
    except ValueError:
        await update.message.reply_text(
            "❌ Adresse invalide (caractères non-hex). Réessayez :"
        )
        return DEST_ADDRESS

    # Validate EIP-55 checksum if mixed case
    from web3 import Web3
    if not Web3.is_address(address):
        await update.message.reply_text(
            "❌ Adresse Ethereum invalide (checksum incorrect). "
            "Vérifiez l'adresse et réessayez :"
        )
        return DEST_ADDRESS

    context.user_data["withdraw_dest"] = address
    usdc = context.user_data.get("withdraw_balance", 0)

    keyboard = [
        [
            InlineKeyboardButton("💯 Tout retirer", callback_data="withdraw_max"),
        ],
        [InlineKeyboardButton("❌ Annuler", callback_data="withdraw_cancel")],
    ]

    await update.message.reply_text(
        f"📬 Destination : `{address[:6]}...{address[-4:]}`\n\n"
        f"Combien d'USDC voulez-vous retirer ? (max: {usdc:.2f})\n\n"
        "Envoyez le montant (ex: `50`) ou cliquez sur \"Tout retirer\".",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return AMOUNT


async def withdraw_max(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Shortcut — withdraw full balance."""
    query = update.callback_query
    await query.answer()

    usdc = context.user_data.get("withdraw_balance", 0)
    # Keep a tiny buffer for potential rounding
    amount = max(0, usdc - 0.01)
    context.user_data["withdraw_amount"] = round(amount, 2)

    return await _show_confirmation(query, context)


async def receive_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive withdrawal amount."""
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(
            "❌ Montant invalide. Envoyez un nombre (ex: `50`).",
            parse_mode="Markdown",
        )
        return AMOUNT

    usdc = context.user_data.get("withdraw_balance", 0)
    if amount <= 0 or amount > usdc:
        await update.message.reply_text(
            f"❌ Montant invalide. Min: 0.01, Max: {usdc:.2f} USDC."
        )
        return AMOUNT

    context.user_data["withdraw_amount"] = round(amount, 2)

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirmer", callback_data="withdraw_confirm"),
            InlineKeyboardButton("❌ Annuler", callback_data="withdraw_cancel"),
        ]
    ]
    dest = context.user_data["withdraw_dest"]
    await update.message.reply_text(
        "📋 **Résumé du retrait**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💵 Montant : **{amount:.2f} USDC**\n"
        f"📬 Vers : `{dest[:6]}...{dest[-4:]}`\n"
        f"🔗 Réseau : **Polygon**\n"
        f"⛽ Gas estimé : ~0.003 POL\n\n"
        "Confirmer le retrait ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CONFIRM


async def _show_confirmation(query, context) -> int:
    """Display confirmation screen (shared by max and manual amount)."""
    amount = context.user_data["withdraw_amount"]
    dest = context.user_data["withdraw_dest"]

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirmer", callback_data="withdraw_confirm"),
            InlineKeyboardButton("❌ Annuler", callback_data="withdraw_cancel"),
        ]
    ]

    await query.edit_message_text(
        "📋 **Résumé du retrait**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💵 Montant : **{amount:.2f} USDC**\n"
        f"📬 Vers : `{dest[:6]}...{dest[-4:]}`\n"
        f"🔗 Réseau : **Polygon**\n"
        f"⛽ Gas estimé : ~0.003 POL\n\n"
        "Confirmer le retrait ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CONFIRM


async def withdraw_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Execute the on-chain USDC transfer."""
    query = update.callback_query
    await query.answer()

    dest = context.user_data.get("withdraw_dest")
    amount = context.user_data.get("withdraw_amount", 0)

    if not dest or amount <= 0:
        await query.edit_message_text(
            "❌ Données de retrait manquantes. "
            "Cliquez à nouveau sur « 💸 Retirer » dans le menu principal."
        )
        return ConversationHandler.END

    await query.edit_message_text("⏳ **Retrait en cours…** Envoi de la transaction on-chain.", parse_mode="Markdown")

    tg_user = update.effective_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user or not user.wallet_address or not user.encrypted_private_key:
            await query.edit_message_text("❌ Erreur — wallet introuvable.")
            return ConversationHandler.END

        # Check gas
        matic = await polygon_client.get_matic_balance(user.wallet_address)
        if matic < 0.005:
            await query.edit_message_text(
                "❌ **Gas insuffisant**\n\n"
                f"Solde POL : {matic:.4f}\n"
                "Il faut au moins ~0.005 POL pour payer le gas du transfert.\n"
                "Déposez un peu de POL/MATIC sur votre wallet Polygon.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        pk = decrypt_private_key(
            user.encrypted_private_key,
            settings.encryption_key,
            user.uuid,
        )

        result = await polygon_client.transfer_usdc(
            from_address=user.wallet_address,
            to_address=dest,
            amount_usdc=amount,
            private_key=pk,
        )
        del pk

    if result.success:
        scan_url = f"https://polygonscan.com/tx/{result.tx_hash}"
        keyboard = [
            [InlineKeyboardButton("🔍 Voir sur PolygonScan", url=scan_url)]
        ]
        await query.edit_message_text(
            "✅ **Retrait effectué !**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💵 Montant : **{amount:.2f} USDC**\n"
            f"📬 Vers : `{dest[:6]}...{dest[-4:]}`\n"
            f"🔗 Tx : `{result.tx_hash}`\n"
            f"⛽ Gas utilisé : {result.gas_used} unités\n\n"
            "Les USDC arriveront en quelques secondes sur l'adresse de destination.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await query.edit_message_text(
            "❌ **Échec du retrait**\n\n"
            f"Erreur : {result.error}\n\n"
            "Réessayez via le bouton « 💸 Retirer » du menu principal. "
            "Si le problème persiste, vérifiez votre solde USDC et POL.",
            parse_mode="Markdown",
        )

    _clear_withdraw_data(context)
    return ConversationHandler.END


async def withdraw_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel withdrawal."""
    query = update.callback_query
    await query.answer()
    _clear_withdraw_data(context)
    await query.edit_message_text("❌ Retrait annulé.")
    return ConversationHandler.END


def _clear_withdraw_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ("withdraw_dest", "withdraw_amount", "withdraw_balance", "withdraw_matic"):
        context.user_data.pop(key, None)


def get_withdraw_handler() -> ConversationHandler:
    """Build the /withdraw conversation handler."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("withdraw", withdraw_command),
            # Lance /withdraw quand on clique sur « 💸 Retirer » dans le menu principal
            CallbackQueryHandler(withdraw_command, pattern="^menu_withdraw$"),
        ],
        states={
            DEST_ADDRESS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_dest_address
                ),
                CallbackQueryHandler(
                    withdraw_cancel, pattern="^withdraw_cancel$"
                ),
            ],
            AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount),
                CallbackQueryHandler(withdraw_max, pattern="^withdraw_max$"),
                CallbackQueryHandler(
                    withdraw_cancel, pattern="^withdraw_cancel$"
                ),
            ],
            CONFIRM: [
                CallbackQueryHandler(
                    withdraw_confirm, pattern="^withdraw_confirm$"
                ),
                CallbackQueryHandler(
                    withdraw_cancel, pattern="^withdraw_cancel$"
                ),
            ],
        },
        fallbacks=[CommandHandler("withdraw", withdraw_command)],
        per_user=True,
    )
