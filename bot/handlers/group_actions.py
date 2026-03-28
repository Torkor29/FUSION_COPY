"""Group-context action interceptor — V3 Multi-tenant.

Intercepte les callbacks de paramètres (set_*) dans les groupes
AVANT que le ConversationHandler ne les attrape (enregistré à group=-1).

Deux modes de traitement :
  1. Toggles instantanés  → flip en DB + rafraîchit le topic menu
  2. Saisies / flows      → envoie un DM avec le panneau adapté

Pourquoi group=-1 ?
  Le ConversationHandler de settings.py est au group=0 (défaut).
  En levant ApplicationHandlerStop depuis group=-1, on empêche le
  ConversationHandler de démarrer une conversation dans le groupe
  (ce qui poserait de l'input text dans le topic = mauvaise UX).
"""

import logging
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ApplicationHandlerStop, CallbackQueryHandler

from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id, get_or_create_settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Toggles : bascule directe, sans saisie clavier
# callback_data → champ UserSettings
# ─────────────────────────────────────────────────────────────

INLINE_TOGGLES: dict[str, str] = {
    "set_signal_scoring_enabled":  "signal_scoring_enabled",
    "set_smart_filter_enabled":    "smart_filter_enabled",
    "set_skip_coin_flip":          "skip_coin_flip",
    "set_auto_pause_cold_traders": "auto_pause_cold_traders",
    "set_trailing_stop_enabled":   "trailing_stop_enabled",
    "set_time_exit_enabled":       "time_exit_enabled",
    "set_scale_out_enabled":       "scale_out_enabled",
}

_TOGGLE_LABELS: dict[str, tuple[str, str]] = {
    "signal_scoring_enabled":  ("✅ Scoring activé",           "❌ Scoring désactivé"),
    "smart_filter_enabled":    ("✅ Smart Filter activé",      "❌ Smart Filter désactivé"),
    "skip_coin_flip":          ("✅ Coin-flip ignoré",         "❌ Coin-flip inclus"),
    "auto_pause_cold_traders": ("✅ Auto-pause activé",        "❌ Auto-pause désactivé"),
    "trailing_stop_enabled":   ("✅ Trailing Stop activé",     "❌ Trailing Stop désactivé"),
    "time_exit_enabled":       ("✅ Time Exit activé",         "❌ Time Exit désactivé"),
    "scale_out_enabled":       ("✅ Scale-Out activé",         "❌ Scale-Out désactivé"),
}


# ─────────────────────────────────────────────────────────────
# Actions de rafraîchissement : ré-affiche le topic menu
# ─────────────────────────────────────────────────────────────

REFRESH_ACTIONS: set[str] = {
    "menu_portfolio_refresh",
    "menu_traders",        # bouton 🔄 Rafraîchir dans le topic traders
}


# ─────────────────────────────────────────────────────────────
# Redirections DM : tout ce qui nécessite une saisie ou un flow
# callback_data → (texte intro DM, label bouton, callback bouton)
# ─────────────────────────────────────────────────────────────

