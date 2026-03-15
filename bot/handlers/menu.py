"""Main menu callback handlers — each button directly executes the relevant logic."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes
from sqlalchemy import select, func, desc

from bot.config import settings
from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id, get_or_create_settings
from bot.services.web3_client import polygon_client
from bot.utils.banner import send_with_banner

logger = logging.getLogger(__name__)


def _build_main_menu_content(tg_user, user) -> tuple[str, list]:
    """Build main menu text and keyboard (single source of truth)."""
    if user.is_active and not user.is_paused:
        status = "🟢 Actif"
    elif user.is_paused:
        status = "🟡 Pause"
    else:
        status = "🔴 Inactif"

    mode = "📝 Paper" if user.paper_trading else "💵 Réel"
    wallet_short = (
        f"`{user.wallet_address[:6]}...{user.wallet_address[-4:]}`"
        if user.wallet_address
        else "Non configuré"
    )
    us = user.settings
    traders_count = len(us.followed_wallets) if us and us.followed_wallets else 0

    # Paper wallet quick summary
    paper_line = ""
    if user.paper_trading:
        paper_line = (
            f"💰 Paper : **{user.paper_balance:.2f}** / "
            f"{user.paper_initial_balance:.2f} USDC\n"
        )

    text = (
        f"**WENPOLYMARKET** — Copy-Trading Polymarket\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Bonjour **{tg_user.first_name}** !\n\n"
        f"📬 Wallet : {wallet_short}\n"
        f"🎛️ {status} • {mode}\n"
        f"{paper_line}"
        f"👥 {traders_count} trader(s) suivi(s)\n"
    )

    if not user.wallet_address:
        text += (
            "\n⚠️ **Wallet non configuré** — Cliquez sur "
            "« 🧭 Configurer mon wallet » pour commencer.\n"
        )

    keyboard = []
    if not user.wallet_address:
        keyboard.append(
            [InlineKeyboardButton(
                "🧭 Configurer mon wallet", callback_data="onboard_start"
            )]
        )

    keyboard.extend([
        [
            InlineKeyboardButton("👛 Wallets", callback_data="menu_balance"),
            InlineKeyboardButton("📊 Positions", callback_data="menu_positions"),
        ],
        [
            InlineKeyboardButton("📜 Historique", callback_data="menu_history"),
            InlineKeyboardButton("👥 Traders suivis", callback_data="menu_traders"),
        ],
        [
            InlineKeyboardButton("⚙️ Paramètres", callback_data="menu_settings"),
            InlineKeyboardButton("❓ Aide", callback_data="menu_help"),
        ],
        [
            InlineKeyboardButton("📡 Dashboard", callback_data="menu_dashboard"),
            InlineKeyboardButton("📋 Récap", callback_data="menu_recap"),
        ],
    ])

    # Stop / Resume Copy button — always visible
    if user.is_active and not user.is_paused:
        keyboard.append([
            InlineKeyboardButton("🛑 Stop Copy", callback_data="stop_copy"),
        ])
    elif user.is_paused:
        keyboard.append([
            InlineKeyboardButton("▶️ Reprendre le Copy", callback_data="resume_copy"),
        ])

    if user.paper_trading:
        keyboard.insert(-1, [
            InlineKeyboardButton("📝 Paper Wallet", callback_data="menu_paper"),
        ])

    return text, keyboard


async def _send_main_menu(message, tg_user, text_override: str | None = None) -> None:
    """Build and send the main menu (reusable)."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user:
            await message.reply_text(
                "❌ Compte non trouvé. Lancez /start pour vous inscrire."
            )
            return

        text, keyboard = _build_main_menu_content(tg_user, user)

        # Fetch last copied trade per followed trader
        us = await get_or_create_settings(session, user)
        followed = us.followed_wallets or []
        if followed:
            from bot.models.trade import Trade, TradeStatus
            last_trades_lines: list[str] = []
            for wallet in followed:
                w_short = f"{wallet[:6]}...{wallet[-4:]}"
                result = await session.execute(
                    select(Trade).where(
                        Trade.user_id == user.id,
                        Trade.master_wallet == wallet,
                        Trade.status == TradeStatus.FILLED,
                    ).order_by(desc(Trade.created_at)).limit(1)
                )
                last = result.scalar_one_or_none()
                if last and last.created_at:
                    dt = last.created_at.strftime("%d/%m %H:%M")
                    side = "🟢 BUY" if last.side.value == "buy" else "🔴 SELL"
                    q = last.market_question or last.market_id or "?"
                    if len(q) > 30:
                        q = q[:27] + "..."
                    last_trades_lines.append(
                        f"  `{w_short}` → {side} {dt}\n"
                        f"    _{q}_ • {last.net_amount_usdc:.2f}$"
                    )
                else:
                    last_trades_lines.append(
                        f"  `{w_short}` → _Aucun trade copié_"
                    )
            text += (
                "\n📡 **Dernière activité copiée :**\n"
                + "\n".join(last_trades_lines)
                + "\n"
            )

    header = text_override or text

    await send_with_banner(
        message, header, reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── Balance ──────────────────────────────────────────

async def menu_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        wallets = user.wallets or []
        primary_address = user.wallet_address or ""
        has_encrypted_pk = user.encrypted_private_key is not None

    if primary_address:
        usdc_native, usdc_e = await polygon_client.get_usdc_balances(primary_address)
        pol = await polygon_client.get_matic_balance(primary_address)
        wallet_short = f"`{primary_address[:6]}...{primary_address[-4:]}`"
    else:
        usdc_native, usdc_e, pol = 0.0, 0.0, 0.0
        wallet_short = "—"

    extra = ""
    if usdc_e > 0 and usdc_native == 0:
        extra = (
            "\n\n⚠️ Vous avez des **USDC.e (anciens USDC bridgés)** sur ce wallet.\n"
            "Ils ne sont pas directement utilisables pour trader sur Polymarket.\n"
            "Swappez-les en **USDC natif** sur Polygon pour qu'ils soient visibles ici."
        )

    header = "👛 **WALLETS**\n━━━━━━━━━━━━━━━━━━━━\n\n"
    if primary_address:
        wallet_block = (
            f"🔷 **Polygon (principal)** — utilisé pour le copy-trading\n"
            f"   📬 Adresse : {wallet_short}\n"
            f"   💵 USDC natif (Polymarket) : **{usdc_native:.2f}**\n"
            f"   💵 USDC.e (bridgé) : **{usdc_e:.2f}**\n"
            f"   ⛽ POL (gas) : **{pol:.4f}**\n"
        )
    else:
        wallet_block = (
            "🔷 **Polygon (principal)** — aucun wallet configuré\n"
            "   👉 Utilisez « 🧭 Configurer mon wallet » pour créer ou importer un wallet.\n"
        )

    # Autres wallets enregistrés (archives / consultation)
    other_lines: list[str] = []
    for w in wallets:
        if w.chain != "polygon":
            continue
        if primary_address and w.address == primary_address:
            continue
        other_lines.append(f"• `{w.address[:6]}...{w.address[-4:]}`")

    if other_lines:
        others_block = (
            "\n📂 **Autres wallets enregistrés** (non utilisés par le bot)\n"
            + "\n".join(other_lines)
            + "\n"
        )
    else:
        others_block = ""

    text = header + wallet_block + others_block + extra

    # Compter les wallets enregistrés pour proposer le switch
    has_multiple_wallets = len([w for w in wallets if w.chain == "polygon"]) > 1

    keyboard = [
        [
            InlineKeyboardButton("💳 Déposer", callback_data="menu_deposit"),
            InlineKeyboardButton("💸 Retirer", callback_data="menu_withdraw"),
        ],
    ]
    if has_multiple_wallets:
        keyboard.append([
            InlineKeyboardButton(
                "🔀 Changer de wallet actif", callback_data="menu_switch_wallet"
            ),
        ])
    if primary_address and has_encrypted_pk:
        keyboard.append([
            InlineKeyboardButton(
                "🔑 Exporter la clé privée", callback_data="export_pk"
            ),
        ])
    if wallets:
        keyboard.append([
            InlineKeyboardButton(
                "🗑️ Supprimer un wallet", callback_data="menu_delete_wallet"
            ),
        ])
    keyboard.extend([
        [
            InlineKeyboardButton(
                "🧭 Ajouter un nouveau wallet", callback_data="onboard_start"
            ),
        ],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ])

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

        from bot.models.trade import Trade, TradeStatus, TradeSide
        # Only BUY trades that are FILLED = open positions
        result = await session.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
                Trade.side == TradeSide.BUY,
            ).order_by(Trade.created_at.desc()).limit(20)
        )
        trades = list(result.scalars().all())

    if not trades:
        text = (
            "📊 **POSITIONS EN COURS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_Positions = paris achetés et toujours ouverts_\n"
            "_sur Polymarket (pas encore vendus/résolus)._\n\n"
            "Aucune position ouverte."
        )
        keyboard = [
            [InlineKeyboardButton("🔄 Rafraîchir", callback_data="menu_positions")],
            [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
        ]
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Fetch current prices for PNL calculation
    from bot.services.polymarket import polymarket_client

    # Collect unique token_ids and fetch current book prices
    current_prices: dict[str, float] = {}
    for t in trades:
        if t.token_id not in current_prices:
            current_prices[t.token_id] = 0.0

    # Fetch prices in parallel
    import asyncio

    async def _fetch_price(token_id: str) -> tuple[str, float]:
        try:
            price = await polymarket_client.get_price(token_id)
            return token_id, price
        except Exception:
            return token_id, 0.0

    price_tasks = [_fetch_price(tid) for tid in current_prices]
    price_results = await asyncio.gather(*price_tasks, return_exceptions=True)
    for res in price_results:
        if isinstance(res, tuple):
            current_prices[res[0]] = res[1]

    lines = [
        "📊 **POSITIONS EN COURS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "_Positions = paris achetés et toujours ouverts_\n"
        "_sur Polymarket (pas encore vendus/résolus)._\n"
    ]
    total_invested = 0.0
    total_current = 0.0

    for t in trades:
        q = t.market_question or t.market_id
        if len(q) > 45:
            q = q[:42] + "..."

        paper = " 📝" if t.is_paper else ""

        # Time
        if t.created_at:
            time_str = t.created_at.strftime("%d/%m %H:%M")
        else:
            time_str = "?"

        # PNL calculation
        entry_price = t.price
        cur_price = current_prices.get(t.token_id, 0)
        invested = t.net_amount_usdc
        shares = t.shares if t.shares else (invested / entry_price if entry_price > 0 else 0)
        current_value = shares * cur_price if cur_price > 0 else 0
        pnl_usdc = current_value - invested
        pnl_pct = (pnl_usdc / invested * 100) if invested > 0 else 0

        total_invested += invested
        total_current += current_value if cur_price > 0 else invested

        # PNL display
        if cur_price > 0:
            pnl_sign = "+" if pnl_usdc >= 0 else ""
            pnl_emoji = "📈" if pnl_usdc >= 0 else "📉"
            pnl_str = f"{pnl_emoji} {pnl_sign}{pnl_usdc:.2f} USDC ({pnl_sign}{pnl_pct:.1f}%)"
        else:
            pnl_str = "⏳ Prix indisponible"

        lines.append(
            f"{'🟢' if t.side.value == 'buy' else '🔴'} **{q}**{paper}\n"
            f"   🕐 {time_str} | Entry: {entry_price:.2f}\n"
            f"   💵 {invested:.2f} USDC | {shares:.1f} shares\n"
            f"   {pnl_str}\n"
        )

    # Total PNL
    total_pnl = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0
    total_sign = "+" if total_pnl >= 0 else ""
    lines.append(
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 **Total investi** : {total_invested:.2f} USDC\n"
        f"📊 **PNL total** : {total_sign}{total_pnl:.2f} USDC ({total_sign}{total_pnl_pct:.1f}%)"
    )

    text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("🔄 Rafraîchir", callback_data="menu_positions")],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]
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
        text = (
            "📜 **HISTORIQUE**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_Historique = tous les trades passés (achats,_\n"
            "_ventes, réussis, échoués, annulés)._\n\n"
            "Aucun trade enregistré."
        )
    else:
        status_emoji = {"filled": "✅", "failed": "❌", "cancelled": "🚫", "pending": "🟡", "executing": "🔄"}
        lines = [
            "📜 **HISTORIQUE**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_Historique = tous les trades passés (achats,_\n"
            "_ventes, réussis, échoués, annulés)._\n"
        ]
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
        "❓ **AIDE — WENPOLYMARKET**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "**Depuis le menu principal :**\n\n"
        "👛 **Wallets** — Voir vos soldes (USDC, POL)\n"
        "📊 **Positions** — Paris ouverts (achetés, pas encore résolus)\n"
        "📜 **Historique** — Tous les trades passés (achats, ventes, échecs…)\n"
        "💳 **Déposer** — Carte, exchange, bridge\n"
        "💸 **Retirer** — Envoyer vos USDC ailleurs\n"
        "👥 **Traders suivis** — Wallets copiés\n"
        "📡 **Dashboard** — Activité LIVE des traders suivis sur Polymarket\n"
        "📋 **Récap** — Ce que le bot a copié pour vous (PNL, win rate)\n"
        "⚙️ **Paramètres** — Capital, sizing, risques, traders\n\n"
        "**📊 Positions vs 📜 Historique :**\n"
        "• _Positions_ = vos paris encore ouverts (non résolus)\n"
        "• _Historique_ = tout ce qui s'est passé (buy, sell, réussis ou non)\n"
        "• _Dashboard_ = ce que vos traders font sur Polymarket\n"
        "• _Récap_ = ce que le bot a copié pour vous\n\n"
        "**Comment ça marche :**\n"
        "1. Configurez un wallet Polygon\n"
        "2. Déposez des USDC dessus\n"
        "3. Choisissez vos traders dans Paramètres\n"
        "4. Les trades sont copiés automatiquement\n"
        "5. Frais : 1% par trade copié\n\n"
        "🔒 Clés chiffrées AES-256 • Jamais exposées en clair\n"
        "📝 Paper Trading activé par défaut (sans fonds réels)"
    )

    keyboard = [[InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")]]
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── Switch wallet ──────────────────────────────────

async def menu_switch_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche la liste des wallets enregistrés pour choisir lequel activer."""
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        wallets = [w for w in (user.wallets or []) if w.chain == "polygon"]
        primary = user.wallet_address or ""

    if len(wallets) < 2:
        await query.edit_message_text(
            "ℹ️ Vous n'avez qu'un seul wallet enregistré.\n"
            "Utilisez « 🧭 Ajouter un nouveau wallet » pour en ajouter un autre.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Retour", callback_data="menu_balance")]]
            ),
        )
        return

    lines = ["🔀 **CHANGER DE WALLET ACTIF**\n━━━━━━━━━━━━━━━━━━━━\n"]
    keyboard = []
    for w in wallets:
        short = f"{w.address[:6]}...{w.address[-4:]}"
        is_active = w.address.lower() == primary.lower()
        label_tag = " ✅ actif" if is_active else ""
        created = " (créé par bot)" if w.auto_created else " (importé)"
        lines.append(f"• `{short}`{created}{label_tag}")
        if not is_active:
            keyboard.append([
                InlineKeyboardButton(
                    f"🔄 Activer {short}",
                    callback_data=f"switch_wallet_{w.id}",
                )
            ])

    keyboard.append(
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_balance")]
    )

    await query.edit_message_text(
        "\n".join(lines) + "\n\nSélectionnez le wallet à activer :",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def switch_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Active un wallet existant comme wallet principal."""
    query = update.callback_query
    await query.answer()

    wallet_id = int(query.data.replace("switch_wallet_", ""))

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        # Trouver le wallet cible
        target = None
        for w in (user.wallets or []):
            if w.id == wallet_id and w.chain == "polygon":
                target = w
                break

        if not target:
            await query.edit_message_text(
                "❌ Wallet introuvable.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Retour", callback_data="menu_balance")]]
                ),
            )
            return

        # Vérifier que le wallet cible a bien une clé chiffrée
        if not target.encrypted_key:
            logger.warning(
                f"UserWallet {target.id} has no encrypted_key — "
                f"cannot switch to {target.address[:10]}..."
            )
            await query.edit_message_text(
                "❌ Ce wallet n'a pas de clé privée enregistrée.\n"
                "Réimportez-le via « Ajouter un wallet ».",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Retour", callback_data="menu_balance")]]
                ),
            )
            return

        # Désactiver tous les wallets polygon
        for w in user.wallets:
            if w.chain == "polygon":
                w.is_primary = False

        # Activer le nouveau
        target.is_primary = True
        user.wallet_address = target.address
        user.encrypted_private_key = target.encrypted_key
        user.wallet_auto_created = target.auto_created
        await session.commit()

        logger.info(
            f"User {user.telegram_id} switched to wallet "
            f"{target.address[:10]}... (UserWallet #{target.id})"
        )

        short = f"{target.address[:6]}...{target.address[-4:]}"

    await query.edit_message_text(
        f"✅ **Wallet activé !**\n\n"
        f"📬 Wallet actif : `{short}`\n\n"
        "Ce wallet sera maintenant utilisé pour le copy-trading.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👛 Wallets", callback_data="menu_balance")],
            [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
        ]),
    )


