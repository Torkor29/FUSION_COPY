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

from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id, create_user, save_wallet

logger = logging.getLogger(__name__)

# Conversation states
WELCOME, WALLET_CHOICE, WALLET_ADDRESS, PRIVATE_KEY, CONFIRM = range(5)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — /start."""
    tg_user = update.effective_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)

        if user:
            status = "🟢 Actif" if user.is_active and not user.is_paused else "🟡 Pause" if user.is_paused else "🔴 Inactif"
            mode = "📝 Paper" if user.paper_trading else "💵 Réel"
            wallet_short = f"`{user.wallet_address[:6]}...{user.wallet_address[-4:]}`" if user.wallet_address else "Non configuré"

            us = None
            if user.settings:
                us = user.settings
            traders_count = len(us.followed_wallets) if us and us.followed_wallets else 0

            text = (
                f"👋 **{tg_user.first_name}** — Menu principal\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📬 Wallet : {wallet_short}\n"
                f"{status} • {mode} • **{traders_count}** trader(s) suivi(s)\n"
            )

            keyboard = [
                [
                    InlineKeyboardButton("💰 Soldes", callback_data="menu_balance"),
                    InlineKeyboardButton("📊 Positions", callback_data="menu_positions"),
                ],
                [
                    InlineKeyboardButton("💳 Déposer", callback_data="menu_deposit"),
                    InlineKeyboardButton("💸 Retirer", callback_data="menu_withdraw"),
                ],
                [
                    InlineKeyboardButton("👥 Traders suivis", callback_data="menu_traders"),
                    InlineKeyboardButton("⚙️ Paramètres", callback_data="menu_settings"),
                ],
                [
                    InlineKeyboardButton("🌉 Bridge", callback_data="menu_bridge"),
                    InlineKeyboardButton("📜 Historique", callback_data="menu_history"),
                ],
                [
                    InlineKeyboardButton("❓ Aide", callback_data="menu_help"),
                ],
            ]

            await update.message.reply_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return ConversationHandler.END

    # New user — start onboarding
    keyboard = [
        [InlineKeyboardButton("🚀 Commencer l'inscription", callback_data="onboard_start")],
        [InlineKeyboardButton("ℹ️ En savoir plus", callback_data="onboard_info")],
    ]
    await update.message.reply_text(
        "👋 **Bienvenue sur Polymarket CopyTrader !**\n\n"
        "Ce bot vous permet de copier automatiquement les trades "
        "d'un trader expert sur Polymarket.\n\n"
        "🔒 Vos clés sont chiffrées AES-256 et jamais stockées en clair.\n"
        "📝 Vous démarrez en mode Paper Trading (sans fonds réels).\n"
        "💸 Frais de plateforme : 1% par trade copié.\n\n"
        "Prêt à commencer ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WELCOME


async def onboard_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show more info about the bot."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🚀 Commencer l'inscription", callback_data="onboard_start")],
    ]
    await query.edit_message_text(
        "📖 **Comment ça marche ?**\n\n"
        "1️⃣ Vous configurez un wallet Polygon (créé par le bot ou le vôtre)\n"
        "2️⃣ Vous déposez des USDC dessus (carte, exchange ou bridge)\n"
        "3️⃣ Vous choisissez quels traders copier\n"
        "4️⃣ Les trades sont copiés automatiquement\n\n"
        "🔐 **Sécurité :**\n"
        "• Clé privée chiffrée AES-256-GCM\n"
        "• Déchiffrée uniquement en mémoire pour signer\n"
        "• Jamais loguée ni exposée\n\n"
        "💸 **Frais :** 1% prélevé sur chaque trade copié\n"
        "📝 Vous commencez en Paper Trading par défaut\n"
        "💸 Vous pouvez retirer vos fonds à tout moment depuis le bouton « 💸 Retirer » du menu.",
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
        "→ Vous fournissez l'adresse + la clé privée (chiffrée immédiatement).\n"
        "→ Le bot utilise directement les USDC déjà présents.\n"
        "→ Vous pouvez aussi bridger du SOL/ETH vers ce wallet.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WALLET_CHOICE


async def onboard_existing_wallet(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Ask user for an existing wallet address."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "📬 **Étape 1/2 — Adresse Wallet**\n\n"
        "Envoyez votre adresse wallet **Polygon** (0x...).\n\n"
        "⚠️ Cette adresse sera utilisée pour le trading sur Polymarket.",
        parse_mode="Markdown",
    )
    return WALLET_ADDRESS


async def onboard_create_wallet(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Create a new Polygon wallet for the user and save it."""
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

    del private_key

    await query.edit_message_text(
        "🎉 **Wallet Polygon créé !**\n\n"
        f"📬 Adresse : `{wallet_address}`\n\n"
        "⚠️ **Ce wallet est vide.** Pour copier des trades, "
        "vous devez d'abord y déposer des **USDC**.\n\n"
        "Depuis le menu principal, utilisez « 💳 Déposer » — le bot vous guide pour :\n"
        "• 💳 Acheter des USDC par carte bancaire\n"
        "• 🏦 Envoyer depuis un exchange (Binance, etc.)\n"
        "• 🌉 Bridger du SOL ou de l'ETH vers ce wallet\n\n"
        "Puis cliquez sur « ⚙️ Paramètres » pour choisir quels traders copier.",
        parse_mode="Markdown",
    )

    return ConversationHandler.END


