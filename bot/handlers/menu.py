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
            InlineKeyboardButton("📡 Activité traders", callback_data="menu_dashboard"),
            InlineKeyboardButton("📋 Mes copies", callback_data="menu_recap"),
        ],
        [
            InlineKeyboardButton("🔍 Scanner traders", callback_data="menu_scanner"),
            InlineKeyboardButton("📈 Analytics V3", callback_data="v3_analytics"),
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
        # BUY trades that are FILLED — split open vs settled
        result = await session.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
                Trade.side == TradeSide.BUY,
            ).order_by(Trade.created_at.desc()).limit(30)
        )
        all_trades = list(result.scalars().all())
        # Show open (unsettled) first, then recently settled
        open_trades = [t for t in all_trades if not t.is_settled]
        settled_recent = [t for t in all_trades if t.is_settled][:5]
        trades = open_trades + settled_recent

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
        invested = t.net_amount_usdc
        shares = t.shares if t.shares else (invested / entry_price if entry_price > 0 else 0)

        if t.is_settled and t.settlement_pnl is not None:
            # Settled — show final result
            pnl_usdc = t.settlement_pnl
            won = pnl_usdc >= 0
            payout = invested + pnl_usdc
            total_invested += invested
            total_current += payout
            result_str = "GAGNÉ ✅" if won else "PERDU ❌"
            outcome = t.market_outcome or "?"
            pnl_str = (
                f"🏆 {result_str} ({outcome}) • "
                f"P&L: {pnl_usdc:+.2f} USDC"
            )
        else:
            # Open position — fetch live price
            cur_price = current_prices.get(t.token_id, 0)
            current_value = shares * cur_price if cur_price > 0 else 0
            pnl_usdc = current_value - invested
            pnl_pct = (pnl_usdc / invested * 100) if invested > 0 else 0
            total_invested += invested
            total_current += current_value if cur_price > 0 else invested

            if cur_price > 0:
                pnl_emoji = "📈" if pnl_usdc >= 0 else "📉"
                pnl_str = f"{pnl_emoji} {pnl_usdc:+.2f} USDC ({pnl_pct:+.1f}%) • now: {cur_price:.2f}"
            else:
                pnl_str = f"⏳ En attente de résolution"

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
    """Hub unique : voir, analyser, filtrer, ajouter, retirer des traders."""
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return
        us = await get_or_create_settings(session, user)
        wallets = us.followed_wallets or []
        trader_filters = us.trader_filters or {}

    if not wallets:
        text = (
            "👥 **TRADERS SUIVIS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Aucun trader suivi_\n\n"
            "Ajoutez l'adresse Polygon (0x…) d'un trader\n"
            "Polymarket pour copier ses positions."
        )
        keyboard = [
            [InlineKeyboardButton("➕ Ajouter un trader", callback_data="set_followed")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
        ]
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # ── Build per-trader rows ──
    lines = []
    keyboard = []
    for i, w in enumerate(wallets):
        short = f"{w[:6]}...{w[-4:]}"
        excl = trader_filters.get(w.lower(), {}).get("excluded_categories", [])
        excl_badge = f" • 🚫 {len(excl)} filtre(s)" if excl else ""
        lines.append(f"  {i + 1}. `{short}`{excl_badge}")

        # 3 boutons par trader : Rapport | Filtres catégorie | Retirer
        keyboard.append([
            InlineKeyboardButton(f"📊 {short}", callback_data=f"trader_rpt_{w}"),
            InlineKeyboardButton("🎯", callback_data=f"trader_cats_{w}"),
            InlineKeyboardButton("❌", callback_data=f"mt_rm_{i}"),
        ])

    wallet_text = "\n".join(lines)
    text = (
        "👥 **TRADERS SUIVIS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{wallet_text}\n\n"
        "📊 Rapport détaillé\n"
        "🎯 Analyse catégories & filtres\n"
        "❌ Retirer le trader"
    )

    keyboard.append([
        InlineKeyboardButton("➕ Ajouter un trader", callback_data="set_followed"),
    ])
    keyboard.append([
        InlineKeyboardButton("📡 Dashboard", callback_data="menu_dashboard"),
        InlineKeyboardButton("🏠 Menu", callback_data="menu_back"),
    ])

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def menu_trader_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a followed trader directly from the Traders suivis hub."""
    query = update.callback_query

    idx_str = query.data.replace("mt_rm_", "")
    try:
        idx = int(idx_str)
    except ValueError:
        await query.answer("❌ Index invalide", show_alert=True)
        return

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return
        us = await get_or_create_settings(session, user)
        wallets = list(us.followed_wallets or [])

        if 0 <= idx < len(wallets):
            removed = wallets.pop(idx)
            us.followed_wallets = wallets

            # Also clean up trader_filters for removed wallet
            trader_filters = dict(us.trader_filters or {})
            trader_filters.pop(removed.lower(), None)
            us.trader_filters = trader_filters

            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(us, "followed_wallets")
            flag_modified(us, "trader_filters")
            await session.commit()

            await query.answer(
                f"✅ {removed[:6]}...{removed[-4:]} retiré", show_alert=False
            )
        else:
            await query.answer("❌ Index invalide", show_alert=True)
            return

    # Refresh the traders menu
    await menu_traders(update, context)


async def menu_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ce handler est conservé pour compatibilité mais la vraie ouverture
    des paramètres est gérée par la ConversationHandler /settings
    (qui intercepte directement le callback « menu_settings »)."""
    query = update.callback_query
    await query.answer()


async def menu_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    # Page 1 by default, page 2 if callback says so
    page = 3 if query.data == "menu_help_3" else (2 if query.data == "menu_help_2" else 1)

    if page == 1:
        text = (
            "❓ **GUIDE COMPLET — WENPOLYMARKET**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "**🏠 MENU PRINCIPAL**\n\n"
            "👛 **Wallets**\n"
            "  Vos soldes en temps réel (USDC, POL/MATIC).\n"
            "  Changer de wallet actif, en ajouter un nouveau.\n\n"
            "📊 **Positions**\n"
            "  Vos paris ouverts sur Polymarket (non résolus).\n"
            "  Prix d'entrée → prix actuel, PNL en cours.\n\n"
            "📜 **Historique**\n"
            "  Tous vos trades passés : achats, ventes,\n"
            "  réussis ou échoués, avec montants et dates.\n\n"
            "👥 **Traders suivis**\n"
            "  La liste des traders que vous copiez.\n"
            "  Pour chaque trader :\n"
            "  • 📊 Rapport détaillé (positions, activité, PNL)\n"
            "  • 🎯 Analyse par catégorie (BTC, ETH, Sports…)\n"
            "    + filtres pour exclure certaines catégories\n"
            "  • ❌ Retirer un trader\n"
            "  • ➕ Ajouter un nouveau trader\n\n"
            "⚙️ **Paramètres**\n"
            "  Capital alloué, mode de sizing (fixe, %,\n"
            "  proportionnel, Kelly), stop-loss, take-profit,\n"
            "  limites de trade, délai de copie.\n"
        )
        keyboard = [
            [InlineKeyboardButton("▶️ Page 2/3", callback_data="menu_help_2")],
            [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
        ]
    elif page == 2:
        text = (
            "❓ **GUIDE COMPLET — PAGE 2/3**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "**📡 Activité traders** (Dashboard)\n"
            "  Ce que vos traders font EN DIRECT sur\n"
            "  Polymarket. Vue globale de tous les traders :\n"
            "  activité par timeframe (1h/3h/5h/24h),\n"
            "  positions ouvertes, PNL. Rapports HTML + PDF.\n\n"
            "**📋 Mes copies** (Récap)\n"
            "  Ce que LE BOT a copié pour vous.\n"
            "  Vos trades copiés, PNL réalisé et non-réalisé,\n"
            "  win rate, statistiques. Rapports HTML + PDF.\n\n"
            "**🔍 Scanner traders**\n"
            "  Recherche intelligente de traders à suivre.\n"
            "  Filtrez par catégorie (Crypto, Sports, Politics…),\n"
            "  bénéfice (1D/1W/1M), volume, nombre de marchés.\n"
            "  Ajoutez un trader trouvé en un clic.\n\n"
            "**📝 Paper Wallet**\n"
            "  Mode simulation sans risque. Même logique de\n"
            "  copy, mais avec de l'argent virtuel. Parfait pour\n"
            "  tester un trader avant de risquer des vrais USDC.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "**COMMENT COMMENCER :**\n"
            "1️⃣ Le bot crée votre wallet Polygon automatiquement\n"
            "2️⃣ Allez dans **👥 Traders suivis** → ajoutez un wallet\n"
            "3️⃣ Le bot copie automatiquement leurs trades\n"
            "4️⃣ Mode Paper par défaut (0 risque)\n"
            "5️⃣ Passez en réel quand vous êtes prêt\n\n"
            "💰 Frais : 1% par trade copié\n"
            "🔒 Clés chiffrées AES-256 — jamais exposées\n"
            "⚡ Copie en < 15 secondes après le trader"
        )
        keyboard = [
            [
                InlineKeyboardButton("⬅️ Page 1/3", callback_data="menu_help"),
                InlineKeyboardButton("▶️ Page 3/3", callback_data="menu_help_3"),
            ],
            [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
        ]
    else:
        text = (
            "❓ **GUIDE V3 — SMART ANALYSIS (3/3)**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "**🧠 Signal Scoring** (automatique)\n"
            "  Chaque signal reçoit un score 0-100 basé sur :\n"
            "  • Spread bid-ask (serré = bon)\n"
            "  • Liquidité du marché (volume 24h)\n"
            "  • Conviction du trader (taille vs portfolio)\n"
            "  • Forme du trader (win rate 7 jours)\n"
            "  • Timing (distance à l'expiry)\n"
            "  • Consensus (d'autres traders font pareil)\n"
            "  Réglable dans ⚙️ → 🧠 Smart Analysis\n\n"
            "**🎯 Filtre Intelligent** (automatique)\n"
            "  Bloque les mauvais trades :\n"
            "  • Marchés coin-flip (prix ~$0.50)\n"
            "  • Trader sans edge prouvé sur ce type\n"
            "  • Trades à faible conviction (< 2% du portfolio)\n"
            "  • Prix qui a trop bougé depuis le master\n\n"
            "**📉 Gestion des positions** (actif)\n"
            "  Le bot surveille vos positions toutes les 15s :\n"
            "  • Trailing stop — suit le prix à la hausse\n"
            "  • Time exit — sort les positions mortes\n"
            "  • Scale-out — prend du profit partiellement\n"
            "  Réglable dans ⚙️ → 📉 Gestion Positions\n\n"
            "**📦 Portfolio & Risque** (automatique)\n"
            "  Protège contre la surexposition :\n"
            "  • Max positions simultanées\n"
            "  • Max par catégorie (30% Crypto par défaut)\n"
            "  • Biais directionnel (pas tout en YES)\n"
            "  Réglable dans ⚙️ → 📦 Portfolio & Risque\n\n"
            "**📈 Analytics** (bouton menu principal)\n"
            "  Tableau de bord avec performances traders,\n"
            "  portfolio, historique signaux, stats filtres.\n\n"
            "**📬 Topics Telegram** (automatique)\n"
            "  Ajoutez le bot comme admin dans un groupe forum.\n"
            "  Il crée automatiquement 5 topics :\n"
            "  📊 Signaux | 👤 Traders | 💼 Portfolio\n"
            "  🚨 Alertes | ⚙️ Admin"
        )
        keyboard = [
            [InlineKeyboardButton("⬅️ Page 2/3", callback_data="menu_help_2")],
            [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
        ]

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
        "_Performances réelles sur Polymarket._\n",
    ]

    total_positions = 0
    grand_unrealized = 0.0
    grand_invested = 0.0
    now_ts = int(time.time())

    for wallet in followed:
        w_short = f"{wallet[:6]}...{wallet[-4:]}"

        # Fetch positions + activity (latest 20 for display)
        positions = await polymarket_client.get_positions_by_address(wallet)
        activity = await polymarket_client.get_activity_by_address(
            wallet, limit=20, start=now_ts - 86400
        )

        # Séparer positions ouvertes vs résolues
        open_pos = [p for p in positions if not p.redeemable]
        settled_pos = [p for p in positions if p.redeemable]

        # Try to get profile stats (PNL total, 1W, 1D)
        profile = None
        try:
            profile = await polymarket_client.get_trader_profile(wallet)
        except Exception:
            pass

        if profile and profile.pseudonym:
            lines.append(f"👤 **{profile.pseudonym}** `{w_short}`")
        else:
            lines.append(f"👤 **Trader** `{w_short}`")

        if not positions and not activity:
            lines.append("   _Aucune activité_\n")
            continue

        # Show profile PNL if available
        if profile and profile.pnl_total != 0:
            e_total = "📈" if profile.pnl_total >= 0 else "📉"
            e_1w = "📈" if profile.pnl_1w >= 0 else "📉"
            lines.append(
                f"   💰 **PNL total : {profile.pnl_total:+,.0f}$** | "
                f"{e_1w} 7j : {profile.pnl_1w:+,.0f}$ | "
                f"24h : {profile.pnl_1d:+,.0f}$"
            )

        # ── Activity par timeframe (avec pagination pour vrais chiffres) ──
        tf_parts = []
        for label, secs in [("1h", 3600), ("24h", 86400)]:
            start_ts = now_ts - secs
            tf_acts = await polymarket_client.get_activity_paginated(
                wallet, start=start_ts, max_trades=5000
            )
            if tf_acts:
                vol = sum(a.usdc_size for a in tf_acts)
                tf_parts.append(f"{label}:{len(tf_acts)}t/{vol:,.0f}$")
        if tf_parts:
            lines.append(f"   📊 {' | '.join(tf_parts)}")

        # ── PNL calculé sur positions ouvertes uniquement ──
        trader_unrealized = sum(p.cash_pnl for p in open_pos)
        trader_invested = sum(p.initial_value for p in open_pos)
        trader_current = sum(p.current_value for p in open_pos)
        grand_unrealized += trader_unrealized
        grand_invested += trader_invested
        total_positions += len(open_pos)

        if open_pos:
            pnl_pct = (trader_unrealized / trader_invested * 100) if trader_invested > 0 else 0
            e = "📈" if trader_unrealized >= 0 else "📉"
            lines.append(
                f"   {e} **PNL ouvert : {trader_unrealized:+.2f}$ ({pnl_pct:+.1f}%)** "
                f"sur {len(open_pos)} pos"
            )
        else:
            lines.append("   _Pas de position ouverte_")

        # ── Top positions ouvertes ──
        if open_pos:
            open_pos.sort(key=lambda p: abs(p.cash_pnl), reverse=True)
            for p in open_pos[:5]:
                e = "📈" if p.cash_pnl >= 0 else "📉"
                title = p.title[:28] + "…" if len(p.title) > 28 else p.title
                lines.append(
                    f"   {e} **{p.outcome}** {p.avg_price:.2f}→{p.current_price:.2f} "
                    f"({p.pnl_pct:+.0f}%) {p.cash_pnl:+.0f}$"
                )
                lines.append(f"      _{title}_")
            if len(open_pos) > 5:
                lines.append(f"   _… +{len(open_pos) - 5} autres_")

        # ── Derniers trades ──
        if activity:
            lines.append("   ─ Derniers trades ─")
            for a in activity[:3]:
                side_e = "🟢" if a.side == "BUY" else "🔴"
                title = a.title[:25] + "…" if len(a.title) > 25 else a.title
                lines.append(
                    f"   {side_e} {a.side} **{a.outcome}** "
                    f"{a.usdc_size:.1f}$ @ {a.price:.2f}"
                )
                lines.append(f"      _{title}_")

        lines.append("")

    # ── Totaux ──
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    pnl_pct = (grand_unrealized / grand_invested * 100) if grand_invested > 0 else 0
    e = "📈" if grand_unrealized >= 0 else "📉"
    lines.append(
        f"{e} **P&L positions ouvertes : {grand_unrealized:+.2f}$ ({pnl_pct:+.1f}%)**\n"
        f"💰 Investi : {grand_invested:.0f}$ • "
        f"{total_positions} pos ouvertes sur {len(followed)} trader(s)"
    )
    lines.append(
        "\n_📋 Récap = vos trades copiés par le bot_"
    )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n\n_… tronqué_"

    keyboard = [
        [
            InlineKeyboardButton("🔄 Rafraîchir", callback_data="menu_dashboard"),
            InlineKeyboardButton("📋 Récap", callback_data="menu_recap"),
        ],
        [
            InlineKeyboardButton("📊 Rapport HTML", callback_data="dashboard_report"),
            InlineKeyboardButton("📄 Rapport PDF", callback_data="dashboard_report_pdf"),
        ],
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

    try:
        return await _menu_recap_impl(query)
    except Exception as e:
        logger.error(f"menu_recap error: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ **Erreur Récap**\n\n`{str(e)[:200]}`\n\n"
                "Réessayez dans quelques secondes.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Réessayer", callback_data="menu_recap")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
        except Exception:
            pass


async def _menu_recap_impl(query) -> None:
    """Internal implementation of menu_recap."""
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

        def _aware(dt):
            """Ensure datetime is timezone-aware (UTC)."""
            if dt is None:
                return None
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

        # Filter by current mode (paper or live)
        is_paper = user.paper_trading
        all_trades = (await session.execute(
            select(Trade).where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
                Trade.is_paper == is_paper,
            ).order_by(Trade.created_at.desc())
        )).scalars().all()
        # Filter out trades with no created_at to prevent comparisons with None
        all_trades = [t for t in all_trades if t.created_at is not None]
        # Make all timestamps timezone-aware
        for t in all_trades:
            t.created_at = _aware(t.created_at)

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

    # P&L global — utiliser settlement_pnl pour les trades settled
    settled_trades = [t for t in all_trades if t.is_settled and t.settlement_pnl is not None]
    total_pnl = sum(t.settlement_pnl for t in settled_trades)
    wins = sum(1 for t in settled_trades if t.settlement_pnl > 0)
    closed = len(settled_trades)

    # Fetch prix live pour positions ouvertes (non settled)
    from bot.services.polymarket import polymarket_client
    open_buys = [t for t in all_trades if t.side == TradeSide.BUY and not t.is_settled]
    unrealized_pnl = 0.0
    for t in open_buys:
        cur = await polymarket_client.get_price(t.token_id)
        if cur > 0 and t.shares > 0:
            unrealized_pnl += (cur * t.shares) - t.net_amount_usdc

    global_wr = f"{(wins / closed) * 100:.0f}%" if closed > 0 else "N/A"

    mode_label = "📝 PAPER" if is_paper else "💵 LIVE"
    lines = [
        f"📋 **RÉCAP — MES TRADES COPIÉS** ({mode_label})\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "_Ce que le bot a exécuté pour vous._\n",
        f"📅 Aujourd'hui : **{len(trades_today)}** trades • **{vol_today:.2f}** USDC",
        f"📆 Cette semaine : **{len(trades_week)}** trades • **{vol_week:.2f}** USDC",
        f"📊 Win rate : **{global_wr}** ({wins}/{closed} résolus)",
        f"📈 P&L réalisé : **{total_pnl:+.2f} USDC**" if closed > 0 else "",
        f"📉 P&L ouvert : **{unrealized_pnl:+.2f} USDC**" if open_buys else "",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "**DÉTAIL PAR TRADER COPIÉ**\n",
    ]
    # Remove empty lines from conditional entries
    lines = [l for l in lines if l or l == ""]

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

        # P&L par trader — utiliser settlement_pnl
        t_settled = [t for t in trader_trades if t.is_settled and t.settlement_pnl is not None]
        t_pnl = sum(t.settlement_pnl for t in t_settled)
        t_wins = sum(1 for t in t_settled if t.settlement_pnl > 0)
        t_closed = len(t_settled)
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
            settled = " ✅" if rt.is_settled else ""
            pnl_s = f" ({rt.settlement_pnl:+.1f})" if rt.settlement_pnl else ""
            lines.append(
                f"   {side_emoji} {date_s} | "
                f"{rt.net_amount_usdc:.2f} USDC{settled}{pnl_s}"
            )
            lines.append(f"      _{q}_")
        lines.append("")

    lines.append(
        "_📡 Dashboard = performances réelles des traders_\n"
        "_📋 Récap = ce que le bot a copié pour vous_"
    )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n\n_… tronqué_"

    keyboard = [
        [
            InlineKeyboardButton("📡 Dashboard", callback_data="menu_dashboard"),
            InlineKeyboardButton("🔄 Rafraîchir", callback_data="menu_recap"),
        ],
        [
            InlineKeyboardButton("📊 Rapport HTML", callback_data="paper_report"),
            InlineKeyboardButton("📄 Rapport PDF", callback_data="paper_report_pdf"),
        ],
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

    # ── Separate active vs expired positions ──
    active_trades = []
    expired_trades = []
    for t in open_trades:
        cur_price = current_prices.get(t.token_id, 0)
        if cur_price > 0:
            active_trades.append(t)
        else:
            expired_trades.append(t)

    # ── Calculate unrealized PNL (active positions only) ──
    total_invested = 0.0
    total_current_value = 0.0
    for t in active_trades:
        invested = t.net_amount_usdc
        shares = t.shares or (invested / t.price if t.price > 0 else 0)
        cur_price = current_prices.get(t.token_id, 0)
        current_value = shares * cur_price
        total_invested += invested
        total_current_value += current_value

    # Expired positions: invested but value unknown (awaiting settlement)
    expired_invested = sum(t.net_amount_usdc for t in expired_trades)

    unrealized_pnl = total_current_value - total_invested
    unrealized_pct = (unrealized_pnl / total_invested * 100) if total_invested > 0 else 0

    # ── Portfolio total = cash + active positions value ──
    # Expired positions NOT counted (result unknown until settlement)
    portfolio_value = paper_balance + total_current_value
    total_pnl = portfolio_value - paper_initial
    pnl_pct = (total_pnl / paper_initial * 100) if paper_initial > 0 else 0
    pnl_sign = "+" if total_pnl >= 0 else ""
    pnl_emoji = "📈" if total_pnl >= 0 else "📉"

    lines = [
        "📝 **PAPER WALLET**\n━━━━━━━━━━━━━━━━━━━━\n",
        f"🏁 Capital initial : **{paper_initial:.2f} USDC**\n",
        f"💵 Cash disponible : **{paper_balance:.2f} USDC**",
        f"📊 Positions actives : **{total_current_value:.2f} USDC**"
        f" ({len(active_trades)} pos.)",
    ]
    if expired_trades:
        lines.append(
            f"⏳ En attente de résolution : **{expired_invested:.2f} USDC**"
            f" ({len(expired_trades)} pos.)"
        )
    lines += [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"💼 **Portefeuille total : {portfolio_value:.2f} USDC**",
        f"{pnl_emoji} **PNL total : {pnl_sign}{total_pnl:.2f} USDC "
        f"({pnl_sign}{pnl_pct:.1f}%)**",
    ]
    if expired_trades:
        lines.append(
            f"_({len(expired_trades)} marches expires en attente — "
            f"resultat final apres resolution)_"
        )
    lines.append("")

    # ── Active positions with unrealized PNL ──
    if active_trades:
        ur_sign = "+" if unrealized_pnl >= 0 else ""
        ur_emoji = "📈" if unrealized_pnl >= 0 else "📉"
        lines.append(
            f"📌 **Positions actives** ({len(active_trades)}) — "
            f"{ur_emoji} {ur_sign}{unrealized_pnl:.2f} USDC "
            f"({ur_sign}{unrealized_pct:.1f}%)"
        )
        for t in active_trades[:10]:
            q = t.market_question or t.market_id
            if len(q) > 35:
                q = q[:32] + "..."
            shares = t.shares or 0
            invested = t.net_amount_usdc
            entry_price = t.price
            cur_price = current_prices.get(t.token_id, 0)
            current_val = shares * cur_price
            pos_pnl = current_val - invested
            pos_pct = (pos_pnl / invested * 100) if invested > 0 else 0

            time_str = t.created_at.strftime("%d/%m %H:%M") if t.created_at else "?"

            p_sign = "+" if pos_pnl >= 0 else ""
            p_emoji = "📈" if pos_pnl >= 0 else "📉"
            pnl_str = f"{p_emoji} {p_sign}{pos_pnl:.2f} ({p_sign}{pos_pct:.0f}%)"

            lines.append(
                f"\n   🟢 **{q}**\n"
                f"   {time_str} | {invested:.2f}→{current_val:.2f} USDC | {pnl_str}\n"
                f"   Entry: {entry_price:.2f} → Now: {cur_price:.2f} | {shares:.1f} shares"
            )

    # ── Expired / awaiting resolution ──
    if expired_trades:
        lines.append(
            f"\n⏳ **Marches expires** ({len(expired_trades)}) — "
            f"en attente de resolution"
        )
        for t in expired_trades[:10]:
            q = t.market_question or t.market_id
            if len(q) > 35:
                q = q[:32] + "..."
            invested = t.net_amount_usdc
            time_str = t.created_at.strftime("%d/%m %H:%M") if t.created_at else "?"
            lines.append(
                f"\n   ⏳ **{q}**\n"
                f"   {time_str} | Mise : {invested:.2f} USDC | Entry: {t.price:.2f}"
            )
        if len(expired_trades) > 10:
            lines.append(f"   _… +{len(expired_trades) - 10} autres_")

    if not active_trades and not expired_trades:
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
            InlineKeyboardButton("📊 Rapport", callback_data="paper_report"),
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
    """Generate and send a PDF report of OUR copied trades (Recap)."""
    query = update.callback_query
    await query.answer("⏳ Génération du rapport PDF (mes trades)…")

    try:
        from bot.models.trade import Trade, TradeStatus
        from bot.services.polymarket import polymarket_client
        from bot.services.report import build_recap_report_data
        from bot.services.report_html import generate_recap_report_html
        import asyncio

        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            if not user:
                return

            us = await get_or_create_settings(session, user)

            is_paper = user.paper_trading
            result = await session.execute(
                select(Trade).where(
                    Trade.user_id == user.id,
                    Trade.status == TradeStatus.FILLED,
                    Trade.is_paper == is_paper,
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

        # Build report data and generate HTML
        report_data = await build_recap_report_data(user, us, trades, current_prices)
        html_buffer = generate_recap_report_html(report_data)

        # Send HTML file
        from datetime import datetime, timezone
        filename = (
            f"wenpolymarket_recap_"
            f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.html"
        )

        await query.message.reply_document(
            document=html_buffer,
            filename=filename,
            caption=(
                f"📊 **Rapport Recap — Mes trades copies**\n"
                f"{'📝 Paper Trading' if user.paper_trading else '💵 Live Trading'}\n"
                f"💼 Portefeuille : {report_data.portfolio_value:.2f} USDC\n"
                f"{'📈' if report_data.total_pnl >= 0 else '📉'} "
                f"PNL : {'+' if report_data.total_pnl >= 0 else ''}"
                f"{report_data.total_pnl:.2f} USDC "
                f"({'+' if report_data.total_pnl_pct >= 0 else ''}"
                f"{report_data.total_pnl_pct:.1f}%)\n\n"
                f"_Ouvrir dans un navigateur pour voir le rapport interactif._"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"paper_report error: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ **Erreur generation PDF**\n\n`{str(e)[:300]}`\n\n"
                "Reessayez dans quelques secondes.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Réessayer", callback_data="paper_report")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
        except Exception:
            pass


async def dashboard_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and send an HTML report of followed traders' performance."""
    query = update.callback_query
    await query.answer("⏳ Génération du rapport traders…")

    try:
        from bot.services.report import build_trader_report_data
        from bot.services.report_html import generate_trader_report_html

        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            if not user:
                return
            us = await get_or_create_settings(session, user)
            followed = us.followed_wallets or []

        if not followed:
            await query.edit_message_text(
                "❌ **Aucun trader suivi.**\n\n"
                "Ajoutez des traders dans les parametres.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ Parametres", callback_data="menu_settings")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
            return

        username = user.telegram_username or f"User {user.telegram_id}"

        # Build report data from Polymarket API
        report_data = await build_trader_report_data(username, followed)
        html_buffer = generate_trader_report_html(report_data)

        # Send HTML file
        from datetime import datetime, timezone
        filename = (
            f"wenpolymarket_traders_"
            f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.html"
        )

        total_pnl = report_data.grand_unrealized
        await query.message.reply_document(
            document=html_buffer,
            filename=filename,
            caption=(
                f"📊 **Rapport Dashboard — Traders suivis**\n"
                f"👥 {len(report_data.traders)} trader(s) | "
                f"{report_data.total_open_positions} positions ouvertes\n"
                f"{'📈' if total_pnl >= 0 else '📉'} "
                f"PNL ouvert : {'+' if total_pnl >= 0 else ''}"
                f"{total_pnl:.2f} USDC\n\n"
                f"_Ouvrir dans un navigateur pour voir le rapport interactif._"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"dashboard_report error: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ **Erreur generation rapport traders**\n\n`{str(e)[:300]}`\n\n"
                "Reessayez dans quelques secondes.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Réessayer", callback_data="dashboard_report")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
        except Exception:
            pass


async def dashboard_report_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and send a PDF report of followed traders' performance."""
    query = update.callback_query
    await query.answer("⏳ Génération du PDF traders…")

    try:
        from bot.services.report import build_trader_report_data, generate_trader_report_pdf

        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            if not user:
                return
            us = await get_or_create_settings(session, user)
            followed = us.followed_wallets or []

        if not followed:
            await query.edit_message_text(
                "❌ **Aucun trader suivi.**",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
            return

        username = user.telegram_username or f"User {user.telegram_id}"
        report_data = await build_trader_report_data(username, followed)
        pdf_buffer = generate_trader_report_pdf(report_data)

        from datetime import datetime, timezone
        filename = f"wenpolymarket_traders_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.pdf"
        total_pnl = report_data.grand_unrealized
        await query.message.reply_document(
            document=pdf_buffer,
            filename=filename,
            caption=(
                f"📄 **Rapport PDF — Traders suivis**\n"
                f"👥 {len(report_data.traders)} trader(s) | "
                f"{report_data.total_open_positions} positions ouvertes\n"
                f"{'📈' if total_pnl >= 0 else '📉'} "
                f"PNL ouvert : {'+' if total_pnl >= 0 else ''}{total_pnl:.2f} USDC"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"dashboard_report_pdf error: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ **Erreur PDF traders**\n\n`{str(e)[:300]}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Réessayer", callback_data="dashboard_report_pdf")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
        except Exception:
            pass


async def paper_report_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and send a PDF report of our copied trades."""
    query = update.callback_query
    await query.answer("⏳ Génération du PDF (mes trades)…")

    try:
        from bot.models.trade import Trade, TradeStatus
        from bot.services.polymarket import polymarket_client
        from bot.services.report import build_recap_report_data, generate_recap_report_pdf
        import asyncio

        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            if not user:
                return
            us = await get_or_create_settings(session, user)
            is_paper = user.paper_trading
            result = await session.execute(
                select(Trade).where(
                    Trade.user_id == user.id,
                    Trade.status == TradeStatus.FILLED,
                    Trade.is_paper == is_paper,
                ).order_by(Trade.created_at.desc())
            )
            trades = list(result.scalars().all())

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

        report_data = await build_recap_report_data(user, us, trades, current_prices)
        pdf_buffer = generate_recap_report_pdf(report_data)

        from datetime import datetime, timezone
        filename = f"wenpolymarket_recap_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.pdf"
        await query.message.reply_document(
            document=pdf_buffer,
            filename=filename,
            caption=(
                f"📄 **Rapport PDF — Mes trades copies**\n"
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
    except Exception as e:
        logger.error(f"paper_report_pdf error: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ **Erreur PDF**\n\n`{str(e)[:300]}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Réessayer", callback_data="paper_report_pdf")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
        except Exception:
            pass


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

async def onboard_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'Accéder au menu principal' from /start welcome (photo message).

    The /start message is a photo (banner), so we can't edit_message_text on it.
    Instead, send a NEW text message with the main menu.
    """
    query = update.callback_query
    await query.answer()

    tg_user = query.from_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user:
            from bot.services.user_service import create_user
            user = await create_user(session, tg_user.id, username=tg_user.username)

        text, keyboard = _build_main_menu_content(tg_user, user)

    await query.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


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


# ── Trader Report (wallet tracker) ─────────────────────

async def trader_report_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of followed wallets to pick one for a detailed report."""
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return
        us = await get_or_create_settings(session, user)
        followed = us.followed_wallets or []

    if not followed:
        await query.edit_message_text(
            "❌ Aucun trader suivi.\n\nAjoutez un wallet dans ⚙️ Paramètres.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
            ]),
        )
        return

    # Get exclusion counts per trader
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        us = await get_or_create_settings(session, user)
        trader_filters = us.trader_filters or {}

    keyboard = []
    for w in followed:
        short = f"{w[:6]}...{w[-4:]}"
        # Show exclusion count if any
        excl_count = len(
            trader_filters.get(w.lower(), {}).get("excluded_categories", [])
        )
        filter_badge = f" 🚫{excl_count}" if excl_count else ""
        keyboard.append([
            InlineKeyboardButton(f"📊 {short}", callback_data=f"trader_rpt_{w}"),
            InlineKeyboardButton(f"🎯 Filtres{filter_badge}", callback_data=f"trader_cats_{w}"),
        ])
    keyboard.append([InlineKeyboardButton("🏠 Menu", callback_data="menu_back")])

    await query.edit_message_text(
        "📊 **RAPPORT TRADER**\n━━━━━━━━━━━━━━━━━━━━\n\n"
        "Choisissez un trader :\n"
        "📊 = Rapport détaillé | 🎯 = Analyse & filtres par catégorie",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def trader_report_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a performance report for a followed trader's wallet."""
    query = update.callback_query
    wallet = query.data.replace("trader_rpt_", "")
    await query.answer("⏳ Analyse du wallet en cours…")

    try:
        from bot.services.polymarket import polymarket_client
        from datetime import datetime, timezone, timedelta
        import asyncio

        w_short = f"{wallet[:6]}...{wallet[-4:]}"

        now = datetime.now(timezone.utc)

        # Fetch positions and activity (paginated, last 24h) in parallel
        positions_task = polymarket_client.get_positions_by_address(wallet)
        ts_24h = int((now - timedelta(hours=24)).timestamp())
        activity_task = polymarket_client.get_activity_paginated(
            wallet, start=ts_24h, max_trades=10000
        )

        positions, activities = await asyncio.gather(
            positions_task, activity_task, return_exceptions=True
        )

        if isinstance(positions, Exception):
            positions = []
        if isinstance(activities, Exception):
            activities = []

        # ── Calculate stats per timeframe ──
        timeframes = [
            ("1h", timedelta(hours=1)),
            ("3h", timedelta(hours=3)),
            ("5h", timedelta(hours=5)),
            ("24h", timedelta(hours=24)),
        ]

        tf_lines = []
        for label, delta in timeframes:
            cutoff_ts = int((now - delta).timestamp())
            tf_acts = [a for a in activities if a.timestamp >= cutoff_ts]
            buys = sum(1 for a in tf_acts if a.side.upper() == "BUY")
            sells = sum(1 for a in tf_acts if a.side.upper() == "SELL")
            volume = sum(a.usdc_size for a in tf_acts)
            trades_count = len(tf_acts)

            tf_lines.append(
                f"**{label}** : {trades_count} trades "
                f"({buys}B/{sells}S) • "
                f"Vol: {volume:.0f} USDC"
            )

        # ── Current positions summary ──
        total_invested = 0.0
        total_current = 0.0
        pos_lines = []

        # Sort by value (biggest first)
        positions.sort(key=lambda p: p.size * p.current_price, reverse=True)

        for p in positions[:20]:  # Top 20 positions
            invested = p.size * p.avg_price
            current_val = p.size * p.current_price
            pnl = current_val - invested
            total_invested += invested
            total_current += current_val

            pnl_emoji = "📈" if pnl >= 0 else "📉"
            title = p.title[:35] + "…" if len(p.title) > 35 else p.title

            pos_lines.append(
                f"{'🟢' if p.outcome.lower() == 'yes' else '🔴'} **{title}**\n"
                f"   {p.outcome} @ {p.avg_price:.2f} → {p.current_price:.2f} "
                f"| {p.size:.1f} shares\n"
                f"   {pnl_emoji} {pnl:+.2f} USDC ({p.pnl_pct:+.1f}%)"
            )

        total_pnl = total_current - total_invested
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0

        # ── Build message ──
        text = (
            f"📊 **RAPPORT TRADER**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 Wallet : `{w_short}`\n"
            f"📅 {now.strftime('%d/%m/%Y %H:%M')} UTC\n\n"
            f"**📈 ACTIVITÉ DE TRADING**\n"
        )
        text += "\n".join(tf_lines)
        text += (
            f"\n\n**💼 POSITIONS OUVERTES** ({len(positions)} total"
            f"{', top 20 affichées' if len(positions) > 20 else ''})\n"
        )

        if pos_lines:
            text += "\n" + "\n\n".join(pos_lines[:10])  # Show top 10 in msg
            if len(positions) > 10:
                remaining_pnl = sum(
                    (p.size * p.current_price) - (p.size * p.avg_price)
                    for p in positions[10:]
                )
                text += f"\n\n_... +{len(positions) - 10} autres positions ({remaining_pnl:+.2f} USDC)_"
        else:
            text += "\n_Aucune position ouverte._"

        text += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 **Total investi** : {total_invested:.2f} USDC\n"
            f"💵 **Valeur actuelle** : {total_current:.2f} USDC\n"
            f"{'📈' if total_pnl >= 0 else '📉'} **PNL** : {total_pnl:+.2f} USDC "
            f"({total_pnl_pct:+.1f}%)\n"
        )

        # Truncate if too long for Telegram (4096 char limit)
        if len(text) > 4000:
            text = text[:3950] + "\n\n_... message tronqué_"

        keyboard = [
            [InlineKeyboardButton("🔄 Rafraîchir", callback_data=f"trader_rpt_{wallet}")],
            [
                InlineKeyboardButton("📊 Rapport HTML", callback_data=f"trader_html_{wallet}"),
                InlineKeyboardButton("📄 Rapport PDF", callback_data=f"trader_pdf_{wallet}"),
            ],
            [InlineKeyboardButton("🎯 Analyse & Filtres par catégorie", callback_data=f"trader_cats_{wallet}")],
            [
                InlineKeyboardButton("👥 Traders suivis", callback_data="menu_traders"),
                InlineKeyboardButton("🏠 Menu", callback_data="menu_back"),
            ],
        ]

        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"trader_report error: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ **Erreur rapport trader**\n\n`{str(e)[:300]}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
        except Exception:
            pass


async def _fetch_single_trader_data(wallet: str) -> dict:
    """Fetch positions, activity, and profile for a single trader. Shared by HTML/PDF."""
    from bot.services.polymarket import polymarket_client
    from bot.services.market_categories import categorize_market
    from datetime import datetime, timezone, timedelta
    import asyncio

    now = datetime.now(timezone.utc)
    ts_7d = int((now - timedelta(days=7)).timestamp())

    # Fetch all data in parallel
    positions_task = polymarket_client.get_positions_by_address(wallet)
    activity_task = polymarket_client.get_activity_paginated(wallet, start=ts_7d, max_trades=5000)
    profile_task = polymarket_client.get_trader_profile(wallet)

    positions, activities, profile = await asyncio.gather(
        positions_task, activity_task, profile_task, return_exceptions=True
    )
    if isinstance(positions, Exception):
        positions = []
    if isinstance(activities, Exception):
        activities = []
    if isinstance(profile, Exception):
        profile = None

    # Timeframe stats
    timeframes = [
        ("1h", timedelta(hours=1)),
        ("3h", timedelta(hours=3)),
        ("5h", timedelta(hours=5)),
        ("24h", timedelta(hours=24)),
        ("7j", timedelta(days=7)),
    ]
    tf_stats = []
    for label, delta in timeframes:
        cutoff_ts = int((now - delta).timestamp())
        tf_acts = [a for a in activities if a.timestamp >= cutoff_ts]
        buys = sum(1 for a in tf_acts if a.side.upper() == "BUY")
        sells = sum(1 for a in tf_acts if a.side.upper() == "SELL")
        volume = sum(a.usdc_size for a in tf_acts)
        tf_stats.append({
            "label": label, "trades": len(tf_acts),
            "buys": buys, "sells": sells, "volume": volume,
        })

    # Category breakdown
    cat_stats: dict[str, dict] = {}
    for act in activities:
        cat = categorize_market(title=act.title, slug=act.slug)
        tag = cat.tag
        if tag not in cat_stats:
            cat_stats[tag] = {"trades": 0, "buys": 0, "sells": 0, "volume": 0.0}
        cat_stats[tag]["trades"] += 1
        cat_stats[tag]["volume"] += act.usdc_size
        if act.side.upper() == "BUY":
            cat_stats[tag]["buys"] += 1
        else:
            cat_stats[tag]["sells"] += 1

    # Positions enrichment
    total_invested = 0.0
    total_current = 0.0
    pos_list = []
    positions.sort(key=lambda p: p.size * p.current_price, reverse=True)
    for p in positions:
        invested = p.size * p.avg_price
        current_val = p.size * p.current_price
        pnl = current_val - invested
        total_invested += invested
        total_current += current_val
        pos_list.append({
            "title": p.title, "outcome": p.outcome,
            "avg_price": p.avg_price, "current_price": p.current_price,
            "size": p.size, "invested": invested,
            "current_val": current_val, "pnl": pnl,
            "pnl_pct": p.pnl_pct,
        })

    return {
        "wallet": wallet,
        "w_short": f"{wallet[:6]}...{wallet[-4:]}",
        "generated_at": now.strftime("%d/%m/%Y %H:%M UTC"),
        "profile": profile,
        "positions": pos_list,
        "total_positions": len(positions),
        "total_invested": total_invested,
        "total_current": total_current,
        "total_pnl": total_current - total_invested,
        "total_pnl_pct": ((total_current - total_invested) / total_invested * 100) if total_invested > 0 else 0,
        "tf_stats": tf_stats,
        "cat_stats": dict(sorted(cat_stats.items(), key=lambda x: x[1]["trades"], reverse=True)),
        "total_trades_7d": len(activities),
        "total_volume_7d": sum(a.usdc_size for a in activities),
    }


def _generate_single_trader_html(data: dict) -> "io.BytesIO":
    """Generate a self-contained HTML report for a single trader."""
    import io
    from datetime import datetime

    profile = data.get("profile")
    pnl_color = "#4ade80" if data["total_pnl"] >= 0 else "#f87171"

    # Profile section
    profile_html = ""
    if profile:
        profile_html = f"""
        <div class="profile-card">
            <h3>👤 {profile.pseudonym or profile.username or data['w_short']}</h3>
            <div class="stat-grid">
                <div class="stat"><span class="label">PNL Total</span><span class="value" style="color:{'#4ade80' if profile.pnl_total >= 0 else '#f87171'}">{profile.pnl_total:+,.2f}$</span></div>
                <div class="stat"><span class="label">PNL 1J</span><span class="value" style="color:{'#4ade80' if profile.pnl_1d >= 0 else '#f87171'}">{profile.pnl_1d:+,.2f}$</span></div>
                <div class="stat"><span class="label">PNL 1S</span><span class="value" style="color:{'#4ade80' if profile.pnl_1w >= 0 else '#f87171'}">{profile.pnl_1w:+,.2f}$</span></div>
                <div class="stat"><span class="label">PNL 1M</span><span class="value" style="color:{'#4ade80' if profile.pnl_1m >= 0 else '#f87171'}">{profile.pnl_1m:+,.2f}$</span></div>
                <div class="stat"><span class="label">Volume</span><span class="value">{profile.volume:,.0f}$</span></div>
                <div class="stat"><span class="label">Marchés</span><span class="value">{profile.markets_traded}</span></div>
            </div>
        </div>"""

    # Timeframe rows
    tf_rows = ""
    for tf in data["tf_stats"]:
        tf_rows += f"""<tr>
            <td><strong>{tf['label']}</strong></td>
            <td>{tf['trades']}</td>
            <td>{tf['buys']}</td>
            <td>{tf['sells']}</td>
            <td>{tf['volume']:,.0f} USDC</td>
        </tr>"""

    # Category rows
    cat_rows = ""
    total_trades = data["total_trades_7d"] or 1
    for tag, stats in data["cat_stats"].items():
        pct = stats["trades"] / total_trades * 100
        bar_w = max(2, int(pct))
        cat_rows += f"""<tr>
            <td><strong>{tag}</strong></td>
            <td>{stats['trades']}</td>
            <td>{stats['buys']}</td>
            <td>{stats['sells']}</td>
            <td>{stats['volume']:,.0f}</td>
            <td><div class="bar" style="width:{bar_w}%">{pct:.0f}%</div></td>
        </tr>"""

    # Positions rows
    pos_rows = ""
    for i, p in enumerate(data["positions"]):
        vis = "" if i < 10 else ' class="hidden-pos"'
        pnl_c = "#4ade80" if p["pnl"] >= 0 else "#f87171"
        outcome_dot = "🟢" if p["outcome"].lower() == "yes" else "🔴"
        title_safe = p["title"].replace("<", "&lt;").replace(">", "&gt;")
        pos_rows += f"""<tr{vis}>
            <td>{outcome_dot} {title_safe[:50]}</td>
            <td>{p['outcome']}</td>
            <td>{p['avg_price']:.3f}</td>
            <td>{p['current_price']:.3f}</td>
            <td>{p['size']:.1f}</td>
            <td style="color:{pnl_c}">{p['pnl']:+.2f}</td>
            <td style="color:{pnl_c}">{p['pnl_pct']:+.1f}%</td>
        </tr>"""

    show_more_btn = ""
    if len(data["positions"]) > 10:
        show_more_btn = f'<button class="toggle-btn" onclick="togglePositions()">Voir les {len(data["positions"]) - 10} autres positions</button>'

    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rapport Trader {data['w_short']}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0f0f0f;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:16px;max-width:1000px;margin:0 auto}}
h1{{color:#f59e0b;font-size:1.5em;margin-bottom:4px}}
h2{{color:#f59e0b;font-size:1.1em;margin:20px 0 8px;border-bottom:1px solid #333;padding-bottom:4px}}
.header{{background:#1a1a2e;border-radius:12px;padding:16px;margin-bottom:16px}}
.header .sub{{color:#888;font-size:0.85em}}
.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}}
.summary .card{{background:#1a1a2e;border-radius:8px;padding:12px;text-align:center}}
.summary .card .val{{font-size:1.4em;font-weight:bold}}
.summary .card .lbl{{color:#888;font-size:0.8em}}
.profile-card{{background:#1a1a2e;border-radius:12px;padding:16px;margin-bottom:16px}}
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-top:8px}}
.stat{{background:#252540;border-radius:8px;padding:8px;text-align:center}}
.stat .label{{display:block;color:#888;font-size:0.75em}}
.stat .value{{display:block;font-weight:bold;font-size:1.1em}}
table{{width:100%;border-collapse:collapse;font-size:0.85em;margin:8px 0}}
th{{background:#1a1a2e;color:#f59e0b;padding:8px;text-align:left}}
td{{padding:6px 8px;border-bottom:1px solid #222}}
tr:hover{{background:#1a1a2e}}
.bar{{background:#f59e0b;color:#000;padding:2px 6px;border-radius:4px;font-size:0.75em;min-width:24px;text-align:center}}
.hidden-pos{{display:none}}
.toggle-btn{{background:#f59e0b;color:#000;border:none;padding:8px 16px;border-radius:8px;cursor:pointer;font-weight:bold;margin:8px 0;width:100%}}
.toggle-btn:hover{{background:#d97706}}
.footer{{text-align:center;color:#555;font-size:0.75em;margin-top:20px;padding-top:10px;border-top:1px solid #222}}
</style></head><body>

<div class="header">
    <h1>📊 Rapport Trader</h1>
    <div class="sub">🔗 {data['w_short']} — {data['generated_at']}</div>
</div>

{profile_html}

<div class="summary">
    <div class="card"><div class="val" style="color:{pnl_color}">{data['total_pnl']:+,.2f}$</div><div class="lbl">PNL Positions</div></div>
    <div class="card"><div class="val">{data['total_invested']:,.0f}$</div><div class="lbl">Investi</div></div>
    <div class="card"><div class="val">{data['total_current']:,.0f}$</div><div class="lbl">Valeur actuelle</div></div>
    <div class="card"><div class="val">{data['total_positions']}</div><div class="lbl">Positions</div></div>
    <div class="card"><div class="val">{data['total_trades_7d']}</div><div class="lbl">Trades 7j</div></div>
    <div class="card"><div class="val">{data['total_volume_7d']:,.0f}$</div><div class="lbl">Volume 7j</div></div>
</div>

<h2>📈 Activité par timeframe</h2>
<table><thead><tr><th>Période</th><th>Trades</th><th>Buys</th><th>Sells</th><th>Volume</th></tr></thead>
<tbody>{tf_rows}</tbody></table>

<h2>🎯 Répartition par catégorie (7j)</h2>
<table><thead><tr><th>Catégorie</th><th>Trades</th><th>B</th><th>S</th><th>Volume</th><th>%</th></tr></thead>
<tbody>{cat_rows}</tbody></table>

<h2>💼 Positions ouvertes ({data['total_positions']})</h2>
<table><thead><tr><th>Marché</th><th>Side</th><th>Entry</th><th>Current</th><th>Shares</th><th>PNL</th><th>%</th></tr></thead>
<tbody>{pos_rows}</tbody></table>
{show_more_btn}

<div class="footer">WenPolymarket Bot — Généré le {data['generated_at']}</div>

<script>
function togglePositions(){{
    document.querySelectorAll('.hidden-pos').forEach(r=>{{
        r.style.display = r.style.display==='none'||!r.style.display ? 'table-row':'none';
    }});
    const btn=event.target;
    btn.textContent=btn.textContent.startsWith('Voir')?'Masquer':'Voir les {len(data["positions"])-10 if len(data["positions"])>10 else 0} autres positions';
}}
</script>
</body></html>"""

    buf = io.BytesIO(html.encode("utf-8"))
    buf.name = f"trader_{data['w_short']}.html"
    buf.seek(0)
    return buf


async def trader_single_html(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and send an HTML report for a single followed trader."""
    query = update.callback_query
    wallet = query.data.replace("trader_html_", "")
    await query.answer("⏳ Génération du rapport HTML…")

    try:
        data = await _fetch_single_trader_data(wallet)
        html_buf = _generate_single_trader_html(data)

        from datetime import datetime, timezone
        filename = f"trader_{data['w_short']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.html"
        pnl = data["total_pnl"]
        await query.message.reply_document(
            document=html_buf,
            filename=filename,
            caption=(
                f"📊 **Rapport HTML — Trader {data['w_short']}**\n"
                f"💼 {data['total_positions']} positions | "
                f"{data['total_trades_7d']} trades (7j)\n"
                f"{'📈' if pnl >= 0 else '📉'} PNL : {pnl:+,.2f} USDC"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"trader_single_html error: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ **Erreur rapport HTML**\n\n`{str(e)[:300]}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Réessayer", callback_data=f"trader_html_{wallet}")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
        except Exception:
            pass


async def trader_single_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and send a PDF report for a single followed trader."""
    query = update.callback_query
    wallet = query.data.replace("trader_pdf_", "")
    await query.answer("⏳ Génération du PDF…")

    try:
        data = await _fetch_single_trader_data(wallet)

        # Build a simple PDF using the existing report infrastructure
        from bot.services.report import TraderSection, TraderReportData, generate_trader_report_pdf

        profile = data.get("profile")
        w_short = data["w_short"]
        section = TraderSection(
            wallet=data["wallet"],
            wallet_short=w_short,
            username=profile.username if profile else "",
            pseudonym=profile.pseudonym if profile else "",
            pnl_total=profile.pnl_total if profile else 0,
            pnl_1d=profile.pnl_1d if profile else 0,
            pnl_1w=profile.pnl_1w if profile else 0,
            pnl_1m=profile.pnl_1m if profile else 0,
            volume=profile.volume if profile else 0,
            markets_traded=profile.markets_traded if profile else 0,
            has_profile=profile is not None,
            total_invested=data["total_invested"],
            total_current=data["total_current"],
            total_unrealized=data["total_pnl"],
        )

        report_data = TraderReportData(
            username=f"Trader {w_short}",
            traders=[section],
            total_open_positions=data["total_positions"],
            grand_unrealized=data["total_pnl"],
        )

        pdf_buffer = generate_trader_report_pdf(report_data)

        from datetime import datetime, timezone
        filename = f"trader_{data['w_short']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.pdf"
        pnl = data["total_pnl"]
        await query.message.reply_document(
            document=pdf_buffer,
            filename=filename,
            caption=(
                f"📄 **Rapport PDF — Trader {data['w_short']}**\n"
                f"💼 {data['total_positions']} positions | "
                f"{data['total_trades_7d']} trades (7j)\n"
                f"{'📈' if pnl >= 0 else '📉'} PNL : {pnl:+,.2f} USDC"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"trader_single_pdf error: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ **Erreur rapport PDF**\n\n`{str(e)[:300]}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Réessayer", callback_data=f"trader_pdf_{wallet}")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
        except Exception:
            pass


async def trader_category_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analyze a trader's activity by market category and show filter toggles.

    Shows a breakdown like:
        Crypto/BTC — 120 trades (62%) | Vol: 5,400 USDC
        Crypto/ETH — 40 trades (21%) | Vol: 1,800 USDC
        Sports/NFL — 15 trades (8%) | Vol: 600 USDC
    With toggle buttons [✅ Crypto/BTC] [❌ Crypto/XRP] to exclude categories.
    """
    query = update.callback_query
    wallet = query.data.replace("trader_cats_", "")
    await query.answer("⏳ Analyse des catégories…")

    try:
        from bot.services.polymarket import polymarket_client
        from bot.services.market_categories import categorize_market
        from datetime import datetime, timezone, timedelta
        import asyncio

        w_short = f"{wallet[:6]}...{wallet[-4:]}"

        # Fetch user settings for current exclusions
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            if not user:
                return
            us = await get_or_create_settings(session, user)
            trader_filters = us.trader_filters or {}
            wallet_lower = wallet.lower()
            current_exclusions = set(
                trader_filters.get(wallet_lower, {}).get("excluded_categories", [])
            )

        # Fetch activities (paginated, last 7 days for good sample)
        now = datetime.now(timezone.utc)
        ts_7d = int((now - timedelta(days=7)).timestamp())
        activities = await polymarket_client.get_activity_paginated(
            wallet, start=ts_7d, max_trades=5000
        )

        # Also fetch current positions for live category info
        positions = await polymarket_client.get_positions_by_address(wallet)

        # ── Categorize all activities ──
        category_stats: dict[str, dict] = {}  # tag → {trades, buys, sells, volume, pnl_est}

        for act in activities:
            cat = categorize_market(title=act.title, slug=act.slug)
            tag = cat.tag

            if tag not in category_stats:
                category_stats[tag] = {
                    "trades": 0,
                    "buys": 0,
                    "sells": 0,
                    "volume": 0.0,
                    "category": cat.category,
                    "subcategory": cat.subcategory,
                }

            stats = category_stats[tag]
            stats["trades"] += 1
            stats["volume"] += act.usdc_size
            if act.side.upper() == "BUY":
                stats["buys"] += 1
            else:
                stats["sells"] += 1

        # ── Categorize open positions ──
        pos_by_cat: dict[str, dict] = {}  # tag → {count, invested, current}
        for p in positions:
            cat = categorize_market(title=p.title, slug=p.slug)
            tag = cat.tag
            if tag not in pos_by_cat:
                pos_by_cat[tag] = {"count": 0, "invested": 0.0, "current": 0.0}
            pos_by_cat[tag]["count"] += 1
            pos_by_cat[tag]["invested"] += p.size * p.avg_price
            pos_by_cat[tag]["current"] += p.size * p.current_price

        # Sort by trade count
        sorted_cats = sorted(
            category_stats.items(), key=lambda x: x[1]["trades"], reverse=True
        )

        total_trades = sum(s["trades"] for _, s in sorted_cats)
        total_volume = sum(s["volume"] for _, s in sorted_cats)

        # ── Build message ──
        text = (
            f"🎯 **ANALYSE PAR CATÉGORIE**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 Trader : `{w_short}`\n"
            f"📅 Derniers 7 jours | {total_trades} trades | {total_volume:,.0f} USDC\n\n"
        )

        cat_lines = []
        for tag, stats in sorted_cats:
            pct = (stats["trades"] / total_trades * 100) if total_trades > 0 else 0
            excluded = tag in current_exclusions

            # Status emoji
            status = "🚫" if excluded else "✅"

            # Bar chart (simple visual)
            bar_len = max(1, int(pct / 5))
            bar = "█" * bar_len + "░" * (20 - bar_len)

            line = (
                f"{status} **{tag}**\n"
                f"   {bar} {pct:.0f}%\n"
                f"   📊 {stats['trades']}t ({stats['buys']}B/{stats['sells']}S) • "
                f"Vol: {stats['volume']:,.0f} USDC"
            )

            # Add position info if exists
            if tag in pos_by_cat:
                pc = pos_by_cat[tag]
                pos_pnl = pc["current"] - pc["invested"]
                line += (
                    f"\n   💼 {pc['count']} pos ouvertes • "
                    f"{'📈' if pos_pnl >= 0 else '📉'} {pos_pnl:+,.1f} USDC"
                )

            cat_lines.append(line)

        text += "\n\n".join(cat_lines)

        if current_exclusions:
            text += (
                f"\n\n━━━━━━━━━━━━━━━━━━━━\n"
                f"🚫 **{len(current_exclusions)} catégorie(s) exclue(s)** — "
                f"les trades de ce trader dans ces catégories seront ignorés"
            )

        # Truncate if needed
        if len(text) > 3800:
            text = text[:3750] + "\n\n_... tronqué, voir rapport HTML_"

        # ── Build toggle buttons (2 per row) ──
        keyboard: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for tag, stats in sorted_cats:
            excluded = tag in current_exclusions
            emoji = "🚫" if excluded else "✅"
            # Encode: tcat_toggle_{wallet}_{tag}
            # Tag might contain "/" so we use it directly (safe for callback_data)
            btn_text = f"{emoji} {tag}"
            # Limit button text length
            if len(btn_text) > 30:
                btn_text = btn_text[:27] + "…"
            row.append(
                InlineKeyboardButton(
                    btn_text,
                    callback_data=f"tcat_t_{wallet_lower}_{tag}",
                )
            )
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        # Action buttons
        keyboard.append([
            InlineKeyboardButton("🔄 Rafraîchir", callback_data=f"trader_cats_{wallet}"),
        ])
        keyboard.append([
            InlineKeyboardButton("📊 Rapport trader", callback_data=f"trader_rpt_{wallet}"),
            InlineKeyboardButton("📡 Dashboard", callback_data="menu_dashboard"),
        ])
        keyboard.append([
            InlineKeyboardButton("🏠 Menu", callback_data="menu_back"),
        ])

        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"trader_category_analysis error: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ **Erreur analyse catégories**\n\n`{str(e)[:300]}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
        except Exception:
            pass


async def trader_category_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle a category exclusion for a specific trader.

    callback_data format: tcat_t_{wallet}_{category_tag}
    Example: tcat_t_0x1234abcd_Crypto/BTC
    """
    query = update.callback_query

    try:
        # Parse callback data: tcat_t_{wallet}_{tag}
        raw = query.data[len("tcat_t_"):]  # Remove "tcat_t_" prefix
        # Wallet is 42 chars (0x + 40 hex), then underscore, then tag
        wallet = raw[:42]
        tag = raw[43:]  # Skip the underscore after wallet

        if not tag or not wallet:
            await query.answer("❌ Données invalides", show_alert=True)
            return

        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            if not user:
                return
            us = await get_or_create_settings(session, user)

            # Get or init trader_filters
            trader_filters = dict(us.trader_filters or {})
            wallet_lower = wallet.lower()

            if wallet_lower not in trader_filters:
                trader_filters[wallet_lower] = {"excluded_categories": []}

            excluded = list(trader_filters[wallet_lower].get("excluded_categories", []))

            # Toggle
            if tag in excluded:
                excluded.remove(tag)
                action = "✅ Activé"
            else:
                excluded.append(tag)
                action = "🚫 Exclu"

            trader_filters[wallet_lower]["excluded_categories"] = excluded
            us.trader_filters = trader_filters

            # Force SQLAlchemy to detect JSON change
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(us, "trader_filters")
            await session.commit()

        await query.answer(f"{action} : {tag}", show_alert=False)

        # Refresh the category analysis view
        # Rewrite callback_data to trigger trader_category_analysis
        query.data = f"trader_cats_{wallet}"
        await trader_category_analysis(update, context)

    except Exception as e:
        logger.error(f"trader_category_toggle error: {e}", exc_info=True)
        await query.answer(f"❌ Erreur: {str(e)[:100]}", show_alert=True)


# ── Scanner Traders ─────────────────────────────────────

_SCANNER_CATEGORIES = [
    "Crypto", "Politics", "Sports", "Finance", "Tech",
    "Economy", "Geopolitics", "Culture", "Weather", "Elections",
]

# Separate min and max presets for positions
_POS_MIN_PRESETS = [
    ("—", None),      # No filter
    ("≥ 10", 10),
    ("≥ 50", 50),
    ("≥ 100", 100),
    ("≥ 500", 500),
]

_POS_MAX_PRESETS = [
    ("—", None),      # No filter
    ("≤ 50", 50),
    ("≤ 100", 100),
    ("≤ 500", 500),
    ("≤ 1000", 1_000),
]

# Separate min and max presets for volume
_VOL_MIN_PRESETS = [
    ("—", None),      # No filter
    ("≥ 10k$", 10_000),
    ("≥ 100k$", 100_000),
    ("≥ 1M$", 1_000_000),
    ("≥ 10M$", 10_000_000),
]

_VOL_MAX_PRESETS = [
    ("—", None),      # No filter
    ("≤ 100k$", 100_000),
    ("≤ 1M$", 1_000_000),
    ("≤ 10M$", 10_000_000),
    ("≤ 100M$", 100_000_000),
]


def _get_scan_config(context) -> dict:
    """Get or init scanner config from user_data."""
    if "scanner" not in context.user_data:
        context.user_data["scanner"] = {
            "categories": ["Crypto"],
            "pnl_1d": False,
            "pnl_1w": False,
            "pnl_1m": False,
            "pos_min_idx": 0,   # Index into _POS_MIN_PRESETS (0 = no filter)
            "pos_max_idx": 0,   # Index into _POS_MAX_PRESETS (0 = no filter)
            "vol_min_idx": 0,   # Index into _VOL_MIN_PRESETS (0 = no filter)
            "vol_max_idx": 0,   # Index into _VOL_MAX_PRESETS (0 = no filter)
        }
    # Migrate old config format
    cfg = context.user_data["scanner"]
    for key in ("pos_min_idx", "pos_max_idx", "vol_min_idx", "vol_max_idx"):
        if key not in cfg:
            cfg[key] = 0
    # Remove old keys
    for old_key in ("trades_preset", "volume_preset", "period"):
        cfg.pop(old_key, None)
    return cfg


def _auto_period(cfg: dict) -> str:
    """Auto-determine leaderboard period from PNL filters.

    Uses the longest PNL period that is enabled. If none, defaults to 'all'.
    """
    if cfg.get("pnl_1m"):
        return "1m"
    if cfg.get("pnl_1w"):
        return "1w"
    if cfg.get("pnl_1d"):
        return "1d"
    return "all"


def _build_scanner_menu(cfg: dict) -> tuple[str, list]:
    """Build the scanner configuration menu text and keyboard."""
    # Categories
    cats = cfg["categories"]
    cat_text = ", ".join(cats) if cats else "Aucune"

    # PNL filters
    pnl_parts = []
    for key, label in [("pnl_1d", "1D"), ("pnl_1w", "1W"), ("pnl_1m", "1M")]:
        pnl_parts.append(f"{label} {'✅' if cfg[key] else '—'}")

    # Positions labels
    pos_min_label = _POS_MIN_PRESETS[cfg["pos_min_idx"]][0]
    pos_max_label = _POS_MAX_PRESETS[cfg["pos_max_idx"]][0]
    pos_text = f"min {pos_min_label} / max {pos_max_label}"

    # Volume labels
    vol_min_label = _VOL_MIN_PRESETS[cfg["vol_min_idx"]][0]
    vol_max_label = _VOL_MAX_PRESETS[cfg["vol_max_idx"]][0]
    vol_text = f"min {vol_min_label} / max {vol_max_label}"

    # Auto-period info
    period = _auto_period(cfg)
    period_labels = {"1d": "1 jour", "1w": "1 semaine", "1m": "1 mois", "all": "tout"}

    text = (
        "🔍 **SCANNER DE TRADERS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Configure tes critères puis lance le scan.\n"
        "Le scanner analyse le leaderboard Polymarket\n"
        "et filtre les traders selon tes critères.\n\n"
        f"📂 **Catégories** : {cat_text}\n"
        f"💰 **Bénéfice requis** : {' | '.join(pnl_parts)}\n"
        f"📊 **Positions** : {pos_text}\n"
        f"💵 **Volume** : {vol_text}\n"
        f"📅 _Période auto : {period_labels[period]}_\n"
    )

    # Build keyboard
    keyboard = []

    # Category toggles (4 per row)
    for row_start in range(0, len(_SCANNER_CATEGORIES), 4):
        row = []
        for cat in _SCANNER_CATEGORIES[row_start:row_start + 4]:
            emoji = "✅" if cat in cats else "⬜"
            row.append(InlineKeyboardButton(
                f"{emoji} {cat}", callback_data=f"scan_cat_{cat}"
            ))
        keyboard.append(row)

    # PNL filters row
    keyboard.append([
        InlineKeyboardButton(
            f"{'✅' if cfg['pnl_1d'] else '⬜'} Bénéf 1D",
            callback_data="scan_pnl_1d",
        ),
        InlineKeyboardButton(
            f"{'✅' if cfg['pnl_1w'] else '⬜'} Bénéf 1W",
            callback_data="scan_pnl_1w",
        ),
        InlineKeyboardButton(
            f"{'✅' if cfg['pnl_1m'] else '⬜'} Bénéf 1M",
            callback_data="scan_pnl_1m",
        ),
    ])

    # Positions min / max
    keyboard.append([
        InlineKeyboardButton(
            f"📊 Pos min: {pos_min_label}", callback_data="scan_pos_min"
        ),
        InlineKeyboardButton(
            f"📊 Pos max: {pos_max_label}", callback_data="scan_pos_max"
        ),
    ])

    # Volume min / max
    keyboard.append([
        InlineKeyboardButton(
            f"💵 Vol min: {vol_min_label}", callback_data="scan_vol_min"
        ),
        InlineKeyboardButton(
            f"💵 Vol max: {vol_max_label}", callback_data="scan_vol_max"
        ),
    ])

    # Action buttons
    keyboard.append([
        InlineKeyboardButton("🔍 LANCER LE SCAN", callback_data="scan_run"),
    ])
    keyboard.append([
        InlineKeyboardButton("🏠 Menu", callback_data="menu_back"),
    ])

    return text, keyboard


async def menu_scanner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the scanner configuration menu."""
    query = update.callback_query
    await query.answer()

    cfg = _get_scan_config(context)
    text, keyboard = _build_scanner_menu(cfg)

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def scan_toggle_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle a category in scanner config."""
    query = update.callback_query
    cat = query.data.replace("scan_cat_", "")

    cfg = _get_scan_config(context)
    if cat in cfg["categories"]:
        cfg["categories"].remove(cat)
    else:
        cfg["categories"].append(cat)

    await query.answer(f"{'✅' if cat in cfg['categories'] else '❌'} {cat}")

    text, keyboard = _build_scanner_menu(cfg)
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def scan_toggle_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle a PNL filter."""
    query = update.callback_query
    period = query.data.replace("scan_pnl_", "")  # "1d", "1w", "1m"
    key = f"pnl_{period}"

    cfg = _get_scan_config(context)
    cfg[key] = not cfg[key]

    await query.answer(f"{'✅' if cfg[key] else '❌'} Bénéfice {period.upper()}")

    text, keyboard = _build_scanner_menu(cfg)
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def scan_cycle_pos_min(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cycle through positions minimum presets."""
    query = update.callback_query
    cfg = _get_scan_config(context)
    cfg["pos_min_idx"] = (cfg["pos_min_idx"] + 1) % len(_POS_MIN_PRESETS)

    label = _POS_MIN_PRESETS[cfg["pos_min_idx"]][0]
    await query.answer(f"📊 Positions min: {label}")

    text, keyboard = _build_scanner_menu(cfg)
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def scan_cycle_pos_max(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cycle through positions maximum presets."""
    query = update.callback_query
    cfg = _get_scan_config(context)
    cfg["pos_max_idx"] = (cfg["pos_max_idx"] + 1) % len(_POS_MAX_PRESETS)

    label = _POS_MAX_PRESETS[cfg["pos_max_idx"]][0]
    await query.answer(f"📊 Positions max: {label}")

    text, keyboard = _build_scanner_menu(cfg)
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def scan_cycle_vol_min(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cycle through volume minimum presets."""
    query = update.callback_query
    cfg = _get_scan_config(context)
    cfg["vol_min_idx"] = (cfg["vol_min_idx"] + 1) % len(_VOL_MIN_PRESETS)

    label = _VOL_MIN_PRESETS[cfg["vol_min_idx"]][0]
    await query.answer(f"💵 Volume min: {label}")

    text, keyboard = _build_scanner_menu(cfg)
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def scan_cycle_vol_max(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cycle through volume maximum presets."""
    query = update.callback_query
    cfg = _get_scan_config(context)
    cfg["vol_max_idx"] = (cfg["vol_max_idx"] + 1) % len(_VOL_MAX_PRESETS)

    label = _VOL_MAX_PRESETS[cfg["vol_max_idx"]][0]
    await query.answer(f"💵 Volume max: {label}")

    text, keyboard = _build_scanner_menu(cfg)
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def scan_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute the scan with current config."""
    query = update.callback_query
    await query.answer()

    cfg = _get_scan_config(context)

    if not cfg["categories"]:
        await query.edit_message_text(
            "❌ **Sélectionnez au moins une catégorie.**",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Retour", callback_data="menu_scanner")],
            ]),
        )
        return

    # Show progress
    await query.edit_message_text(
        "🔍 **SCAN EN COURS…**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📂 Catégories : {', '.join(cfg['categories'])}\n"
        "📡 Scraping du leaderboard…\n\n"
        "_Cela peut prendre 30-60 secondes._",
        parse_mode="Markdown",
    )

    try:
        from bot.services.scanner import ScanFilters, run_scan

        period = _auto_period(cfg)

        filters = ScanFilters(
            categories=cfg["categories"],
            period=period,
            pnl_1d_positive=cfg["pnl_1d"],
            pnl_1w_positive=cfg["pnl_1w"],
            pnl_1m_positive=cfg["pnl_1m"],
            trades_min=_POS_MIN_PRESETS[cfg["pos_min_idx"]][1],
            trades_max=_POS_MAX_PRESETS[cfg["pos_max_idx"]][1],
            volume_min=_VOL_MIN_PRESETS[cfg["vol_min_idx"]][1],
            volume_max=_VOL_MAX_PRESETS[cfg["vol_max_idx"]][1],
            max_profiles=30,
        )

        # Progress callback
        async def _progress(current, total, msg):
            try:
                await query.edit_message_text(
                    f"🔍 **SCAN EN COURS…**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{msg}\n"
                    f"{'█' * max(1, int(current / max(total, 1) * 20))}"
                    f"{'░' * (20 - max(1, int(current / max(total, 1) * 20)))}"
                    f" {current}/{total}",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        results = await run_scan(filters, progress_callback=_progress)

        # Build results message
        if not results:
            await query.edit_message_text(
                "🔍 **RÉSULTATS DU SCAN**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "❌ **Aucun trader ne correspond à tes critères.**\n\n"
                "Essaie d'assouplir tes filtres (moins de conditions PNL,\n"
                "ou un volume/nombre de marchés plus large).",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Modifier les filtres", callback_data="menu_scanner")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
                ]),
            )
            return

        # Format results
        text = (
            f"🔍 **RÉSULTATS DU SCAN**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ **{len(results)} traders** correspondent\n\n"
        )

        keyboard = []
        for i, t in enumerate(results[:15]):  # Max 15 results
            name = t.pseudonym or t.username or f"{t.wallet[:6]}...{t.wallet[-4:]}"
            if len(name) > 20:
                name = name[:17] + "…"

            pnl_emoji = "📈" if t.pnl_total >= 0 else "📉"
            eff = f"{t.pnl_volume_ratio:.1f}%" if t.volume > 0 else "N/A"

            text += (
                f"**{i + 1}. {name}**\n"
                f"   {pnl_emoji} PNL: {t.pnl_1d:+,.0f}$ (1D) | "
                f"{t.pnl_1w:+,.0f}$ (1W) | {t.pnl_1m:+,.0f}$ (1M)\n"
                f"   📊 {t.markets_traded} marchés | 💵 {t.volume:,.0f}$ | "
                f"⚡ Eff: {eff}\n\n"
            )

            w_short = f"{t.wallet[:6]}…{t.wallet[-4:]}"
            keyboard.append([
                InlineKeyboardButton(
                    f"📊 {w_short}", callback_data=f"trader_rpt_{t.wallet}"
                ),
                InlineKeyboardButton(
                    "➕ Suivre", callback_data=f"scan_follow_{t.wallet}"
                ),
            ])

        # Truncate text if needed
        if len(text) > 3800:
            text = text[:3750] + "\n\n_… résultats tronqués_"

        keyboard.append([
            InlineKeyboardButton("⬅️ Modifier les filtres", callback_data="menu_scanner"),
        ])
        keyboard.append([
            InlineKeyboardButton("🏠 Menu", callback_data="menu_back"),
        ])

        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"scan_run error: {e}", exc_info=True)
        await query.edit_message_text(
            f"❌ **Erreur pendant le scan**\n\n`{str(e)[:300]}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Réessayer", callback_data="scan_run")],
                [InlineKeyboardButton("🏠 Menu", callback_data="menu_back")],
            ]),
        )


async def scan_follow_trader(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Follow a trader directly from scan results."""
    query = update.callback_query
    wallet = query.data.replace("scan_follow_", "")

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        if not user:
            return
        us = await get_or_create_settings(session, user)
        wallets = list(us.followed_wallets or [])

        if wallet.lower() in [w.lower() for w in wallets]:
            await query.answer("⚠️ Vous suivez déjà ce trader !", show_alert=True)
            return

        wallets.append(wallet)
        us.followed_wallets = wallets

        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(us, "followed_wallets")
        await session.commit()

    w_short = f"{wallet[:6]}...{wallet[-4:]}"
    await query.answer(f"✅ {w_short} ajouté aux traders suivis !", show_alert=True)


def get_menu_handlers() -> list:
    return [
        CallbackQueryHandler(menu_balance, pattern="^menu_balance$"),
        CallbackQueryHandler(menu_positions, pattern="^menu_positions$"),
        CallbackQueryHandler(menu_deposit, pattern="^menu_deposit$"),
        CallbackQueryHandler(menu_withdraw, pattern="^menu_withdraw$"),
        CallbackQueryHandler(menu_traders, pattern="^menu_traders$"),
        CallbackQueryHandler(menu_trader_remove, pattern=r"^mt_rm_\d+$"),
        CallbackQueryHandler(menu_settings, pattern="^menu_settings$"),
        CallbackQueryHandler(menu_bridge, pattern="^menu_bridge$"),
        CallbackQueryHandler(menu_history, pattern="^menu_history$"),
        CallbackQueryHandler(menu_help, pattern="^menu_help$"),
        CallbackQueryHandler(menu_help, pattern="^menu_help_2$"),
        CallbackQueryHandler(menu_help, pattern="^menu_help_3$"),
        CallbackQueryHandler(menu_dashboard, pattern="^menu_dashboard$"),
        CallbackQueryHandler(menu_recap, pattern="^menu_recap$"),
        CallbackQueryHandler(menu_switch_wallet, pattern="^menu_switch_wallet$"),
        CallbackQueryHandler(switch_wallet_callback, pattern=r"^switch_wallet_\d+$"),
        CallbackQueryHandler(menu_delete_wallet, pattern="^menu_delete_wallet$"),
        CallbackQueryHandler(delete_wallet_confirm, pattern=r"^delwallet_confirm_\d+$"),
        CallbackQueryHandler(delete_wallet_exec, pattern=r"^delwallet_exec_\d+$"),
        CallbackQueryHandler(menu_paper, pattern="^menu_paper$"),
        CallbackQueryHandler(paper_report, pattern="^paper_report$"),
        CallbackQueryHandler(paper_report_pdf, pattern="^paper_report_pdf$"),
        CallbackQueryHandler(dashboard_report, pattern="^dashboard_report$"),
        CallbackQueryHandler(dashboard_report_pdf, pattern="^dashboard_report_pdf$"),
        CallbackQueryHandler(trader_report_select, pattern="^trader_report$"),
        CallbackQueryHandler(trader_report_generate, pattern=r"^trader_rpt_0x[a-fA-F0-9]+$"),
        CallbackQueryHandler(trader_single_html, pattern=r"^trader_html_0x[a-fA-F0-9]+$"),
        CallbackQueryHandler(trader_single_pdf, pattern=r"^trader_pdf_0x[a-fA-F0-9]+$"),
        CallbackQueryHandler(trader_category_analysis, pattern=r"^trader_cats_0x[a-fA-F0-9]+$"),
        CallbackQueryHandler(trader_category_toggle, pattern=r"^tcat_t_0x[a-fA-F0-9]+_.+$"),
        # Scanner
        CallbackQueryHandler(menu_scanner, pattern="^menu_scanner$"),
        CallbackQueryHandler(scan_toggle_category, pattern=r"^scan_cat_.+$"),
        CallbackQueryHandler(scan_toggle_pnl, pattern=r"^scan_pnl_.+$"),
        CallbackQueryHandler(scan_cycle_pos_min, pattern="^scan_pos_min$"),
        CallbackQueryHandler(scan_cycle_pos_max, pattern="^scan_pos_max$"),
        CallbackQueryHandler(scan_cycle_vol_min, pattern="^scan_vol_min$"),
        CallbackQueryHandler(scan_cycle_vol_max, pattern="^scan_vol_max$"),
        CallbackQueryHandler(scan_run, pattern="^scan_run$"),
        CallbackQueryHandler(scan_follow_trader, pattern=r"^scan_follow_0x[a-fA-F0-9]+$"),
        CallbackQueryHandler(paper_set_balance, pattern="^paper_set_balance$"),
        CallbackQueryHandler(paper_init_callback, pattern=r"^paper_init_\d+$"),
        CallbackQueryHandler(paper_reset, pattern="^paper_reset$"),
        CallbackQueryHandler(export_pk, pattern="^export_pk$"),
        CallbackQueryHandler(stop_copy, pattern="^stop_copy$"),
        CallbackQueryHandler(resume_copy, pattern="^resume_copy$"),
        CallbackQueryHandler(menu_back, pattern="^menu_back$"),
        # Fallback: "Accéder au menu principal" — envoie un NOUVEAU message
        # (le message /start est une photo, on ne peut pas l'éditer en texte)
        CallbackQueryHandler(onboard_to_main_menu, pattern="^onboard_menu_main$"),
    ]