# ── Delete wallet ─────────────────────────────────

async def menu_delete_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche la liste de TOUS les wallets pour suppression (y compris l'actif)."""
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        wallets = [w for w in (user.wallets or []) if w.chain == "polygon"]
        primary = user.wallet_address or ""

    if not wallets:
        await query.edit_message_text(
            "ℹ️ Aucun wallet enregistré.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Retour", callback_data="menu_balance")]]
            ),
        )
        return

    lines = ["🗑️ **SUPPRIMER UN WALLET**\n━━━━━━━━━━━━━━━━━━━━\n"]
    keyboard = []
    for w in wallets:
        short = f"{w.address[:6]}...{w.address[-4:]}"
        is_active = w.address.lower() == primary.lower()
        tag = " ✅ actif" if is_active else ""
        created = " (créé)" if w.auto_created else " (importé)"
        lines.append(f"• `{short}`{created}{tag}")
        label = f"🗑️ Supprimer {short}" + (" ⚠️" if is_active else "")
        keyboard.append([
            InlineKeyboardButton(
                label,
                callback_data=f"delwallet_confirm_{w.id}",
            )
        ])

    keyboard.append(
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_balance")]
    )

    await query.edit_message_text(
        "\n".join(lines) + "\n\nSélectionnez le wallet à supprimer :",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def delete_wallet_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Demande confirmation avant suppression (wallet actif inclus)."""
    query = update.callback_query
    await query.answer()

    wallet_id = int(query.data.replace("delwallet_confirm_", ""))

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        target = None
        for w in (user.wallets or []):
            if w.id == wallet_id and w.chain == "polygon":
                target = w
                break

        if not target:
            await query.edit_message_text(
                "❌ Wallet introuvable.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Retour", callback_data="menu_balance")]]
                ),
            )
            return

        short = f"{target.address[:6]}...{target.address[-4:]}"
        is_active = target.address.lower() == (user.wallet_address or "").lower()

    if is_active:
        warning = (
            f"🚨 **ATTENTION — WALLET ACTIF**\n\n"
            f"Wallet : `{short}`\n\n"
            "C'est votre wallet **principal** utilisé par le bot.\n"
            "Le supprimer va :\n"
            "• Arrêter le copy-trading\n"
            "• Supprimer la clé privée de nos serveurs\n"
            "• Remettre le bot en état « non configuré »\n\n"
            "⚠️ **Vos fonds restent sur la blockchain** — "
            "seule la clé est supprimée de notre système.\n\n"
            "Vous êtes sûr(e) ?"
        )
    else:
        warning = (
            f"⚠️ **Confirmer la suppression ?**\n\n"
            f"Wallet : `{short}`\n\n"
            "Cette action est irréversible. La clé privée chiffrée "
            "sera supprimée de nos serveurs."
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Oui, supprimer", callback_data=f"delwallet_exec_{wallet_id}"
            ),
            InlineKeyboardButton("❌ Annuler", callback_data="menu_balance"),
        ]
    ]
    await query.edit_message_text(
        warning,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def delete_wallet_exec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Supprime définitivement un wallet (actif ou non)."""
    query = update.callback_query
    await query.answer()

    wallet_id = int(query.data.replace("delwallet_exec_", ""))

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        target = None
        for w in (user.wallets or []):
            if w.id == wallet_id and w.chain == "polygon":
                target = w
                break

        if not target:
            await query.edit_message_text("❌ Wallet introuvable.")
            return

        short = f"{target.address[:6]}...{target.address[-4:]}"
        is_active = target.address.lower() == (user.wallet_address or "").lower()

        # Delete the wallet record
        await session.delete(target)

        if is_active:
            # Check if there's another polygon wallet to promote
            remaining = [
                w for w in (user.wallets or [])
                if w.chain == "polygon" and w.id != wallet_id
            ]

            if remaining:
                # Promote the first remaining wallet as active
                new_primary = remaining[0]
                new_primary.is_primary = True
                user.wallet_address = new_primary.address
                user.encrypted_private_key = new_primary.encrypted_key
                user.wallet_auto_created = new_primary.auto_created
                new_short = f"{new_primary.address[:6]}...{new_primary.address[-4:]}"
                extra_msg = (
                    f"\n\n🔄 Wallet actif basculé sur `{new_short}`."
                )
            else:
                # No remaining wallet — reset user to unconfigured state
                user.wallet_address = None
                user.encrypted_private_key = None
                user.wallet_auto_created = False
                user.polymarket_approved = False
                extra_msg = (
                    "\n\n📭 Plus aucun wallet configuré.\n"
                    "Utilisez « 🧭 Configurer mon wallet » pour en ajouter un."
                )
        else:
            extra_msg = ""

        await session.commit()

        logger.info(
            f"User {user.telegram_id} deleted wallet "
            f"{target.address[:10]}... (UserWallet #{wallet_id})"
            f"{' [was active]' if is_active else ''}"
        )

    await query.edit_message_text(
        f"✅ Wallet `{short}` supprimé.\n\n"
        f"La clé privée chiffrée a été effacée de nos serveurs.{extra_msg}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👛 Wallets", callback_data="menu_balance")],
            [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
        ]),
    )