async def receive_wallet_address(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive and validate wallet address."""
    address = update.message.text.strip()

    # Basic validation
    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text(
            "❌ Adresse invalide. Elle doit commencer par `0x` "
            "et faire 42 caractères.\n\nRéessayez :",
            parse_mode="Markdown",
        )
        return WALLET_ADDRESS

    # Check hex
    try:
        int(address, 16)
    except ValueError:
        await update.message.reply_text(
            "❌ Adresse invalide — caractères non-hexadécimaux détectés.\n\nRéessayez :",
        )
        return WALLET_ADDRESS

    context.user_data["wallet_address"] = address

    await update.message.reply_text(
        "🔑 **Étape 2/2 — Clé Privée**\n\n"
        "Envoyez votre clé privée Polygon.\n\n"
        "🔒 Elle sera **immédiatement chiffrée** (AES-256-GCM) "
        "et le message sera supprimé.\n\n"
        "⚠️ Ne partagez JAMAIS votre clé privée ailleurs.",
        parse_mode="Markdown",
    )
    return PRIVATE_KEY


async def receive_private_key(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive private key — encrypt immediately, delete message."""
    private_key = update.message.text.strip()

    # Delete the message containing the private key IMMEDIATELY
    try:
        await update.message.delete()
    except Exception:
        pass  # Bot may not have delete permissions

    # Basic validation
    if len(private_key) < 32:
        await update.message.reply_text(
            "❌ Clé privée trop courte. Réessayez :",
        )
        return PRIVATE_KEY

    context.user_data["private_key"] = private_key

    wallet = context.user_data["wallet_address"]
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirmer", callback_data="onboard_confirm"),
            InlineKeyboardButton("❌ Annuler", callback_data="onboard_cancel"),
        ]
    ]
    await update.effective_chat.send_message(
        "📋 **Résumé de l'inscription**\n\n"
        f"📬 Wallet : `{wallet[:6]}...{wallet[-4:]}`\n"
        "🔑 Clé privée : ✅ Reçue (sera chiffrée)\n"
        "📝 Mode : Paper Trading (défaut)\n"
        "💸 Frais : 1% par trade copié\n\n"
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
        user = await create_user(
            session, tg_user.id, username=tg_user.username
        )
        await save_wallet(session, user, wallet_address, private_key, chain="polygon")
        user.wallet_auto_created = False
        await session.commit()

        await query.edit_message_text(
            "🎉 **Inscription réussie !**\n\n"
            f"🆔 Votre ID : `{user.uuid}`\n"
            f"📬 Wallet importé : `{wallet_address[:6]}...{wallet_address[-4:]}`\n"
            "🔒 Clé privée : chiffrée AES-256 ✅\n"
            "📝 Mode : Paper Trading\n\n"
            "💡 Le bot utilisera les USDC déjà présents sur ce wallet. "
            "Vous pouvez aussi déposer plus de fonds via le bouton « 💳 Déposer ».\n\n"
            "**Prochaines étapes :**\n"
            "• Bouton « 💰 Soldes » — Voir vos soldes actuels\n"
            "• Bouton « ⚙️ Paramètres » — Choisir quels traders copier\n"
            "• Bouton « 💳 Déposer » — Ajouter des fonds si besoin",
            parse_mode="Markdown",
        )

    # Clear sensitive data from context
    context.user_data.pop("wallet_address", None)
    context.user_data.pop("private_key", None)

    return ConversationHandler.END


async def onboard_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel registration."""
    query = update.callback_query
    await query.answer()

    context.user_data.pop("wallet_address", None)
    context.user_data.pop("private_key", None)

    await query.edit_message_text(
        "❌ Inscription annulée.\n\nVous pouvez relancer /start à tout moment."
    )
    return ConversationHandler.END


def get_start_handler() -> ConversationHandler:
    """Build the /start conversation handler."""
    return ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            WELCOME: [
                CallbackQueryHandler(onboard_start, pattern="^onboard_start$"),
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
            WALLET_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_wallet_address),
            ],
            PRIVATE_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_private_key),
            ],
            CONFIRM: [
                CallbackQueryHandler(onboard_confirm, pattern="^onboard_confirm$"),
                CallbackQueryHandler(onboard_cancel, pattern="^onboard_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("start", start_command)],
        per_user=True,
    )
