"""Onboarding handler — /start command."""

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

from bot.config import settings
from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id, create_user, save_wallet
from bot.utils.banner import send_with_banner

logger = logging.getLogger(__name__)

# Conversation states
WELCOME, WALLET_CHOICE, WALLET_ADDRESS, PRIVATE_KEY, CONFIRM = range(5)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — /start."""
    # Message d'accueil unique pour tout le monde (nouveaux et anciens),
    # avec accès au menu principal via un bouton.
    keyboard = [
        [InlineKeyboardButton("🏠 Accéder au menu principal", callback_data="onboard_menu_main")],
        [InlineKeyboardButton("ℹ️ En savoir plus", callback_data="onboard_info")],
    ]

    welcome_text = (
        "👋 **Bienvenue sur WENPOLYMARKET V3**\n\n"
        "Bot Telegram de **copy-trading intelligent** Polymarket.\n"
        "Ne copiez pas bêtement — copiez *malin*.\n\n"
        "✨ **Copy-Trading**\n"
        "• Copie auto des traders que vous choisissez\n"
        "• Wallet Polygon dédié\n"
        "• Suivi soldes, positions, historique\n\n"
        "🧠 **Analyse V3** (ce qui nous rend unique)\n"
        "• Score 0-100 pour chaque signal avant copie\n"
        "• Filtre intelligent (coin-flip, conviction, edge)\n"
        "• Trailing stop, sortie auto, prise de profit partielle\n"
        "• Contrôle du risque portfolio (max positions, diversification)\n"
        "• Analytics en temps réel par trader et catégorie\n\n"
        "🔐 **Sécurité**\n"
        "• Clés privées chiffrées AES-256-GCM\n"
        "• Mode Paper (simulation) activé par défaut\n\n"
        "Cliquez ci-dessous pour commencer."
    )

    await send_with_banner(
        update.message,
        welcome_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    return WELCOME


async def onboard_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show more info about the bot."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "🏠 Accéder au menu principal", callback_data="onboard_menu_main"
            )
        ],
    ]
    await query.message.reply_text(
        "📖 **Comment ça marche ?**\n\n"
        "1️⃣ Vous configurez un wallet Polygon (créé par le bot ou le vôtre)\n"
        "2️⃣ Vous déposez des USDC dessus (carte, exchange ou bridge)\n"
        "3️⃣ Vous choisissez quels traders copier\n"
        "4️⃣ Les trades sont copiés automatiquement\n\n"
        "🔐 **Sécurité :**\n"
        "• Clé privée chiffrée AES-256-GCM\n"
        "• Déchiffrée uniquement en mémoire pour signer\n"
        "• Jamais loguée ni exposée\n",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WELCOME


async def onboard_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin registration — let user choose wallet creation method."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "🆕 Créer un wallet Polygon", callback_data="onboard_create_wallet"
            )
        ],
        [
            InlineKeyboardButton(
                "📩 J'ai déjà un wallet", callback_data="onboard_existing_wallet"
            )
        ],
        [InlineKeyboardButton("⬅️ Annuler", callback_data="onboard_cancel")],
    ]

    await query.edit_message_text(
        "🧭 **Configuration du wallet**\n\n"
        "Le bot a besoin d'un wallet Polygon pour passer des ordres "
        "sur Polymarket en votre nom. Deux options :\n\n"
        "🆕 **Créer un wallet** (recommandé si débutant)\n"
        "→ Le bot génère un nouveau wallet Polygon pour vous.\n"
        "→ Il sera vide : vous devrez y déposer des USDC ensuite "
        "(par carte bancaire, depuis un exchange, ou via bridge).\n"
        "→ Vous n'avez PAS besoin de MetaMask ou autre app.\n\n"
        "📩 **J'ai déjà un wallet Polygon** (utilisateurs avancés)\n"
        "→ Vous avez un wallet Polygon (MetaMask, etc.) avec des USDC dessus.\n"
        "→ Vous fournissez la clé privée (chiffrée immédiatement).\n"
        "→ L'adresse est déduite automatiquement de la clé.\n"
        "→ Le bot utilise directement les USDC déjà présents.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WALLET_CHOICE


async def onboard_menu_main(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Afficher le menu principal depuis l'écran d'accueil (nouvel utilisateur).

    Délègue au menu unifié de menu.py pour éviter toute divergence.
    """
    from bot.handlers.menu import _build_main_menu_content

    query = update.callback_query
    await query.answer()

    tg_user = update.effective_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user:
            user = await create_user(session, tg_user.id, username=tg_user.username)

        text, keyboard = _build_main_menu_content(tg_user, user)

    await query.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def onboard_existing_wallet(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Ask user for their private key (address will be derived automatically)."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("❌ Annuler", callback_data="onboard_cancel")],
    ]
    await query.edit_message_text(
        "🔑 **Importer un wallet — Clé Privée**\n\n"
        "Envoyez votre **clé privée** Polygon.\n\n"
        "📬 L'adresse du wallet sera **déduite automatiquement** "
        "de la clé — pas besoin de la fournir.\n\n"
        "🔒 Votre message sera **immédiatement supprimé** et la clé "
        "chiffrée en AES-256-GCM.\n\n"
        "⚠️ Ne partagez JAMAIS votre clé privée ailleurs.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PRIVATE_KEY


async def onboard_create_wallet(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Create a new Polygon wallet for the user and save it.

    SECURITY: The private key is NEVER displayed in the chat.
    It is encrypted immediately and stored in DB. The user can
    export it later via a dedicated button with auto-delete.
    """
    from web3 import Web3

    query = update.callback_query
    await query.answer()

    tg_user = update.effective_user

    # Generate a new wallet locally (no RPC needed)
    w3 = Web3()
    account = w3.eth.account.create()
    wallet_address = account.address
    private_key = account.key.hex()

    async with async_session() as session:
        existing = await get_user_by_telegram_id(session, tg_user.id)
        if existing:
            user = existing
        else:
            user = await create_user(session, tg_user.id, username=tg_user.username)
        await save_wallet(
            session,
            user,
            wallet_address=wallet_address,
            private_key=private_key,
            chain="polygon",
        )
        user.wallet_auto_created = True
        await session.commit()

    # SECURITY: wipe PK from memory immediately — never show in chat
    del private_key

    keyboard = [
        [InlineKeyboardButton(
            "🔑 Exporter la clé privée (⚠️ sensible)",
            callback_data="export_pk",
        )],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]
    await query.edit_message_text(
        "🎉 **Wallet Polygon dédié créé !**\n\n"
        f"📬 Adresse :\n`{wallet_address}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 **Clé privée : chiffrée et sauvegardée (AES-256-GCM)** ✅\n\n"
        "Pour des raisons de **sécurité**, la clé privée n'est "
        "**pas affichée ici**. Telegram n'est pas chiffré de bout "
        "en bout — un message contenant votre clé pourrait être "
        "intercepté.\n\n"
        "Si vous souhaitez sauvegarder votre clé privée, utilisez "
        "le bouton ci-dessous. Le message s'auto-supprimera après "
        "**60 secondes**.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "**Prochaines étapes :**\n"
        "1. « 💳 Déposer » — Alimenter le wallet en USDC\n"
        "2. « ⚙️ Paramètres » — Choisir vos traders à copier",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    return ConversationHandler.END


async def receive_private_key(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive private key — derive address, encrypt immediately, delete message."""
    private_key = update.message.text.strip()
    chat = update.effective_chat

    # Delete the message containing the private key IMMEDIATELY
    try:
        await update.message.delete()
    except Exception as del_err:
        # H5 FIX: Warn user urgently if PK message couldn't be deleted
        logger.error(f"CRITICAL: Failed to delete PK message for {update.effective_user.id}: {del_err}")
        try:
            await chat.send_message(
                "⚠️ **ALERTE SÉCURITÉ** ⚠️\n\n"
                "Impossible de supprimer automatiquement votre message "
                "contenant la clé privée.\n\n"
                "**Supprimez-le MANUELLEMENT immédiatement** pour protéger "
                "votre wallet !\n\n"
                "📱 Appuyez longuement sur le message → Supprimer",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    cancel_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Annuler", callback_data="onboard_cancel")]]
    )

    # Strip 0x prefix if present for consistency
    pk_clean = private_key
    if pk_clean.startswith("0x"):
        pk_clean = pk_clean[2:]

    # Basic validation (use chat.send_message since original message may be deleted)
    if len(pk_clean) < 32:
        await chat.send_message(
            "❌ Clé privée trop courte. Réessayez :",
            reply_markup=cancel_kb,
        )
        return PRIVATE_KEY

    # Derive the wallet address from the private key
    try:
        from eth_account import Account
        derived_address = Account.from_key(private_key).address
    except Exception as e:
        logger.warning(f"Invalid private key during onboarding: {e}")
        await chat.send_message(
            "❌ Clé privée invalide — impossible de dériver l'adresse.\n\n"
            "Vérifiez que vous avez copié la bonne clé et réessayez :",
            reply_markup=cancel_kb,
        )
        return PRIVATE_KEY

    context.user_data["private_key"] = private_key
    context.user_data["wallet_address"] = derived_address

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirmer", callback_data="onboard_confirm"),
            InlineKeyboardButton("❌ Annuler", callback_data="onboard_cancel"),
        ]
    ]
    await chat.send_message(
        "📋 **Résumé de l'inscription**\n\n"
        f"📬 Wallet dérivé : `{derived_address}`\n"
        "🔑 Clé privée : ✅ Reçue (sera chiffrée AES-256)\n"
        "📝 Mode : Paper Trading (défaut)\n"
        "💸 Frais : 1% par trade copié\n\n"
        "⚠️ **Vérifiez que l'adresse ci-dessus correspond bien "
        "à votre wallet (MetaMask, etc.).**\n\n"
        "Confirmer l'inscription ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CONFIRM


async def onboard_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Finalize registration — create user, encrypt and save key."""
    query = update.callback_query
    await query.answer()

    tg_user = update.effective_user
    wallet_address = context.user_data.get("wallet_address")
    private_key = context.user_data.get("private_key")

    if not wallet_address or not private_key:
        await query.edit_message_text("❌ Erreur — données manquantes. Relancez /start.")
        return ConversationHandler.END

    async with async_session() as session:
        # Si l'utilisateur existe déjà, on met simplement à jour / enregistre son wallet.
        existing = await get_user_by_telegram_id(session, tg_user.id)
        if existing:
            user = existing
        else:
            user = await create_user(session, tg_user.id, username=tg_user.username)

        await save_wallet(session, user, wallet_address, private_key, chain="polygon")
        user.wallet_auto_created = False
        await session.commit()

        keyboard = [
            [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
        ]
        await query.edit_message_text(
            "🎉 **Wallet importé avec succès !**\n\n"
            f"📬 Wallet : `{wallet_address[:6]}...{wallet_address[-4:]}`\n"
            "🔒 Clé privée : chiffrée AES-256-GCM ✅\n"
            "📝 Mode : Paper Trading (par défaut)\n\n"
            "🔐 Votre clé est stockée **chiffrée** et utilisée uniquement "
            "pour signer vos trades automatiquement. Elle n'est jamais "
            "visible en clair.\n\n"
            "**Prochaines étapes :**\n"
            "1. « 👛 Wallets » — Voir votre wallet et vos soldes\n"
            "2. « ⚙️ Paramètres » — Choisir quels traders copier",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    # Clear sensitive data from context
    context.user_data.pop("wallet_address", None)
    context.user_data.pop("private_key", None)

    return ConversationHandler.END


async def onboard_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel registration and return to main menu."""
    from bot.handlers.menu import _build_main_menu_content

    query = update.callback_query
    await query.answer()

    context.user_data.pop("wallet_address", None)
    context.user_data.pop("private_key", None)

    tg_user = update.effective_user
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if user:
            text, keyboard = _build_main_menu_content(tg_user, user)
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            await query.edit_message_text(
                "❌ Inscription annulée.\n\nVous pouvez relancer /start à tout moment."
            )

    return ConversationHandler.END


def get_start_handler() -> ConversationHandler:
    """Build the /start conversation handler (écran d'accueil + config wallet)."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            # Permet de lancer "Configurer mon wallet" depuis le menu principal
            CallbackQueryHandler(onboard_start, pattern="^onboard_start$"),
        ],
        states={
            WELCOME: [
                CallbackQueryHandler(onboard_menu_main, pattern="^onboard_menu_main$"),
                CallbackQueryHandler(onboard_info, pattern="^onboard_info$"),
            ],
            WALLET_CHOICE: [
                CallbackQueryHandler(
                    onboard_existing_wallet, pattern="^onboard_existing_wallet$"
                ),
                CallbackQueryHandler(
                    onboard_create_wallet, pattern="^onboard_create_wallet$"
                ),
                CallbackQueryHandler(onboard_cancel, pattern="^onboard_cancel$"),
            ],
            PRIVATE_KEY: [
                CallbackQueryHandler(onboard_cancel, pattern="^onboard_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_private_key),
            ],
            CONFIRM: [
                CallbackQueryHandler(onboard_confirm, pattern="^onboard_confirm$"),
                CallbackQueryHandler(onboard_cancel, pattern="^onboard_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start_command),
            CallbackQueryHandler(onboard_menu_main, pattern="^onboard_menu_main$"),
        ],
        per_user=True,
        per_message=False,
    )