# ── Dashboard : positions RÉELLES des traders suivis ───

async def menu_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dashboard = ce que les traders suivis font réellement sur Polymarket.

    Appelle l'API Polymarket (Data API) pour chaque trader et affiche
    positions actuelles + trades récents (24h). Permet de vérifier
    qu'on n'a raté aucun trade.
    """
    query = update.callback_query
    await query.answer("⏳ Chargement des positions…")

    from bot.services.polymarket import polymarket_client
    import time

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return
        us = await get_or_create_settings(session, user)
        followed = us.followed_wallets or []

    if not followed:
        await query.edit_message_text(
            "📡 **DASHBOARD — TRADERS SUIVIS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Aucun trader suivi.\n"
            "Ajoutez des traders dans ⚙️ Paramètres.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Paramètres", callback_data="menu_settings")],
                [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
            ]),
        )
        return

    lines = [
        "📡 **DASHBOARD — TRADERS SUIVIS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "_Positions & trades des comptes suivis_\n"
        "_sur Polymarket en temps réel._\n",
    ]

    total_positions = 0
    total_trades_24h = 0
    since_24h = int(time.time()) - 86400

    for wallet in followed:
        w_short = f"{wallet[:6]}...{wallet[-4:]}"
        positions = await polymarket_client.get_positions_by_address(wallet)
        activity = await polymarket_client.get_activity_by_address(
            wallet, limit=20, start=since_24h
        )

        n_pos = len(positions)
        n_trades = len(activity)
        total_positions += n_pos
        total_trades_24h += n_trades

        lines.append(f"👤 `{w_short}` — **{n_pos}** pos / **{n_trades}** trades 24h")

        if not positions and not activity:
            lines.append("   _Aucune activité_\n")
            continue

        # ── Positions ouvertes ──
        if positions:
            positions.sort(key=lambda p: p.size, reverse=True)

            trader_pnl = 0.0
            for p in positions:
                if p.avg_price > 0:
                    trader_pnl += (p.current_price - p.avg_price) * p.size
            pnl_emoji = "🟢" if trader_pnl >= 0 else "🔴"
            lines.append(f"   {pnl_emoji} P&L positions : **{trader_pnl:+.2f} USDC**")

            for p in positions[:5]:
                pnl_e = "📈" if p.pnl_pct >= 0 else "📉"
                outcome = p.outcome or "?"
                val = p.size * p.current_price
                lines.append(
                    f"   {pnl_e} **{outcome}** @ {p.current_price:.2f} "
                    f"({p.pnl_pct:+.1f}%) • {val:.1f}$"
                )
            if len(positions) > 5:
                lines.append(f"   _… +{len(positions) - 5} autres positions_")

        # ── Trades récents (24h) ──
        if activity:
            lines.append("   ─ Trades 24h ─")
            for a in activity[:5]:
                side_e = "🟢" if a.side == "BUY" else "🔴"
                title = a.title[:35] + "…" if len(a.title) > 35 else a.title
                lines.append(
                    f"   {side_e} {a.side} **{a.outcome}** • "
                    f"{a.usdc_size:.1f}$ @ {a.price:.2f}"
                )
                if title:
                    lines.append(f"      _{title}_")
            if len(activity) > 5:
                lines.append(f"   _… +{len(activity) - 5} autres trades_")

        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"📊 **{total_positions} positions** / "
        f"**{total_trades_24h} trades 24h** "
        f"sur {len(followed)} trader(s)"
    )
    lines.append(
        "\n_Compare avec 📋 Récap pour vérifier_\n"
        "_que le bot a bien copié tous les trades._"
    )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n\n_… tronqué_"

    keyboard = [
        [
            InlineKeyboardButton("🔄 Rafraîchir", callback_data="menu_dashboard"),
            InlineKeyboardButton("📋 Récap", callback_data="menu_recap"),
        ],
        [InlineKeyboardButton("📄 Rapport PDF", callback_data="paper_report")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
    ]
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── Récap : trades copiés par le bot pour l'utilisateur ───

async def menu_recap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Récap = ce que LE BOT a copié pour l'utilisateur.

    Montre les trades exécutés par trader suivi, avec volume,
    win rate et P&L. À comparer avec le Dashboard pour voir
    si rien n'a été raté.
    """
    query = update.callback_query
    await query.answer()

    from datetime import datetime, timezone, timedelta
    from bot.models.trade import Trade, TradeStatus, TradeSide

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        us = await get_or_create_settings(session, user)
        followed = us.followed_wallets or []

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())

        all_trades = (await session.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
            ).order_by(Trade.created_at.desc())
        )).scalars().all()

    if not followed:
        await query.edit_message_text(
            "📋 **RÉCAP — MES COPIES**\n━━━━━━━━━━━━━━━━━━━━\n\n"
            "Aucun trader suivi.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
            ]),
        )
        return

    # --- Stats globales ---
    trades_today = [t for t in all_trades if t.created_at >= today_start]
    trades_week = [t for t in all_trades if t.created_at >= week_start]
    vol_today = sum(t.gross_amount_usdc for t in trades_today)
    vol_week = sum(t.gross_amount_usdc for t in trades_week)

    # P&L global
    buy_avg: dict[str, list[float]] = {}
    for t in all_trades:
        if t.side == TradeSide.BUY:
            buy_avg.setdefault(t.token_id, []).append(t.price)
    total_pnl = 0.0
    wins = 0
    closed = 0
    for t in all_trades:
        if t.side == TradeSide.SELL and t.token_id in buy_avg:
            avg = sum(buy_avg[t.token_id]) / len(buy_avg[t.token_id])
            pnl = (t.price - avg) * t.shares
            total_pnl += pnl
            closed += 1
            if pnl > 0:
                wins += 1
    global_wr = f"{(wins / closed) * 100:.0f}%" if closed > 0 else "N/A"
    global_pnl = f"{total_pnl:+.2f}" if closed > 0 else "N/A"

    lines = [
        "📋 **RÉCAP — MES TRADES COPIÉS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "_Ce que le bot a exécuté pour vous._\n",
        f"📅 Aujourd'hui : **{len(trades_today)}** trades • **{vol_today:.2f}** USDC",
        f"📆 Cette semaine : **{len(trades_week)}** trades • **{vol_week:.2f}** USDC",
        f"📊 Win rate : **{global_wr}** • P&L réalisé : **{global_pnl} USDC**",
        f"_⚠️ P&L = uniquement les trades fermés (vendus/résolus)._\n",
        "━━━━━━━━━━━━━━━━━━━━",
        "**DÉTAIL PAR TRADER COPIÉ**\n",
    ]

    for wallet in followed:
        w_short = f"{wallet[:6]}...{wallet[-4:]}"
        w_lower = wallet.lower()

        trader_trades = [
            t for t in all_trades
            if t.master_wallet and t.master_wallet.lower() == w_lower
        ]
        t_today = [t for t in trader_trades if t.created_at >= today_start]
        t_week = [t for t in trader_trades if t.created_at >= week_start]
        t_vol = sum(t.gross_amount_usdc for t in trader_trades)

        # Win rate par trader
        t_buy_avg: dict[str, list[float]] = {}
        for t in trader_trades:
            if t.side == TradeSide.BUY:
                t_buy_avg.setdefault(t.token_id, []).append(t.price)
        t_pnl = 0.0
        t_wins = 0
        t_closed = 0
        for t in trader_trades:
            if t.side == TradeSide.SELL and t.token_id in t_buy_avg:
                avg = sum(t_buy_avg[t.token_id]) / len(t_buy_avg[t.token_id])
                pnl = (t.price - avg) * t.shares
                t_pnl += pnl
                t_closed += 1
                if pnl > 0:
                    t_wins += 1
        t_wr = f"{(t_wins / t_closed) * 100:.0f}%" if t_closed > 0 else "—"
        t_pnl_str = f"{t_pnl:+.2f}" if t_closed > 0 else "—"

        lines.append(f"👤 `{w_short}`")
        lines.append(
            f"   📅 Jour : {len(t_today)} • 📆 Sem : {len(t_week)} • "
            f"Vol : {t_vol:.2f}"
        )
        lines.append(f"   WR : {t_wr} • P&L : {t_pnl_str} USDC")

        # Derniers trades (max 4)
        for rt in trader_trades[:4]:
            side_emoji = "🟢" if rt.side == TradeSide.BUY else "🔴"
            q = rt.market_question or rt.market_id
            if len(q) > 28:
                q = q[:25] + "..."
            date_s = rt.created_at.strftime("%d/%m %H:%M")
            paper = " 📝" if rt.is_paper else ""
            lines.append(
                f"   {side_emoji} {date_s} | "
                f"{rt.net_amount_usdc:.2f} USDC{paper}"
            )
            lines.append(f"      _{q}_")
        lines.append("")

    lines.append(
        "_Compare avec 📡 Dashboard pour vérifier_\n"
        "_qu'aucun trade n'a été raté._"
    )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n\n_… tronqué_"

    keyboard = [
        [
            InlineKeyboardButton("📡 Dashboard", callback_data="menu_dashboard"),
            InlineKeyboardButton("🔄 Rafraîchir", callback_data="menu_recap"),
        ],
        [InlineKeyboardButton("📄 Rapport PDF", callback_data="paper_report")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
    ]
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── Paper Wallet ─────────────────────────────────────

async def menu_paper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Paper trading wallet overview — balance, unrealized PNL, settled results."""
    query = update.callback_query
    await query.answer("⏳ Calcul du portefeuille…")

    from bot.models.trade import Trade, TradeStatus, TradeSide
    from bot.services.polymarket import polymarket_client
    import asyncio

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        # Paper trades — open (unsettled) and settled
        result_open = await session.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.is_paper == True,  # noqa: E712
                Trade.is_settled == False,  # noqa: E712
                Trade.status == TradeStatus.FILLED,
                Trade.side == TradeSide.BUY,
            ).order_by(Trade.created_at.desc()).limit(20)
        )
        open_trades = list(result_open.scalars().all())

        result_settled = await session.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.is_paper == True,  # noqa: E712
                Trade.is_settled == True,  # noqa: E712
            ).order_by(Trade.created_at.desc()).limit(20)
        )
        settled_trades = list(result_settled.scalars().all())

        paper_balance = user.paper_balance
        paper_initial = user.paper_initial_balance

    # ── Fetch current prices for open positions (unrealized PNL) ──
    current_prices: dict[str, float] = {}
    if open_trades:
        unique_tokens = {t.token_id for t in open_trades}

        async def _fetch_price(token_id: str) -> tuple[str, float]:
            try:
                price = await polymarket_client.get_price(token_id)
                return token_id, price
            except Exception:
                return token_id, 0.0

        price_results = await asyncio.gather(
            *[_fetch_price(tid) for tid in unique_tokens],
            return_exceptions=True,
        )
        for res in price_results:
            if isinstance(res, tuple):
                current_prices[res[0]] = res[1]

    # ── Calculate unrealized PNL ──
    total_invested = 0.0
    total_current_value = 0.0
    for t in open_trades:
        invested = t.net_amount_usdc
        shares = t.shares or (invested / t.price if t.price > 0 else 0)
        cur_price = current_prices.get(t.token_id, 0)
        current_value = shares * cur_price if cur_price > 0 else invested
        total_invested += invested
        total_current_value += current_value

    unrealized_pnl = total_current_value - total_invested
    unrealized_pct = (unrealized_pnl / total_invested * 100) if total_invested > 0 else 0

    # ── Portfolio total = cash + positions value ──
    portfolio_value = paper_balance + total_current_value
    total_pnl = portfolio_value - paper_initial
    pnl_pct = (total_pnl / paper_initial * 100) if paper_initial > 0 else 0
    pnl_sign = "+" if total_pnl >= 0 else ""
    pnl_emoji = "📈" if total_pnl >= 0 else "📉"

    lines = [
        "📝 **PAPER WALLET**\n━━━━━━━━━━━━━━━━━━━━\n",
        f"🏁 Capital initial : **{paper_initial:.2f} USDC**\n",
        f"💵 Cash disponible : **{paper_balance:.2f} USDC**",
        f"📊 Positions ouvertes : **{total_current_value:.2f} USDC**"
        f" ({len(open_trades)} pos.)",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"💼 **Portefeuille total : {portfolio_value:.2f} USDC**",
        f"{pnl_emoji} **PNL total : {pnl_sign}{total_pnl:.2f} USDC "
        f"({pnl_sign}{pnl_pct:.1f}%)**\n",
    ]

    # ── Open positions with unrealized PNL ──
    if open_trades:
        ur_sign = "+" if unrealized_pnl >= 0 else ""
        ur_emoji = "📈" if unrealized_pnl >= 0 else "📉"
        lines.append(
            f"📌 **Positions ouvertes** ({len(open_trades)}) — "
            f"{ur_emoji} {ur_sign}{unrealized_pnl:.2f} USDC "
            f"({ur_sign}{unrealized_pct:.1f}%)"
        )
        for t in open_trades[:10]:
            q = t.market_question or t.market_id
            if len(q) > 35:
                q = q[:32] + "..."
            shares = t.shares or 0
            invested = t.net_amount_usdc
            entry_price = t.price
            cur_price = current_prices.get(t.token_id, 0)
            current_val = shares * cur_price if cur_price > 0 else 0
            pos_pnl = current_val - invested
            pos_pct = (pos_pnl / invested * 100) if invested > 0 else 0

            time_str = t.created_at.strftime("%d/%m %H:%M") if t.created_at else "?"

            if cur_price > 0:
                p_sign = "+" if pos_pnl >= 0 else ""
                p_emoji = "📈" if pos_pnl >= 0 else "📉"
                pnl_str = f"{p_emoji} {p_sign}{pos_pnl:.2f} ({p_sign}{pos_pct:.0f}%)"
            else:
                pnl_str = "⏳ prix indispo."

            lines.append(
                f"\n   🟢 **{q}**\n"
                f"   {time_str} | {invested:.2f}→{current_val:.2f} USDC | {pnl_str}\n"
                f"   Entry: {entry_price:.2f} → Now: {cur_price:.2f} | {shares:.1f} shares"
            )
    else:
        lines.append("\n📌 _Aucune position ouverte._")

    # ── Settled positions summary ──
    if settled_trades:
        wins = sum(1 for t in settled_trades if (t.settlement_pnl or 0) > 0)
        losses = sum(1 for t in settled_trades if (t.settlement_pnl or 0) <= 0)
        total_settled_pnl = sum(t.settlement_pnl or 0 for t in settled_trades)
        wr = (wins / len(settled_trades) * 100) if settled_trades else 0

        lines.append(
            f"\n🏁 **Trades résolus** : {wins}W / {losses}L "
            f"(WR {wr:.0f}%) • PNL réalisé {total_settled_pnl:+.2f} USDC"
        )
        for t in settled_trades[:5]:
            q = t.market_question or t.market_id
            if len(q) > 35:
                q = q[:32] + "..."
            pnl = t.settlement_pnl or 0
            emoji = "✅" if pnl > 0 else "❌"
            lines.append(
                f"   {emoji} {pnl:+.2f} USDC | {t.net_amount_usdc:.2f} misé — _{q}_"
            )

    lines.append(
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "💡 Prix mis à jour en temps réel depuis Polymarket.\n"
        "Les paris se règlent quand le marché est résolu."
    )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n\n_… tronqué_"

    keyboard = [
        [
            InlineKeyboardButton("🔄 Rafraîchir", callback_data="menu_paper"),
            InlineKeyboardButton("📄 Rapport PDF", callback_data="paper_report"),
        ],
        [
            InlineKeyboardButton("💰 Changer solde", callback_data="paper_set_balance"),
            InlineKeyboardButton("🔁 Réinitialiser", callback_data="paper_reset"),
        ],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def paper_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and send a PDF performance report."""
    query = update.callback_query
    await query.answer("⏳ Génération du rapport PDF…")

    from bot.models.trade import Trade, TradeStatus
    from bot.services.polymarket import polymarket_client
    from bot.services.report import build_report_data, generate_report_pdf
    import asyncio

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        us = await get_or_create_settings(session, user)

        # Fetch all FILLED trades
        result = await session.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
            ).order_by(Trade.created_at.desc())
        )
        trades = list(result.scalars().all())

    # Fetch current prices for open positions
    current_prices: dict[str, float] = {}
    open_token_ids = {
        t.token_id for t in trades
        if not t.is_settled and t.side.value == "buy"
    }

    if open_token_ids:
        async def _fetch_price(token_id: str) -> tuple[str, float]:
            try:
                price = await polymarket_client.get_price(token_id)
                return token_id, price
            except Exception:
                return token_id, 0.0

        results = await asyncio.gather(
            *[_fetch_price(tid) for tid in open_token_ids],
            return_exceptions=True,
        )
        for res in results:
            if isinstance(res, tuple):
                current_prices[res[0]] = res[1]

    # Build report data and generate PDF
    report_data = await build_report_data(user, us, trades, current_prices)
    pdf_buffer = generate_report_pdf(report_data)

    # Send PDF
    from datetime import datetime, timezone
    filename = (
        f"wenpolymarket_report_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.pdf"
    )

    await query.message.reply_document(
        document=pdf_buffer,
        filename=filename,
        caption=(
            f"📄 **Rapport de performance WENPOLYMARKET**\n"
            f"{'📝 Paper Trading' if user.paper_trading else '💵 Live Trading'}\n"
            f"💼 Portefeuille : {report_data.portfolio_value:.2f} USDC\n"
            f"{'📈' if report_data.total_pnl >= 0 else '📉'} "
            f"PNL : {'+' if report_data.total_pnl >= 0 else ''}"
            f"{report_data.total_pnl:.2f} USDC "
            f"({'+' if report_data.total_pnl_pct >= 0 else ''}"
            f"{report_data.total_pnl_pct:.1f}%)"
        ),
        parse_mode="Markdown",
    )