DM_REDIRECTS: dict[str, tuple[str, str, str]] = {
    # ── Smart Analysis ─────────────────────────────────────
    "set_v3_smart": (
        "🧠 *Smart Analysis*\n\nScoring de signaux, filtres intelligents et tracking des traders.",
        "⚙️ Ouvrir Smart Analysis",
        "set_v3_smart",
    ),
    "set_min_signal_score": (
        "🎯 *Score minimum du signal*\n\nDéfinissez le seuil en dessous duquel un signal est ignoré (0-100).",
        "🧠 Ouvrir Smart Analysis",
        "set_v3_smart",
    ),
    "set_scoring_criteria_menu": (
        "📐 *Critères de scoring*\n\nConfigurez les poids de chaque composant (spread, liquidité, conviction…).",
        "🧠 Ouvrir Smart Analysis",
        "set_v3_smart",
    ),
    "set_min_conviction_pct": (
        "💪 *Conviction minimum*\n\nPourcentage minimum du portfolio du trader que doit représenter le trade.",
        "🧠 Ouvrir Smart Analysis",
        "set_v3_smart",
    ),
    "set_cold_trader_threshold": (
        "🥶 *Seuil trader froid*\n\nWin rate en dessous duquel le trader est considéré « froid » et auto-pausé.",
        "🧠 Ouvrir Smart Analysis",
        "set_v3_smart",
    ),
    "set_hot_streak_boost": (
        "🔥 *Boost hot streak*\n\nMultiplicateur de mise appliqué aux traders « hot » (ex : ×1.5).",
        "🧠 Ouvrir Smart Analysis",
        "set_v3_smart",
    ),

    # ── Gestion des positions ──────────────────────────────
    "set_v3_positions": (
        "📉 *Gestion des positions*\n\nTrailing stop, time exit et scale-out.",
        "📉 Ouvrir Gestion Positions",
        "set_v3_positions",
    ),
    "set_stop_loss_menu": (
        "🛑 *Stop-Loss*\n\nDéfinissez votre seuil de perte maximal par position.",
        "📉 Ouvrir Gestion Positions",
        "set_v3_positions",
    ),
    "set_take_profit_menu": (
        "🎯 *Take-Profit*\n\nDéfinissez votre objectif de gain par position.",
        "📉 Ouvrir Gestion Positions",
        "set_v3_positions",
    ),
    "set_trailing_stop_pct": (
        "📉 *Trailing Stop %*\n\nRecul depuis le sommet qui déclenche la vente.",
        "📉 Ouvrir Gestion Positions",
        "set_v3_positions",
    ),
    "set_time_exit_hours": (
        "⏰ *Time Exit (heures)*\n\nDurée maximale d'une position avant sortie automatique.",
        "📉 Ouvrir Gestion Positions",
        "set_v3_positions",
    ),
    "set_scale_out_pct": (
        "📤 *Scale-Out %*\n\nPourcentage de la position à vendre au TP1 avant de laisser courir.",
        "📉 Ouvrir Gestion Positions",
        "set_v3_positions",
    ),

    # ── Risque Portfolio ────────────────────────────────────
    "set_v3_portfolio": (
        "📦 *Risque Portfolio*\n\nLimites de positions simultanées et d'exposition.",
        "📦 Ouvrir Risque Portfolio",
        "set_v3_portfolio",
    ),
    "set_max_positions": (
        "📦 *Positions maximum*\n\nNombre maximum de positions ouvertes en même temps.",
        "📦 Ouvrir Risque Portfolio",
        "set_v3_portfolio",
    ),
    "set_max_category_exposure_pct": (
        "📂 *Exposition max par catégorie*\n\nPourcentage maximum du portfolio dans une même catégorie (Crypto, Politique…).",
        "📦 Ouvrir Risque Portfolio",
        "set_v3_portfolio",
    ),
    "set_max_direction_bias_pct": (
        "⚖️ *Biais de direction max*\n\nMax % de positions dans le même sens (YES ou NO) pour éviter la sur-exposition.",
        "📦 Ouvrir Risque Portfolio",
        "set_v3_portfolio",
    ),

    # ── Notifications ───────────────────────────────────────
    "set_v3_notif": (
        "📬 *Notifications*\n\nChoisissez comment recevoir les alertes : DM, groupe ou les deux.",
        "📬 Ouvrir Notifications",
        "set_v3_notif",
    ),

    # ── Traders ─────────────────────────────────────────────
    "set_add_wallet": (
        "➕ *Ajouter un trader*\n\nSuivez un nouveau wallet Polymarket.\n\n"
        "_L'ajout se fait en DM pour plus de fluidité._",
        "➕ Ajouter un trader",
        "set_add_wallet",
    ),
    "set_followed": (
        "👤 *Gérer les traders suivis*\n\nRetirer ou réorganiser les wallets que vous copiez.",
        "👤 Gérer les traders",
        "set_followed",
    ),
    "v3_analytics": (
        "📊 *Analytics détaillés*\n\nPerfomance par trader, par catégorie, win rates historiques.",
        "📊 Voir les analytics",
        "menu_analytics",
    ),

    # ── Admin ───────────────────────────────────────────────
    "menu_settings": (
        "⚙️ *Tous les paramètres*\n\nAccédez à la vue complète de vos paramètres.",
        "⚙️ Ouvrir les paramètres",
        "menu_settings",
    ),
    "set_paper_trading": (
        "📝 *Basculer Paper / Live*\n\n"
        "⚠️ En mode *Live*, vos vrais USDC sont utilisés — vérifiez votre wallet avant d'activer.\n\n"
        "Confirmez le changement de mode ici :",
        "📝 Changer de mode",
        "set_paper_trading",
    ),
}

# Ensemble de tous les callbacks gérés par cet intercepteur
_ALL_HANDLED: frozenset[str] = frozenset(INLINE_TOGGLES) | frozenset(REFRESH_ACTIONS) | frozenset(DM_REDIRECTS)


# ─────────────────────────────────────────────────────────────
# Handler principal
# ─────────────────────────────────────────────────────────────

async def group_action_interceptor(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Intercepte les callbacks setting/menu dans les groupes.

    - Si groupe → traite (toggle/refresh/DM) + lève ApplicationHandlerStop
    - Si DM     → laisse passer (ConversationHandler prend la main)
    """
    query = update.callback_query
    chat = update.effective_chat

    # Ne s'active QUE dans les groupes
    if not chat or chat.type == "private":
        return

    data = (query.data or "").strip()
    if data not in _ALL_HANDLED:
        return

    await query.answer()  # dismiss le spinner immédiatement

    tg_user = update.effective_user

    try:
        # ── 1. Toggle inline ────────────────────────────────────
        if data in INLINE_TOGGLES:
            field = INLINE_TOGGLES[data]
            new_value = await _flip_setting(tg_user.id, field)
            on_msg, off_msg = _TOGGLE_LABELS.get(field, ("✅ Activé", "❌ Désactivé"))
            await query.answer(on_msg if new_value else off_msg, show_alert=False)
            # Rafraîchit le menu du topic courant
            await _refresh_topic_menu(update, context)

        # ── 2. Refresh ──────────────────────────────────────────
        elif data in REFRESH_ACTIONS:
            await _refresh_topic_menu(update, context)

        # ── 3. Redirection DM ───────────────────────────────────
        elif data in DM_REDIRECTS:
            intro_text, btn_label, btn_cb = DM_REDIRECTS[data]
            await _send_dm_panel(context, tg_user.id, intro_text, btn_label, btn_cb)
            await query.answer("📬 Envoyé en DM", show_alert=False)

    except ApplicationHandlerStop:
        raise
    except Exception as e:
        logger.warning("group_action error (data=%s): %s", data, e)

    # Empêche le ConversationHandler (group=0) de traiter le même callback
    raise ApplicationHandlerStop


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

async def _flip_setting(telegram_id: int, field: str) -> bool:
    """Inverse un champ booléen dans UserSettings. Retourne la nouvelle valeur."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, telegram_id)
        if not user:
            raise ValueError(f"User not found for telegram_id={telegram_id}")
        us = await get_or_create_settings(session, user)
        current = bool(getattr(us, field, False))
        setattr(us, field, not current)
        await session.commit()
    return not current


async def _refresh_topic_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Ré-affiche le menu du topic courant (envoie un nouveau message)."""
    from bot.handlers.topic_menus import show_topic_menu
    try:
        shown = await show_topic_menu(update, context)
        if not shown:
            # Topic non reconnu — envoie le menu principal
            from bot.handlers.menu import _send_main_menu
            await _send_main_menu(update.effective_message, update.effective_user)
    except Exception as e:
        logger.debug("_refresh_topic_menu failed: %s", e)


async def _send_dm_panel(
    context,
    telegram_id: int,
    intro_text: str,
    btn_label: str,
    btn_callback: str,
) -> None:
    """Envoie un panneau de configuration en DM avec un bouton d'action direct."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(btn_label, callback_data=btn_callback)
    ]])
    try:
        await context.bot.send_message(
            chat_id=telegram_id,
            text=(
                f"{intro_text}\n\n"
                "_Cliquez sur le bouton ci-dessous pour ouvrir le panneau de configuration._"
            ),
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    except Exception as e:
        # L'utilisateur n'a jamais démarré de conversation DM avec le bot
        raise RuntimeError(
            "Impossible d'envoyer un DM — démarrez d'abord une conversation "
            "privée avec le bot en cliquant sur son nom."
        ) from e


# ─────────────────────────────────────────────────────────────
# Enregistrement
# ─────────────────────────────────────────────────────────────

def get_group_action_handlers() -> list:
    """Handlers à enregistrer à group=-1 (priorité maximale)."""
    # Pattern qui correspond à tous les callbacks gérés
    pattern = "^(" + "|".join(re.escape(k) for k in sorted(_ALL_HANDLED)) + ")$"
    return [CallbackQueryHandler(group_action_interceptor, pattern=pattern)]
