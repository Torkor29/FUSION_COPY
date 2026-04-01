"""Group-context action interceptor — V3 Multi-tenant.

Enregistré à group=-1 (avant le ConversationHandler de settings.py).

Trois modes selon le type d'action :

  1. TOGGLE      → flip en DB + rafraîchit le topic menu (aucune navigation)
  2. PRESET_PICK → mini-picker de valeurs prédéfinies dans le topic courant
                   bouton "Autre" → prompt dans le topic Général
  3. REPORT      → génère le rapport et le poste dans le bon topic
  4. DM_ONLY     → wallet add/remove + notifs (les seuls vrais DM)

Principe du topic Général :
  • Toutes les saisies libres se font dans le Général (pas de pollution des topics spécifiques)
  • Les résultats (rapports, confirmations) sont postés dans le topic métier adapté
  • thread_id absent = Général dans un Forum Group Telegram
"""

import logging
import re
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id, get_or_create_settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 1. TOGGLES — bascule directe
# callback_data → champ UserSettings
# ─────────────────────────────────────────────────────────────

INLINE_TOGGLES: dict[str, str] = {
    "set_signal_scoring_enabled":  "signal_scoring_enabled",
    "set_smart_filter_enabled":    "smart_filter_enabled",
    "set_skip_coin_flip":          "skip_coin_flip",
    "set_auto_pause_cold_traders": "auto_pause_cold_traders",
    "set_stop_loss_enabled":       "stop_loss_enabled",
    "set_take_profit_enabled":     "take_profit_enabled",
    "set_trailing_stop_enabled":   "trailing_stop_enabled",
    "set_time_exit_enabled":       "time_exit_enabled",
    "set_scale_out_enabled":       "scale_out_enabled",
}

_TOGGLE_LABELS: dict[str, tuple[str, str]] = {
    "signal_scoring_enabled":  ("✅ Scoring activé",        "❌ Scoring désactivé"),
    "smart_filter_enabled":    ("✅ Smart Filter activé",   "❌ Smart Filter désactivé"),
    "skip_coin_flip":          ("✅ Coin-flip ignoré",      "❌ Coin-flip inclus"),
    "auto_pause_cold_traders": ("✅ Auto-pause activé",     "❌ Auto-pause désactivé"),
    "stop_loss_enabled":       ("✅ Stop-Loss activé",      "❌ Stop-Loss désactivé"),
    "take_profit_enabled":     ("✅ Take-Profit activé",    "❌ Take-Profit désactivé"),
    "trailing_stop_enabled":   ("✅ Trailing Stop activé",  "❌ Trailing Stop désactivé"),
    "time_exit_enabled":       ("✅ Time Exit activé",      "❌ Time Exit désactivé"),
    "scale_out_enabled":       ("✅ Scale-Out activé",      "❌ Scale-Out désactivé"),
}


# ─────────────────────────────────────────────────────────────
# 2. PRESET PICKERS — mini-menu de valeurs dans le topic courant
#
# Format callback : "grp_set:{field}:{value}"
# ─────────────────────────────────────────────────────────────