async def paper_set_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt user to set paper initial balance."""
    query = update.callback_query
    await query.answer()

    text = (
        "💰 **SOLDE PAPER — CHANGER LE MONTANT INITIAL**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Envoyez le montant en USDC pour votre solde initial paper.\n"
        "Ex: `5000` pour démarrer avec 5000 USDC virtuels.\n\n"
        "⚠️ Cela réinitialisera votre solde ET votre historique paper."
    )
    keyboard = [
        [
            InlineKeyboardButton("500 USDC", callback_data="paper_init_500"),
            InlineKeyboardButton("1000 USDC", callback_data="paper_init_1000"),
        ],
        [
            InlineKeyboardButton("5000 USDC", callback_data="paper_init_5000"),
            InlineKeyboardButton("10000 USDC", callback_data="paper_init_10000"),
        ],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_paper")],
    ]
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def paper_init_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set paper balance to a preset amount."""
    query = update.callback_query
    await query.answer()

    amount_str = query.data.replace("paper_init_", "")
    try:
        amount = float(amount_str)
    except ValueError:
        await query.edit_message_text("❌ Montant invalide.")
        return

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        user.paper_balance = amount
        user.paper_initial_balance = amount

        # Reset settled status on all paper trades (fresh start)
        from bot.models.trade import Trade
        result = await session.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.is_paper == True,  # noqa: E712
            )
        )
        for trade in result.scalars().all():
            trade.is_settled = True  # Mark as settled to close them out
            if trade.settlement_pnl is None:
                trade.settlement_pnl = 0.0

        await session.commit()

    await query.edit_message_text(
        f"✅ **Paper wallet réinitialisé !**\n\n"
        f"💰 Nouveau solde : **{amount:.0f} USDC**\n"
        f"🏁 Solde initial : **{amount:.0f} USDC**\n\n"
        "Les anciens trades paper ont été archivés.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Paper Wallet", callback_data="menu_paper")],
            [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
        ]),
    )


