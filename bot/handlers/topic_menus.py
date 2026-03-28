"""Menus contextuels par topic — V3 Multi-tenant.

Quand un utilisateur tape /menu depuis un topic de son groupe,
ce module détecte le topic et affiche un écran adapté à son contexte.

Topics :
  📊 signals  — scoring, filtres, signaux reçus
  👤 traders  — traders suivis, stats, pause
  💼 portfolio — positions, PNL, exposition
  🚨 alerts   — SL/TP/trailing, alertes actives
  ⚙️ admin    — statut bot, wallet, paramètres
"""

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.db.session import async_session
from bot.models.group_config import GroupConfig
from bot.models.user import User
from bot.services.user_service import get_user_by_telegram_id, get_or_create_settings
from bot.utils.formatting import (
    bar, bar_bicolor, fmt_usd, fmt_pnl, badge_trader_status,
    badge_score, short_wallet as sw, SEP, header,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Topic detection
# ─────────────────────────────────────────────

async def detect_topic(user_id: int, group_id: int, thread_id: Optional[int]) -> str:
    """Return which topic a message was sent in.

    Returns one of: "signals" | "traders" | "portfolio" | "alerts" | "admin" | "general"
    Queries by group_id only (unique) — user_id param kept for API compat but unused.
    """
    if not thread_id:
        return "general"

    try:
        from sqlalchemy import select
        async with async_session() as session:
            config = (await session.execute(
                select(GroupConfig).where(GroupConfig.group_id == group_id)
            )).scalar_one_or_none()

            if not config:
                logger.debug("detect_topic: no config for group=%s", group_id)
                return "general"

            mapping = {
                "signals":   config.topic_signals_id,
                "traders":   config.topic_traders_id,
                "portfolio": config.topic_portfolio_id,
                "alerts":    config.topic_alerts_id,
                "admin":     config.topic_admin_id,
            }

        logger.debug("detect_topic: group=%s thread=%s map=%s", group_id, thread_id, mapping)

        for name, tid in mapping.items():
            if tid is not None and tid == thread_id:
                return name

    except Exception as e:
        logger.warning("detect_topic error: %s", e)

    return "general"


# ─────────────────────────────────────────────
# Main dispatcher
# ─────────────────────────────────────────────

async def show_topic_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Show a context-aware menu depending on which topic the message was sent in.

    Returns True if a topic menu was shown, False if caller should show generic menu.

    IMPORTANT: session stays open while calling _show_*_menu so ORM objects
    remain live (prevents DetachedInstanceError / MissingGreenlet in async).
    """
    if not update.effective_chat or update.effective_chat.type == "private":
        return False

    tg_user = update.effective_user
    group_id = update.effective_chat.id
    thread_id = getattr(update.effective_message, "message_thread_id", None)

    topic = await detect_topic(0, group_id, thread_id)  # user_id unused now
    logger.debug("show_topic_menu: topic=%s thread=%s group=%s", topic, thread_id, group_id)

    if topic == "general":
        return False  # let caller show default menu

    try:
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, tg_user.id)
            if not user:
                return False
            us = await get_or_create_settings(session, user)

            # Session stays open → ORM objects are live throughout
            if topic == "signals":
                await _show_signals_menu(update, user, us)
            elif topic == "traders":
                await _show_traders_menu(update, user, us)
            elif topic == "portfolio":
                await _show_portfolio_menu(update, user, us)
            elif topic == "alerts":
                await _show_alerts_menu(update, user, us)
            elif topic == "admin":
                await _show_admin_menu(update, user, us)
            else:
                return False

        return True

    except Exception as e:
        logger.warning("show_topic_menu error (topic=%s): %s", topic, e, exc_info=True)
        return False


# ─────────────────────────────────────────────
# 📊 Signals topic
# ─────────────────────────────────────────────

async def _show_signals_menu(update: Update, user: User, us) -> None:
    """Écran du topic 📊 Signals — scoring, filtres, derniers signaux."""
    from bot.models.signal_score import SignalScore
    from sqlalchemy import select, func

    scoring_on = bool(getattr(us, "signal_scoring_enabled", True))
    smart_on   = bool(getattr(us, "smart_filter_enabled", True))
    coin_skip  = bool(getattr(us, "skip_coin_flip", True))
    min_score  = float(getattr(us, "min_signal_score", 40.0))
    min_conv   = float(getattr(us, "min_conviction_pct", 2.0))
    max_drift  = float(getattr(us, "max_price_drift_pct", 5.0))

    # Stats signaux récents
    total_signals = 0
    passed_signals = 0
    avg_score = 0.0
    try:
        async with async_session() as session:
            total_signals = (await session.scalar(
                select(func.count(SignalScore.id))
            )) or 0
            passed_signals = (await session.scalar(
                select(func.count(SignalScore.id)).where(SignalScore.passed == True)  # noqa
            )) or 0
            avg_score = float((await session.scalar(
                select(func.avg(SignalScore.total_score))
            )) or 0)
    except Exception:
        pass

    block_rate = ((total_signals - passed_signals) / total_signals * 100) if total_signals > 0 else 0
    pass_rate  = 100 - block_rate
    score_bar  = bar(min_score, 100, 12)

    on  = "✅"
    off = "❌"

    lines = [
        f"📊 *SIGNAUX & SCORING*\n{SEP}\n",
        f"*Scoring intelligent :* {on if scoring_on else off}",
        f"  Score minimum : *{min_score:.0f}/100*",
        f"  {score_bar} seuil",
        f"  Score moyen reçu : *{avg_score:.0f}/100*\n",
        f"*Filtres actifs :*",
        f"  Smart Filter : {on if smart_on else off}",
        f"  Skip Coin-Flip : {on if coin_skip else off}",
        f"  Conviction min : *{min_conv:.0f}%* du portfolio trader",
        f"  Drift max autorisé : *{max_drift:.0f}%*\n",
    ]

    if total_signals > 0:
        accept_bar = bar(pass_rate, 100, 12)
        lines += [
            f"*Historique :* {total_signals} signaux analysés",
            f"  {accept_bar} *{pass_rate:.0f}%* acceptés / *{block_rate:.0f}%* bloqués\n",
        ]

    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton(f"🎯 Score min : {min_score:.0f}", callback_data="set_min_signal_score"),
            InlineKeyboardButton(f"🧠 Scoring : {on if scoring_on else off}", callback_data="set_signal_scoring_enabled"),
        ],
        [
            InlineKeyboardButton("📐 Critères de scoring", callback_data="set_scoring_criteria_menu"),
            InlineKeyboardButton(f"🔍 Smart Filter : {on if smart_on else off}", callback_data="set_smart_filter_enabled"),
        ],
        [
            InlineKeyboardButton(f"🪙 Skip coin-flip : {on if coin_skip else off}", callback_data="set_skip_coin_flip"),
            InlineKeyboardButton(f"📉 Conviction ≥ {min_conv:.0f}%", callback_data="set_min_conviction_pct"),
        ],
        [InlineKeyboardButton("📊 Mes positions ouvertes", callback_data="menu_positions")],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    await update.effective_message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─────────────────────────────────────────────
# 👤 Traders topic
# ─────────────────────────────────────────────

async def _show_traders_menu(update: Update, user: User, us) -> None:
    """Écran du topic 👤 Traders — suivi, stats, pause."""
    from bot.models.trader_stats import TraderStats
    from sqlalchemy import select

    followed = list(getattr(us, "followed_wallets", None) or [])
    auto_pause = bool(getattr(us, "auto_pause_cold_traders", True))
    cold_thresh = float(getattr(us, "cold_trader_threshold", 40.0))
    hot_boost  = float(getattr(us, "hot_streak_boost", 1.5))

    # Récupère les stats trader pour les wallets suivis
    trader_lines = []
    try:
        async with async_session() as session:
            for wallet in followed[:8]:
                stat = (await session.execute(
                    select(TraderStats).where(
                        TraderStats.wallet == wallet,
                        TraderStats.period == "7d",
                    )
                )).scalar_one_or_none()

                short = sw(wallet)
                if stat:
                    badge = badge_trader_status(stat.win_rate, stat.trade_count)
                    wr_bar = bar(stat.win_rate, 100, 8)
                    pnl_sign = "+" if stat.total_pnl >= 0 else ""
                    paused = " ⏸️" if stat.auto_paused else ""
                    trader_lines.append(
                        f"  {badge} `{short}` — *{stat.win_rate:.0f}%* WR "
                        f"{wr_bar}\n"
                        f"    {stat.trade_count} trades | PNL: *{pnl_sign}{fmt_usd(stat.total_pnl)}*{paused}"
                    )
                else:
                    trader_lines.append(f"  ⏳ `{short}` — stats en cours de calcul")
    except Exception:
        trader_lines = [f"  • `{sw(w)}`" for w in followed[:8]]

    n = len(followed)
    on = "✅"
    off = "❌"

    lines = [
        f"👤 *TRADERS SUIVIS*\n{SEP}\n",
        f"*{n} trader{'s' if n > 1 else ''} suivi{'s' if n > 1 else ''}*\n",
    ]
    if trader_lines:
        lines += trader_lines
        lines.append("")
    else:
        lines.append("_Aucun trader suivi pour l'instant._\n")

    lines += [
        f"*Protection automatique :*",
        f"  Auto-pause cold traders : {on if auto_pause else off}",
        f"  Seuil cold : *{cold_thresh:.0f}%* WR",
        f"  Boost hot trader : *×{hot_boost:.1f}*",
    ]

    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("➕ Suivre un trader", callback_data="set_add_wallet"),
            InlineKeyboardButton("➖ Retirer un trader", callback_data="set_followed"),
        ],
        [
            InlineKeyboardButton("📊 Analytics détaillés", callback_data="v3_analytics"),
            InlineKeyboardButton("🔄 Rafraîchir", callback_data="menu_traders"),
        ],
        [
            InlineKeyboardButton(f"⏸️ Auto-pause : {on if auto_pause else off}", callback_data="set_auto_pause_cold_traders"),
            InlineKeyboardButton(f"🥶 Seuil : {cold_thresh:.0f}%", callback_data="set_cold_trader_threshold"),
        ],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    await update.effective_message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─────────────────────────────────────────────
# 💼 Portfolio topic
# ─────────────────────────────────────────────

async def _show_portfolio_menu(update: Update, user: User, us) -> None:
    """Écran du topic 💼 Portfolio — positions, PNL, exposition."""
    from bot.models.active_position import ActivePosition
    from bot.models.trade import Trade, TradeStatus, TradeSide
    from sqlalchemy import select, and_

    max_pos     = int(getattr(us, "max_positions", 15))
    max_cat_exp = float(getattr(us, "max_category_exposure_pct", 30.0))
    max_dir     = float(getattr(us, "max_direction_bias_pct", 70.0))

    # Compte les positions ouvertes + PNL approximatif
    open_count = 0
    total_invested = 0.0
    yes_count = 0
    no_count  = 0
    categories: dict[str, int] = {}

    try:
        async with async_session() as session:
            # Active positions (V3 tracking)
            positions = list((await session.execute(
                select(ActivePosition).where(
                    and_(ActivePosition.user_id == user.id,
                         ActivePosition.is_closed == False)  # noqa
                )
            )).scalars().all())
            open_count = len(positions)

            for p in positions:
                shares = p.shares or 0
                entry  = p.entry_price or 0
                total_invested += shares * entry
                if p.outcome and p.outcome.upper() in ("YES", "Y", "BUY"):
                    yes_count += 1
                else:
                    no_count += 1
                cat = "Autre"
                if p.market_question:
                    q = p.market_question.lower()
                    if any(k in q for k in ("btc", "bitcoin", "eth", "crypto", "sol")):
                        cat = "Crypto"
                    elif any(k in q for k in ("trump", "biden", "election", "president")):
                        cat = "Politique"
                    elif any(k in q for k in ("nfl", "nba", "soccer", "football")):
                        cat = "Sports"
                    elif any(k in q for k in ("fed", "gdp", "inflation", "rate")):
                        cat = "Économie"
                categories[cat] = categories.get(cat, 0) + 1
    except Exception as e:
        logger.debug("portfolio topic fetch error: %s", e)

    pos_bar  = bar(open_count, max_pos, 12)
    yes_pct  = round(yes_count / open_count * 100, 0) if open_count else 0
    no_pct   = 100 - yes_pct
    dir_bar  = bar_bicolor(yes_count, no_count, max(open_count, 1), 12)

    lines = [
        f"💼 *PORTFOLIO*\n{SEP}\n",
        f"*{open_count}/{max_pos}* positions ouvertes",
        f"{pos_bar} {open_count}/{max_pos}\n",
    ]

    if total_invested > 0:
        lines.append(f"💵 Investi : *{fmt_usd(total_invested)}*\n")

    if open_count > 0:
        lines += [
            f"*Direction :*",
            f"  {dir_bar}",
            f"  YES *{yes_pct:.0f}%* / NO *{no_pct:.0f}%*\n",
        ]
        if categories:
            lines.append("*Exposition par catégorie :*")
            for cat, cnt in sorted(categories.items(), key=lambda x: -x[1]):
                pct = round(cnt / open_count * 100, 0)
                cat_bar = bar(pct, 100, 10)
                over = " ⚠️" if pct > max_cat_exp else ""
                lines.append(f"  {cat_bar} {cat} *{pct:.0f}%*{over}")
            lines.append("")
    else:
        lines.append("_Aucune position ouverte._\n")

    lines += [
        f"*Limites de risque :*",
        f"  Max positions : *{max_pos}*",
        f"  Max catégorie : *{max_cat_exp:.0f}%*",
        f"  Biais direction max : *{max_dir:.0f}%*",
    ]

    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("📊 Détail des positions", callback_data="menu_positions"),
            InlineKeyboardButton("📜 Historique", callback_data="menu_history"),
        ],
        [
            InlineKeyboardButton(f"📦 Max positions : {max_pos}", callback_data="set_max_positions"),
            InlineKeyboardButton(f"📂 Max catégorie : {max_cat_exp:.0f}%", callback_data="set_max_category_exposure_pct"),
        ],
        [
            InlineKeyboardButton(f"⚖️ Biais max : {max_dir:.0f}%", callback_data="set_max_direction_bias_pct"),
            InlineKeyboardButton("🔄 Rafraîchir", callback_data="menu_portfolio_refresh"),
        ],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    await update.effective_message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─────────────────────────────────────────────
# 🚨 Alerts topic
# ─────────────────────────────────────────────

async def _show_alerts_menu(update: Update, user: User, us) -> None:
    """Écran du topic 🚨 Alerts — SL/TP/trailing/time exit."""
    sl_on       = bool(getattr(us, "stop_loss_enabled", True))
    sl_pct      = float(getattr(us, "stop_loss_pct", 20.0))
    tp_on       = bool(getattr(us, "take_profit_enabled", False))
    tp_pct      = float(getattr(us, "take_profit_pct", 50.0))
    trail_on    = bool(getattr(us, "trailing_stop_enabled", False))
    trail_pct   = float(getattr(us, "trailing_stop_pct", 10.0))
    time_on     = bool(getattr(us, "time_exit_enabled", False))
    time_h      = int(getattr(us, "time_exit_hours", 24))
    scale_on    = bool(getattr(us, "scale_out_enabled", False))
    scale_pct   = float(getattr(us, "scale_out_pct", 50.0))

    on  = "✅"
    off = "❌"

    def _status(enabled, value, unit=""):
        if enabled:
            return f"{on} *{value:.0f}{unit}*"
        return f"{off} désactivé"

    lines = [
        f"🚨 *ALERTES & PROTECTIONS*\n{SEP}\n",
        f"*🛑 Stop-Loss :* {_status(sl_on, sl_pct, '%')}",
        f"   Clôture si position baisse de *{sl_pct:.0f}%*\n",
        f"*🎯 Take-Profit :* {_status(tp_on, tp_pct, '%')}",
        f"   Clôture si position monte de *{tp_pct:.0f}%*\n",
        f"*📉 Trailing Stop :* {_status(trail_on, trail_pct, '%')}",
        f"   Suit le prix — déclenche si repli de *{trail_pct:.0f}%* depuis le sommet\n",
        f"*⏰ Time Exit :* {_status(time_on, time_h, 'h')}",
        f"   Clôture automatiquement après *{time_h}h* si toujours ouverte\n",
        f"*📤 Scale-Out :* {_status(scale_on, scale_pct, '%')}",
        f"   Prend *{scale_pct:.0f}%* des gains au TP puis laisse courir",
    ]

    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton(f"🛑 Stop-Loss : {sl_pct:.0f}%", callback_data="set_stop_loss_menu"),
            InlineKeyboardButton(f"🎯 Take-Profit : {'ON' if tp_on else 'OFF'}", callback_data="set_take_profit_menu"),
        ],
        [
            InlineKeyboardButton(f"📉 Trailing : {'ON' if trail_on else 'OFF'}", callback_data="set_v3_positions"),
            InlineKeyboardButton(f"⏰ Time Exit : {'ON' if time_on else 'OFF'}", callback_data="set_v3_positions"),
        ],
        [
            InlineKeyboardButton(f"📤 Scale-Out : {'ON' if scale_on else 'OFF'}", callback_data="set_v3_positions"),
            InlineKeyboardButton("📊 Positions ouvertes", callback_data="menu_positions"),
        ],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    await update.effective_message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─────────────────────────────────────────────
# ⚙️ Admin topic
# ─────────────────────────────────────────────

async def _show_admin_menu(update: Update, user: User, us) -> None:
    """Écran du topic ⚙️ Admin — statut bot, wallet, paramètres."""
    from bot.services.web3_client import polygon_client

    is_active   = user.is_active
    is_paused   = user.is_paused
    paper       = user.paper_trading
    wallet_addr = user.wallet_address or ""
    notif_mode  = getattr(us, "notification_mode", "group")

    # Status
    if is_paused:
        status_icon, status_text = "⏸️", "Pause"
    elif is_active:
        status_icon, status_text = "🟢", "Actif"
    else:
        status_icon, status_text = "🔴", "Inactif"

    mode_icon = "📝" if paper else "💵"
    mode_text = "Paper Trading" if paper else "Live"
    notif_icon = "💬" if notif_mode == "dm" else ("📊" if notif_mode == "group" else "🔀")
    notif_text = {"dm": "DM seulement", "group": "Groupe seulement", "both": "DM + Groupe"}.get(notif_mode, notif_mode)

    # Solde wallet
    usdc_str = "—"
    pol_str  = "—"
    if wallet_addr:
        try:
            if paper:
                usdc_str = fmt_usd(user.paper_balance or 0)
                pol_str  = "N/A (paper)"
            else:
                usdc, _ = await polygon_client.get_usdc_balances(wallet_addr)
                pol      = await polygon_client.get_matic_balance(wallet_addr)
                usdc_str = fmt_usd(usdc)
                pol_str  = f"{pol:.4f} POL"
        except Exception:
            pass

    wallet_short = f"`{wallet_addr[:6]}...{wallet_addr[-4:]}`" if wallet_addr else "non configuré"

    lines = [
        f"⚙️ *ADMINISTRATION*\n{SEP}\n",
        f"*Statut :* {status_icon} {status_text}",
        f"*Mode :* {mode_icon} {mode_text}",
        f"*Notifications :* {notif_icon} {notif_text}\n",
        f"*Wallet :* {wallet_short}",
        f"  💵 USDC : *{usdc_str}*",
        f"  ⛽ Gas : *{pol_str}*\n",
        f"*Scoring :* {'✅ actif' if getattr(us, 'signal_scoring_enabled', True) else '❌ inactif'} "
        f"| Seuil : *{getattr(us, 'min_signal_score', 40):.0f}/100*",
        f"*SL :* {'✅' if getattr(us, 'stop_loss_enabled', True) else '❌'} "
        f"*{getattr(us, 'stop_loss_pct', 20):.0f}%* "
        f"| *TP :* {'✅' if getattr(us, 'take_profit_enabled', False) else '❌'}",
    ]

    text = "\n".join(lines)

    # Bouton pause/resume selon état
    if is_paused:
        ctrl_btn = InlineKeyboardButton("▶️ Reprendre le copy", callback_data="resume_copy")
    elif is_active:
        ctrl_btn = InlineKeyboardButton("⏸️ Mettre en pause", callback_data="stop_copy")
    else:
        ctrl_btn = InlineKeyboardButton("▶️ Activer le copy", callback_data="resume_copy")

    keyboard = [
        [
            InlineKeyboardButton("⚙️ Tous les paramètres", callback_data="menu_settings"),
            InlineKeyboardButton("👛 Mon wallet", callback_data="menu_balance"),
        ],
        [
            InlineKeyboardButton("💳 Déposer des USDC", callback_data="menu_deposit"),
            InlineKeyboardButton("💸 Retirer", callback_data="menu_withdraw"),
        ],
        [
            ctrl_btn,
            InlineKeyboardButton(f"{mode_icon} Changer de mode", callback_data="set_paper_trading"),
        ],
        [
            InlineKeyboardButton(f"{notif_icon} Notifs : {notif_text[:12]}", callback_data="set_v3_notif"),
            InlineKeyboardButton("📊 Mon groupe", callback_data="menu_mygroup"),
        ],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    await update.effective_message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )
