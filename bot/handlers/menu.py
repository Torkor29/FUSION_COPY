"""Main menu callback handlers — each button directly executes the relevant logic."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes
from sqlalchemy import select, func

from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id, get_or_create_settings
from bot.services.web3_client import polygon_client

logger = logging.getLogger(__name__)


async def _send_main_menu(message, tg_user, text_override: str | None = None) -> None:
    """Build and send the main menu (reusable)."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user:
            await message.reply_text("❌ Compte non trouvé. Lancez l'inscription avec le bouton ci-dessous.")
            return

        status = "🟢 Actif" if user.is_active and not user.is_paused else "🟡 Pause" if user.is_paused else "🔴 Inactif"
        mode = "📝 Paper" if user.paper_trading else "💵 Réel"
        wallet_short = f"`{user.wallet_address[:6]}...{user.wallet_address[-4:]}`" if user.wallet_address else "—"
        us = user.settings
        traders_count = len(us.followed_wallets) if us and us.followed_wallets else 0

    header = text_override or (
        f"👋 **{tg_user.first_name}** — Menu principal\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📬 Wallet : {wallet_short}\n"
        f"{status} • {mode} • **{traders_count}** trader(s) suivi(s)"
    )

    keyboard = [
        [
            InlineKeyboardButton("👛 Wallets", callback_data="menu_balance"),
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
        [InlineKeyboardButton("❓ Aide", callback_data="menu_help")],
    ]

    await message.reply_text(
        header, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── Balance ──────────────────────────────────────────

async def menu_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        wallet = user.wallet_address or ""
        wallet_short = f"`{wallet[:6]}...{wallet[-4:]}`" if wallet else "—"

    if wallet:
        usdc_native, usdc_e = await polygon_client.get_usdc_balances(wallet)
        pol = await polygon_client.get_matic_balance(wallet)
    else:
        usdc_native, usdc_e, pol = 0.0, 0.0, 0.0

    extra = ""
    if usdc_e > 0 and usdc_native == 0:
        extra = (
            "\n\n⚠️ Vous avez des **USDC.e (anciens USDC bridgés)** sur ce wallet.\n"
            "Ils ne sont pas directement utilisables pour trader sur Polymarket.\n"
            "Swappez-les en **USDC natif** sur Polygon pour qu'ils soient visibles ici."
        )

    header = "👛 **WALLETS**\n━━━━━━━━━━━━━━━━━━━━\n\n"
    if wallet:
        wallet_block = (
            f"🔷 **Polygon (principal)**\n"
            f"   📬 Adresse : {wallet_short}\n"
            f"   💵 USDC natif (Polymarket) : **{usdc_native:.2f}**\n"
            f"   💵 USDC.e (bridgé) : **{usdc_e:.2f}**\n"
            f"   ⛽ POL (gas) : **{pol:.4f}**\n"
        )
    else:
        wallet_block = (
            "🔷 **Polygon (principal)**\n"
            "   📬 Adresse : *Non configuré*\n"
            "   👉 Utilisez le bouton « 🧭 Configurer mon wallet » dans le menu principal.\n"
        )

    text = header + wallet_block + extra

    keyboard = [
        [
            InlineKeyboardButton("💳 Déposer", callback_data="menu_deposit"),
            InlineKeyboardButton("💸 Retirer", callback_data="menu_withdraw"),
        ],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── Positions ────────────────────────────────────────

async def menu_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        from bot.models.trade import Trade, TradeStatus
        result = await session.execute(
            select(Trade).where(
                Trade.user_id == user.id, Trade.status == TradeStatus.FILLED
            ).order_by(Trade.created_at.desc()).limit(15)
        )
        trades = result.scalars().all()

    if not trades:
        text = "📊 **Positions actives**\n\nAucune position pour le moment."
    else:
        lines = ["📊 **POSITIONS ACTIVES**\n━━━━━━━━━━━━━━━━━━━━\n"]
        for t in trades:
            emoji = "🟢" if t.side.value == "buy" else "🔴"
            q = t.market_question or t.market_id
            if len(q) > 40:
                q = q[:37] + "..."
            lines.append(
                f"{emoji} **{q}**\n"
                f"   {t.side.value.upper()} @ {t.price:.2f} | "
                f"{t.net_amount_usdc:.2f} USDC\n"
            )
        text = "\n".join(lines)

    keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── History ──────────────────────────────────────────

async def menu_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        from bot.models.trade import Trade, TradeStatus
        result = await session.execute(
            select(Trade).where(Trade.user_id == user.id)
            .order_by(Trade.created_at.desc()).limit(15)
        )
        trades = result.scalars().all()

    if not trades:
        text = "📜 **Historique**\n\nAucun trade enregistré."
    else:
        status_emoji = {"filled": "✅", "failed": "❌", "cancelled": "🚫", "pending": "🟡", "executing": "🔄"}
        lines = ["📜 **HISTORIQUE**\n━━━━━━━━━━━━━━━━━━━━\n"]
        for t in trades:
            emoji = status_emoji.get(t.status.value, "❓")
            q = t.market_question or t.market_id
            if len(q) > 35:
                q = q[:32] + "..."
            date_str = t.created_at.strftime("%d/%m %H:%M") if t.created_at else "?"
            paper = " 📝" if t.is_paper else ""
            lines.append(f"{emoji} {date_str} | {t.net_amount_usdc:.2f} USDC{paper}\n   {q}\n")
        text = "\n".join(lines)

    keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── Deposit (inline summary + link to /deposit) ─────

async def menu_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        wallet = user.wallet_address if user else ""
        auto = user.wallet_auto_created if user else False

    if auto:
        tag = "🏷️ *Wallet créé par le bot — il est vide au départ*"
    else:
        tag = "🏷️ *Wallet importé — vos fonds existants sont utilisables*"

    text = (
        "💳 **DÉPOSER DES USDC**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{tag}\n"
        f"📬 `{wallet}`\n\n"
        "Choisissez une option :"
    )

    keyboard = [
        [InlineKeyboardButton("💳 Carte bancaire", callback_data="dep_card")],
        [InlineKeyboardButton("🏦 Depuis un exchange", callback_data="dep_exchange")],
        [InlineKeyboardButton("🌉 Bridge (SOL, ETH…)", callback_data="dep_bridge")],
        [InlineKeyboardButton("📋 Copier adresse", callback_data="deposit_copy_address")],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── Withdraw / Bridge / Settings / Traders ──────────────────────────

async def menu_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ce handler reste déclaré mais l'action réelle est gérée par
    la ConversationHandler de /withdraw, qui intercepte directement
    le callback « menu_withdraw ». Rien à faire ici."""
    query = update.callback_query
    await query.answer()
    # Le flux de retrait démarre automatiquement via get_withdraw_handler().


async def menu_bridge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Délègue directement au flux /bridge pour éviter de demander une commande."""
    # On laisse la logique d'affichage centralisée dans bridge_command
    from bot.handlers.bridge import bridge_command

    await bridge_command(update, context)


async def menu_traders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return
        us = await get_or_create_settings(session, user)
        wallets = us.followed_wallets or []

    if wallets:
        lines = [f"  {i}. `{w[:6]}...{w[-4:]}`" for i, w in enumerate(wallets, 1)]
        wallet_text = "\n".join(lines)
    else:
        wallet_text = "  _Aucun trader suivi_"

    text = (
        "👥 **TRADERS SUIVIS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{wallet_text}\n\n"
        "Pour ajouter ou retirer des traders, utilisez le bouton "
        "**⚙️ Paramètres** ci-dessous."
    )

    keyboard = [
        [InlineKeyboardButton("⚙️ Ouvrir paramètres", callback_data="menu_settings")],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def menu_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ce handler est conservé pour compatibilité mais la vraie ouverture
    des paramètres est gérée par la ConversationHandler /settings
    (qui intercepte directement le callback « menu_settings »)."""
    query = update.callback_query
    await query.answer()


async def menu_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    text = (
        "❓ **AIDE — NAVIGATION**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "**Depuis le menu principal :**\n\n"
        "💰 **Soldes** — Voir USDC, USDC.e et gas\n"
        "💳 **Déposer** — Carte, exchange, bridge\n"
        "💸 **Retirer** — Envoyer vos USDC vers un autre wallet / exchange\n"
        "🌉 **Bridge** — Guide pour bridger SOL/ETH → USDC Polygon\n\n"
        "📊 **Positions** — Voir les trades copiés en cours\n"
        "📜 **Historique** — Derniers trades\n"
        "👥 **Traders suivis** — Liste des wallets copiés\n"
        "⚙️ **Paramètres** — Capital, sizing, risques, traders suivis\n\n"
        "Vous pouvez toujours revenir au menu principal avec le bouton "
        "« 🏠 Menu principal »."
    )

    keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def menu_wallet_bridge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explication pour les utilisateurs qui ont déjà de la crypto sur une autre chaîne."""
    query = update.callback_query
    await query.answer()

    text = (
        "🌉 **J'ai de la crypto sur une autre blockchain**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ Commence par **créer ou importer un wallet Polygon** avec les boutons ci-dessus.\n"
        "   → Tu auras alors une adresse de destination sur Polygon.\n\n"
        "2️⃣ Ensuite, depuis le menu principal, utilise le bouton « 🌉 Bridge ».\n"
        "   → Le guide t'explique comment bridger SOL, ETH, USDC... vers ton wallet Polygon.\n\n"
        "En résumé :\n"
        "• D'abord une adresse Polygon (wallet dédié ou existant)\n"
        "• Ensuite seulement, le bridge pour envoyer des fonds dessus."
    )

    keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── Back to main menu ───────────────────────────────

async def menu_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    tg_user = query.from_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user:
            return

        status = "🟢 Actif" if user.is_active and not user.is_paused else "🟡 Pause" if user.is_paused else "🔴 Inactif"
        mode = "📝 Paper" if user.paper_trading else "💵 Réel"
        wallet_short = f"`{user.wallet_address[:6]}...{user.wallet_address[-4:]}`" if user.wallet_address else "—"
        us = user.settings
        traders_count = len(us.followed_wallets) if us and us.followed_wallets else 0

    header = (
        f"👋 **{tg_user.first_name}** — Menu principal\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📬 Wallet : {wallet_short}\n"
        f"{status} • {mode} • **{traders_count}** trader(s) suivi(s)"
    )

    keyboard = [
        [
            InlineKeyboardButton("👛 Wallets", callback_data="menu_balance"),
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
        [InlineKeyboardButton("❓ Aide", callback_data="menu_help")],
    ]

    await query.edit_message_text(
        header, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


def get_menu_handlers() -> list:
    return [
        CallbackQueryHandler(menu_balance, pattern="^menu_balance$"),
        CallbackQueryHandler(menu_positions, pattern="^menu_positions$"),
        CallbackQueryHandler(menu_deposit, pattern="^menu_deposit$"),
        CallbackQueryHandler(menu_withdraw, pattern="^menu_withdraw$"),
        CallbackQueryHandler(menu_traders, pattern="^menu_traders$"),
        CallbackQueryHandler(menu_settings, pattern="^menu_settings$"),
        CallbackQueryHandler(menu_bridge, pattern="^menu_bridge$"),
        CallbackQueryHandler(menu_history, pattern="^menu_history$"),
        CallbackQueryHandler(menu_help, pattern="^menu_help$"),
        CallbackQueryHandler(menu_wallet_bridge, pattern="^menu_wallet_bridge$"),
        CallbackQueryHandler(menu_back, pattern="^menu_back$"),
    ]