async def paper_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset paper balance to initial amount without changing the initial amount."""
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        initial = user.paper_initial_balance
        user.paper_balance = initial

        # Mark all open paper trades as settled
        from bot.models.trade import Trade, TradeStatus
        result = await session.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.is_paper == True,  # noqa: E712
                Trade.is_settled == False,  # noqa: E712
            )
        )
        for trade in result.scalars().all():
            trade.is_settled = True
            if trade.settlement_pnl is None:
                trade.settlement_pnl = 0.0

        await session.commit()

    await query.edit_message_text(
        f"🔁 **Paper wallet réinitialisé !**\n\n"
        f"💰 Solde remis à : **{initial:.0f} USDC**\n"
        "Toutes les positions paper ont été fermées.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Paper Wallet", callback_data="menu_paper")],
            [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
        ]),
    )


# ── Export private key (ephemeral, auto-delete after 60s) ──────────

async def export_pk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export the user's private key in an auto-deleting message.

    SECURITY:
    - The PK is shown in a SEPARATE message (not edit) so it can be deleted
    - Auto-deletes after 60 seconds
    - Logs a warning for audit trail
    """
    import asyncio
    from bot.services.crypto import decrypt_private_key
    from bot.config import settings as app_settings

    query = update.callback_query
    await query.answer()

    tg_user = query.from_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user or not user.encrypted_private_key:
            await query.edit_message_text(
                "❌ Aucune clé privée enregistrée pour ce wallet."
            )
            return

        try:
            pk = decrypt_private_key(
                user.encrypted_private_key,
                app_settings.encryption_key,
                user.uuid,
            )
        except Exception as e:
            logger.error(f"Failed to decrypt PK for export (user {tg_user.id}): {e}")
            await query.edit_message_text(
                "❌ Erreur lors du déchiffrement. Contactez le support."
            )
            return

    logger.warning(
        f"⚠️ SECURITY: User {tg_user.id} exported private key "
        f"for wallet {user.wallet_address}"
    )

    # Send PK in a separate message that will be auto-deleted
    pk_msg = await query.message.reply_text(
        "🔑 **CLÉE PRIVÉE — AUTO-SUPPRESSION DANS 60 SECONDES**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"`{pk}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ **Copiez-la MAINTENANT** dans un gestionnaire de "
        "mots de passe (Bitwarden, 1Password, etc.)\n\n"
        "🚫 Ne la partagez **JAMAIS** — ni sur Discord, ni par "
        "message, ni par email.\n\n"
        "⏱️ Ce message sera **automatiquement supprimé** dans 60 secondes.",
        parse_mode="Markdown",
    )

    # Update original message to remove the export button
    keyboard = [
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]
    await query.edit_message_text(
        "✅ Clé privée envoyée ci-dessous.\n\n"
        "⏱️ Le message sera auto-supprimé dans **60 secondes**.\n"
        "Copiez-la dans un endroit sûr maintenant.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    # Wipe PK from memory
    del pk

    # Auto-delete after 60 seconds
    async def _auto_delete():
        await asyncio.sleep(60)
        try:
            await pk_msg.delete()
        except Exception:
            pass  # Message may already be deleted by user

    asyncio.create_task(_auto_delete())


# ── Stop Copy ───────────────────────────────────────

async def stop_copy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop copytrading — pauses paper if in paper mode, live if in live mode."""
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        mode = "📝 Paper" if user.paper_trading else "💵 Live"

        if user.is_paused:
            await query.edit_message_text(
                f"⚠️ Le copytrading ({mode}) est déjà en pause.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
            return

        user.is_paused = True
        await session.commit()

    await query.edit_message_text(
        f"🛑 **Copytrading arrêté**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Mode : {mode}\n"
        f"Le bot ne copiera plus aucun trade tant que\n"
        f"vous ne relancerez pas le copy.\n\n"
        f"Pour reprendre, allez dans ⚙️ **Paramètres** → Reprendre\n"
        f"ou utilisez la commande /resume.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Reprendre le copy", callback_data="resume_copy")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
        ]),
    )


async def resume_copy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume copytrading after stop."""
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return

        mode = "📝 Paper" if user.paper_trading else "💵 Live"
        user.is_paused = False
        await session.commit()

    await query.edit_message_text(
        f"▶️ **Copytrading relancé !**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Mode : {mode}\n"
        f"Le bot copie à nouveau les trades de vos traders suivis.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
        ]),
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

        text, keyboard = _build_main_menu_content(tg_user, user)

        # Add last copied trade per followed trader
        us = await get_or_create_settings(session, user)
        followed = us.followed_wallets or []
        if followed:
            from bot.models.trade import Trade, TradeStatus
            last_trades_lines: list[str] = []
            for wallet in followed:
                w_short = f"{wallet[:6]}...{wallet[-4:]}"
                result = await session.execute(
                    select(Trade).where(
                        Trade.user_id == user.id,
                        Trade.master_wallet == wallet,
                        Trade.status == TradeStatus.FILLED,
                    ).order_by(desc(Trade.created_at)).limit(1)
                )
                last = result.scalar_one_or_none()
                if last and last.created_at:
                    dt = last.created_at.strftime("%d/%m %H:%M")
                    side = "🟢 BUY" if last.side.value == "buy" else "🔴 SELL"
                    q = last.market_question or last.market_id or "?"
                    if len(q) > 30:
                        q = q[:27] + "..."
                    last_trades_lines.append(
                        f"  `{w_short}` → {side} {dt}\n"
                        f"    _{q}_ • {last.net_amount_usdc:.2f}$"
                    )
                else:
                    last_trades_lines.append(
                        f"  `{w_short}` → _Aucun trade copié_"
                    )
            text += (
                "\n📡 **Dernière activité copiée :**\n"
                + "\n".join(last_trades_lines)
                + "\n"
            )

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
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
        CallbackQueryHandler(menu_dashboard, pattern="^menu_dashboard$"),
        CallbackQueryHandler(menu_recap, pattern="^menu_recap$"),
        CallbackQueryHandler(menu_switch_wallet, pattern="^menu_switch_wallet$"),
        CallbackQueryHandler(switch_wallet_callback, pattern=r"^switch_wallet_\d+$"),
        CallbackQueryHandler(menu_delete_wallet, pattern="^menu_delete_wallet$"),
        CallbackQueryHandler(delete_wallet_confirm, pattern=r"^delwallet_confirm_\d+$"),
        CallbackQueryHandler(delete_wallet_exec, pattern=r"^delwallet_exec_\d+$"),
        CallbackQueryHandler(menu_paper, pattern="^menu_paper$"),
        CallbackQueryHandler(paper_report, pattern="^paper_report$"),
        CallbackQueryHandler(paper_set_balance, pattern="^paper_set_balance$"),
        CallbackQueryHandler(paper_init_callback, pattern=r"^paper_init_\d+$"),
        CallbackQueryHandler(paper_reset, pattern="^paper_reset$"),
        CallbackQueryHandler(export_pk, pattern="^export_pk$"),
        CallbackQueryHandler(stop_copy, pattern="^stop_copy$"),
        CallbackQueryHandler(resume_copy, pattern="^resume_copy$"),
        CallbackQueryHandler(menu_back, pattern="^menu_back$"),
    ]