# callback_data → (label affiché, champ UserSettings, unité, [(label_btn, valeur_str), ...])
PRESET_PICKERS: dict[str, tuple[str, str, str, list[tuple[str, str]]]] = {
    "set_min_signal_score": (
        "🎯 Score minimum du signal",
        "min_signal_score",
        "/100",
        [("25", "25"), ("30", "30"), ("40", "40"), ("50", "50"), ("65", "65"), ("75", "75")],
    ),
    "set_cold_trader_threshold": (
        "🥶 Seuil trader froid",
        "cold_trader_threshold",
        "%",
        [("30%", "30"), ("35%", "35"), ("40%", "40"), ("45%", "45"), ("50%", "50")],
    ),
    "set_hot_streak_boost": (
        "🔥 Boost hot streak",
        "hot_streak_boost",
        "×",
        [("×1.0", "1.0"), ("×1.2", "1.2"), ("×1.5", "1.5"), ("×1.8", "1.8"), ("×2.0", "2.0")],
    ),
    "set_min_conviction_pct": (
        "💪 Conviction minimum",
        "min_conviction_pct",
        "%",
        [("1%", "1"), ("2%", "2"), ("3%", "3"), ("5%", "5"), ("10%", "10")],
    ),
    "set_max_positions": (
        "📦 Positions maximum",
        "max_positions",
        "",
        [("5", "5"), ("8", "8"), ("10", "10"), ("15", "15"), ("20", "20"), ("25", "25")],
    ),
    "set_max_category_exposure_pct": (
        "📂 Exposition max par catégorie",
        "max_category_exposure_pct",
        "%",
        [("20%", "20"), ("25%", "25"), ("30%", "30"), ("40%", "40"), ("50%", "50")],
    ),
    "set_max_direction_bias_pct": (
        "⚖️ Biais de direction max",
        "max_direction_bias_pct",
        "%",
        [("60%", "60"), ("65%", "65"), ("70%", "70"), ("80%", "80"), ("90%", "90")],
    ),
    "set_trailing_stop_pct": (
        "📉 Trailing stop %",
        "trailing_stop_pct",
        "%",
        [("5%", "5"), ("8%", "8"), ("10%", "10"), ("15%", "15"), ("20%", "20")],
    ),
    "set_time_exit_hours": (
        "⏰ Time exit (heures)",
        "time_exit_hours",
        "h",
        [("6h", "6"), ("12h", "12"), ("24h", "24"), ("48h", "48"), ("72h", "72")],
    ),
    "set_scale_out_pct": (
        "📤 Scale-Out %",
        "scale_out_pct",
        "%",
        [("25%", "25"), ("33%", "33"), ("50%", "50"), ("66%", "66"), ("75%", "75")],
    ),
    "set_stop_loss_pct": (
        "🛑 Stop-Loss",
        "stop_loss_pct",
        "%",
        [("10%", "10"), ("15%", "15"), ("20%", "20"), ("25%", "25"), ("30%", "30")],
    ),
    "set_take_profit_pct": (
        "🎯 Take-Profit",
        "take_profit_pct",
        "%",
        [("25%", "25"), ("33%", "33"), ("50%", "50"), ("75%", "75"), ("100%", "100")],
    ),
}

# Boutons qui ouvrent un sous-menu → cible preset ou sous-menu dédié
_MENU_TO_PRESET: dict[str, str] = {
    "set_v3_positions":          "_positions_submenu",   # sous-menu dédié
    "set_v3_smart":              "_smart_submenu",
    "set_v3_portfolio":          "_portfolio_submenu",
    "set_scoring_criteria_menu": "_smart_submenu",
}


# ─────────────────────────────────────────────────────────────
# 3. REPORT ACTIONS — génère et poste dans le bon topic
# ─────────────────────────────────────────────────────────────

REPORT_ACTIONS: set[str] = {
    "v3_analytics",           # analytics traders → topic 👤 Traders
    "menu_portfolio_refresh", # refresh → topic 💼 Portfolio
}


# ─────────────────────────────────────────────────────────────
# 4. DM SEULEMENT — multi-step ou sécurité
# ─────────────────────────────────────────────────────────────

DM_ONLY: dict[str, tuple[str, str, str]] = {
    "set_add_wallet": (
        "➕ *Ajouter un trader*\n\nSuivez un nouveau wallet Polymarket.\n"
        "_L'ajout utilise plusieurs étapes — mieux en DM._",
        "➕ Ajouter un trader",
        "set_add_wallet",
    ),
    "set_followed": (
        "👤 *Gérer les traders suivis*\n\nRetirer ou modifier les wallets copiés.",
        "👤 Gérer les traders",
        "set_followed",
    ),
    "set_v3_notif": (
        "📬 *Notifications*\n\nChoisissez comment recevoir les alertes :\n"
        "DM uniquement / Groupe uniquement / Les deux.\n\n"
        "_Ce paramètre concerne le DM lui-même, donc on le configure là-bas._",
        "📬 Configurer les notifications",
        "set_v3_notif",
    ),
    "set_paper_trading": (
        "📝 *Basculer Paper / Live*\n\n"
        "⚠️ En mode *Live*, vos vrais USDC sont utilisés.\n"
        "Vérifiez votre wallet avant d'activer.\n\n"
        "Confirmez ici :",
        "📝 Changer de mode",
        "set_paper_trading",
    ),
}


# Sous-menus de groupement (affichés comme des pickers à plusieurs lignes)
_SUBMENU_LABELS = {
    "_positions_submenu": "📉 Gestion des positions",
    "_smart_submenu":     "🧠 Smart Analysis",
    "_portfolio_submenu": "📦 Risque Portfolio",
}

# Refresh uniquement (topic menu)
REFRESH_ACTIONS: set[str] = {"menu_portfolio_refresh", "menu_traders"}

# Ensemble complet géré
_ALL_HANDLED: frozenset[str] = frozenset(
    list(INLINE_TOGGLES)
    + list(PRESET_PICKERS)
    + list(_MENU_TO_PRESET)
    + list(REPORT_ACTIONS)
    + list(DM_ONLY)
    + list(REFRESH_ACTIONS)
)


# ─────────────────────────────────────────────────────────────
# Handler principal — intercepte avant le ConversationHandler
# ─────────────────────────────────────────────────────────────

async def group_action_interceptor(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    chat = update.effective_chat

    if not chat or chat.type == "private":
        return  # laisse passer au ConversationHandler

    data = (query.data or "").strip()
    if data not in _ALL_HANDLED:
        return

    await query.answer()
    tg_user = update.effective_user

    try:
        if data in INLINE_TOGGLES:
            await _handle_toggle(update, context, tg_user.id, INLINE_TOGGLES[data])

        elif data in PRESET_PICKERS:
            await _show_preset_picker(update, context, tg_user.id, data)

        elif data in _MENU_TO_PRESET:
            target = _MENU_TO_PRESET[data]
            if target in PRESET_PICKERS:
                await _show_preset_picker(update, context, tg_user.id, target)
            else:
                await _show_submenu(update, context, tg_user.id, target)

        elif data in REPORT_ACTIONS:
            await _handle_report_action(update, context, tg_user.id, data)

        elif data in REFRESH_ACTIONS:
            await _refresh_topic_menu(update, context)

        elif data in DM_ONLY:
            intro_text, btn_label, btn_cb = DM_ONLY[data]
            await _send_dm_panel(context, tg_user.id, intro_text, btn_label, btn_cb)
            await query.answer("📬 Envoyé en DM", show_alert=False)

    except Exception as e:
        logger.warning("group_action error (data=%s): %s", data, e, exc_info=True)

    raise ApplicationHandlerStop


# ─────────────────────────────────────────────────────────────
# Handler preset selection — "grp_set:{field}:{value}"
# ─────────────────────────────────────────────────────────────

async def group_preset_select(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Applique la valeur preset choisie et rafraîchit le topic menu."""
    query = update.callback_query
    chat = update.effective_chat

    if not chat or chat.type == "private":
        return

    data = query.data or ""
    parts = data.split(":", 2)  # "grp_set", field, value
    if len(parts) != 3:
        return

    _, field, raw_value = parts
    tg_user = update.effective_user

    await query.answer()

    try:
        # Convertit la valeur en float ou int
        value: float = float(raw_value)
        if value == int(value):
            value = int(value)

        async with async_session() as session:
            user = await get_user_by_telegram_id(session, tg_user.id)
            if not user:
                raise ValueError("Utilisateur non trouvé")
            us = await get_or_create_settings(session, user)
            setattr(us, field, value)
            await session.commit()

        # Trouver l'unité pour le message de confirmation
        unit = ""
        for _, (_, f, u, _) in PRESET_PICKERS.items():
            if f == field:
                unit = u
                break

        await query.answer(f"✅ Mis à jour : {value}{unit}", show_alert=False)

        # Efface le picker et rafraîchit le topic menu
        try:
            await query.message.delete()
        except Exception:
            pass
        await _refresh_topic_menu(update, context)

    except Exception as e:
        logger.warning("preset_select error field=%s value=%s: %s", field, raw_value, e)
        await query.answer("❌ Erreur lors de la mise à jour", show_alert=True)

    raise ApplicationHandlerStop


# ─────────────────────────────────────────────────────────────
# Handler saisie libre — topic Général
# Déclenché quand context.user_data["_pending_group_setting"] est présent
# ─────────────────────────────────────────────────────────────

async def group_free_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Traite une saisie libre dans le groupe pour un paramètre en attente."""
    pending = context.user_data.get("_pending_group_setting")
    if not pending:
        return

    chat = update.effective_chat
    if not chat or chat.type == "private":
        return

    # Vérifier que c'est le bon groupe et le bon user
    if pending.get("chat_id") != chat.id:
        return
    if pending.get("user_id") != update.effective_user.id:
        return

    text = (update.message.text or "").strip()
    field = pending.get("field")
    label = pending.get("label", field)
    unit  = pending.get("unit", "")

    try:
        value: float = float(text.replace(",", "."))
    except ValueError:
        await update.message.reply_text(
            f"❌ *{label}*\n\nValeur invalide : `{text}`\n"
            f"Entrez un nombre (ex: `42`) ou /annuler pour abandonner.",
            parse_mode="Markdown",
        )
        return

    try:
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, update.effective_user.id)
            if not user:
                raise ValueError("Utilisateur non trouvé")
            us = await get_or_create_settings(session, user)
            setattr(us, field, value)
            await session.commit()

        # Nettoie l'état en attente
        context.user_data.pop("_pending_group_setting", None)

        if value == int(value):
            value = int(value)

        await update.message.reply_text(
            f"✅ *{label}* mis à jour : *{value}{unit}*",
            parse_mode="Markdown",
        )

        # Rafraîchit le topic d'origine si connu
        origin_thread = pending.get("origin_thread_id")
        if origin_thread:
            from bot.handlers.topic_menus import detect_topic
            topic = await detect_topic(user.id, chat.id, origin_thread)
            await _post_refresh_to_topic(context, chat.id, origin_thread, user, us, topic)

    except Exception as e:
        logger.warning("free_input error field=%s: %s", field, e)
        await update.message.reply_text("❌ Erreur lors de la mise à jour.", parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────
# Helpers internes
# ─────────────────────────────────────────────────────────────

async def _handle_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int, field: str,
) -> None:
    """Flip un booléen en DB et rafraîchit le topic menu."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, telegram_id)
        if not user:
            return
        us = await get_or_create_settings(session, user)
        current = bool(getattr(us, field, False))
        setattr(us, field, not current)
        await session.commit()

    on_msg, off_msg = _TOGGLE_LABELS.get(field, ("✅ Activé", "❌ Désactivé"))
    await update.callback_query.answer(
        on_msg if not current else off_msg, show_alert=False
    )
    await _refresh_topic_menu(update, context)


async def _show_preset_picker(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int, cb_data: str,
) -> None:
    """Envoie un mini-picker de valeurs prédéfinies dans le topic courant."""
    label, field, unit, presets = PRESET_PICKERS[cb_data]

    # Valeur actuelle
    current_val: Optional[float] = None
    try:
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, telegram_id)
            if user:
                us = await get_or_create_settings(session, user)
                current_val = getattr(us, field, None)
    except Exception:
        pass

    current_str = ""
    if current_val is not None:
        current_str = f"  _(actuel : *{int(current_val) if current_val == int(current_val) else current_val}{unit}*)_"

    # Boutons preset en rangées de 3
    preset_buttons = [
        InlineKeyboardButton(
            f"{'✓ ' if current_val is not None and abs(float(v) - current_val) < 0.01 else ''}{l}",
            callback_data=f"grp_set:{field}:{v}",
        )
        for l, v in presets
    ]
    rows = [preset_buttons[i:i+3] for i in range(0, len(preset_buttons), 3)]

    # Bouton "Autre" → saisie libre dans le topic Général
    thread_id = getattr(update.effective_message, "message_thread_id", None)
    rows.append([
        InlineKeyboardButton(
            "✏️ Autre valeur (dans le Général)",
            callback_data=f"grp_free:{field}:{unit}:{label}:{thread_id or 0}",
        )
    ])
    rows.append([InlineKeyboardButton("✖️ Annuler", callback_data="grp_cancel_picker")])

    await update.effective_message.reply_text(
        f"*{label}*{current_str}\n\nChoisissez une valeur :",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _show_submenu(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int, submenu_key: str,
) -> None:
    """Affiche un sous-menu regroupant plusieurs presets."""
    title = _SUBMENU_LABELS.get(submenu_key, "Paramètres")

    # Mapping sous-menu → champs concernés
    submenu_fields: dict[str, list[str]] = {
        "_positions_submenu": [
            "set_stop_loss_enabled",
            "set_stop_loss_pct",
            "set_take_profit_enabled",
            "set_take_profit_pct",
            "set_trailing_stop_enabled",
            "set_trailing_stop_pct",
            "set_time_exit_enabled",
            "set_time_exit_hours",
            "set_scale_out_enabled",
            "set_scale_out_pct",
        ],
        "_smart_submenu": [
            "set_signal_scoring_enabled",
            "set_min_signal_score",
            "set_smart_filter_enabled",
            "set_skip_coin_flip",
            "set_min_conviction_pct",
            "set_cold_trader_threshold",
            "set_hot_streak_boost",
        ],
        "_portfolio_submenu": [
            "set_max_positions",
            "set_max_category_exposure_pct",
            "set_max_direction_bias_pct",
        ],
    }

    fields = submenu_fields.get(submenu_key, [])
    rows: list[list[InlineKeyboardButton]] = []

    # Valeurs actuelles
    current_vals: dict[str, object] = {}
    try:
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, telegram_id)
            if user:
                us = await get_or_create_settings(session, user)
                for cb in fields:
                    if cb in PRESET_PICKERS:
                        _, f, _, _ = PRESET_PICKERS[cb]
                        current_vals[cb] = getattr(us, f, None)
                    elif cb in INLINE_TOGGLES:
                        current_vals[cb] = bool(getattr(us, INLINE_TOGGLES[cb], False))
    except Exception:
        pass

    for cb in fields:
        if cb in INLINE_TOGGLES:
            is_on = bool(current_vals.get(cb, False))
            lbl, field_ = (("✅", INLINE_TOGGLES[cb]) if is_on else ("❌", INLINE_TOGGLES[cb]))
            # Label lisible depuis _TOGGLE_LABELS
            on_msg, off_msg = _TOGGLE_LABELS.get(field_, ("ON", "OFF"))
            btn_text = on_msg if is_on else off_msg
            rows.append([InlineKeyboardButton(btn_text, callback_data=cb)])
        elif cb in PRESET_PICKERS:
            lbl, field_, unit_, _ = PRESET_PICKERS[cb]
            val = current_vals.get(cb)
            val_str = f" : {int(val) if val is not None and val == int(val) else val}{unit_}" if val is not None else ""
            rows.append([InlineKeyboardButton(f"{lbl}{val_str}", callback_data=cb)])

    rows.append([InlineKeyboardButton("✖️ Fermer", callback_data="grp_cancel_picker")])

    await update.effective_message.reply_text(
        f"*{title}*\n\nSélectionnez un paramètre à modifier :",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _handle_report_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int, action: str,
) -> None:
    """Génère un rapport et le poste dans le bon topic, confirme localement."""
    chat = update.effective_chat
    if not chat:
        return

    if action == "v3_analytics":
        # Poste dans le topic 👤 Traders via TopicRouter
        try:
            from bot.services.topic_router import TopicRouter
            bot = context.bot
            async with async_session() as session:
                user = await get_user_by_telegram_id(session, telegram_id)
                db_user_id = user.id if user else None
            user_router = await TopicRouter.for_user(db_user_id, bot) if db_user_id else None
            if user_router:
                # Texte de rapport basique (enrichi plus tard par TraderTracker)
                await user_router.send_trader_report(
                    "📊 *Rapport Analytics*\n\n"
                    "_Calcul en cours… Revenez dans quelques instants._"
                )
                await update.effective_message.reply_text(
                    "📊 Rapport posté dans le topic 👤 Traders ↑",
                    parse_mode="Markdown",
                )
            else:
                await _refresh_topic_menu(update, context)
        except Exception as e:
            logger.warning("report action failed: %s", e)
            await _refresh_topic_menu(update, context)
    else:
        # Simple refresh du topic courant
        await _refresh_topic_menu(update, context)


async def _refresh_topic_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Ré-affiche le menu du topic courant (nouveau message)."""
    from bot.handlers.topic_menus import show_topic_menu
    try:
        shown = await show_topic_menu(update, context)
        if not shown:
            from bot.handlers.menu import _send_main_menu
            await _send_main_menu(update.effective_message, update.effective_user)
    except Exception as e:
        logger.debug("_refresh_topic_menu: %s", e)


async def _post_refresh_to_topic(
    context, chat_id: int, thread_id: int,
    user, us, topic: str,
) -> None:
    """Poste un rappel dans un topic spécifique après une mise à jour depuis le Général."""
    topic_names = {
        "signals":   "📊 Signals",
        "traders":   "👤 Traders",
        "portfolio": "💼 Portfolio",
        "alerts":    "🚨 Alerts",
        "admin":     "⚙️ Admin",
    }
    name = topic_names.get(topic, "")
    if name and thread_id:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=f"🔄 _Paramètre mis à jour depuis le Général — tapez /menu pour voir les nouveaux réglages._",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def _send_dm_panel(
    context, telegram_id: int,
    intro_text: str, btn_label: str, btn_callback: str,
) -> None:
    """Envoie un panneau de configuration en DM."""
    try:
        await context.bot.send_message(
            chat_id=telegram_id,
            text=(
                f"{intro_text}\n\n"
                "_Cliquez ci-dessous pour ouvrir le panneau de configuration._"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(btn_label, callback_data=btn_callback)
            ]]),
        )
    except Exception as e:
        raise RuntimeError(
            "Impossible d'envoyer un DM — démarrez d'abord une conversation "
            "privée avec le bot."
        ) from e


# ─────────────────────────────────────────────────────────────
# Handler "saisie libre" — bouton "✏️ Autre valeur"
# callback_data = "grp_free:{field}:{unit}:{label}:{origin_thread_id}"
# ─────────────────────────────────────────────────────────────

async def group_free_trigger(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Déclenche une saisie libre dans le topic Général."""
    query = update.callback_query
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return

    data = query.data or ""
    parts = data.split(":", 4)
    if len(parts) < 5:
        return

    _, field, unit, label, origin_thread_str = parts
    try:
        origin_thread = int(origin_thread_str)
    except ValueError:
        origin_thread = None

    await query.answer()

    # Supprime le picker
    try:
        await query.message.delete()
    except Exception:
        pass

    # Stocke le paramètre en attente
    context.user_data["_pending_group_setting"] = {
        "field":            field,
        "unit":             unit,
        "label":            label,
        "chat_id":          chat.id,
        "user_id":          update.effective_user.id,
        "origin_thread_id": origin_thread if origin_thread else None,
    }

    # Envoie la demande dans le Général (sans message_thread_id)
    await context.bot.send_message(
        chat_id=chat.id,
        text=(
            f"⚙️ *{label}*\n\n"
            f"Entrez la nouvelle valeur{' (' + unit + ')' if unit else ''} "
            f"directement ici, ou tapez /annuler pour abandonner."
        ),
        parse_mode="Markdown",
    )

    raise ApplicationHandlerStop


async def group_cancel_picker(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Annule un picker ou une saisie libre en attente."""
    query = update.callback_query
    await query.answer("Annulé")
    try:
        await query.message.delete()
    except Exception:
        pass
    context.user_data.pop("_pending_group_setting", None)
    raise ApplicationHandlerStop


# ─────────────────────────────────────────────────────────────
# Enregistrement
# ─────────────────────────────────────────────────────────────

def get_group_action_handlers() -> list:
    """Handlers à enregistrer à group=-1 (avant le ConversationHandler)."""
    pattern_main = "^(" + "|".join(re.escape(k) for k in sorted(_ALL_HANDLED)) + ")$"

    return [
        # 1. Intercepteur principal (toggles, presets, reports, DM redirects)
        CallbackQueryHandler(group_action_interceptor, pattern=pattern_main),

        # 2. Sélection d'un preset ("grp_set:{field}:{value}")
        CallbackQueryHandler(group_preset_select, pattern=r"^grp_set:"),

        # 3. Déclencheur saisie libre ("grp_free:{field}:{unit}:{label}:{thread}")
        CallbackQueryHandler(group_free_trigger, pattern=r"^grp_free:"),

        # 4. Annulation d'un picker
        CallbackQueryHandler(group_cancel_picker, pattern=r"^grp_cancel_picker$"),

        # 5. Saisie libre dans le groupe (MessageHandler — texte en attente)
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
            group_free_input,
        ),
    ]
