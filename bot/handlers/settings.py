"""Settings handler — /settings command with inline keyboard menus."""

import logging
from typing import Optional

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
from bot.models.settings import SizingMode
from bot.services.user_service import get_user_by_telegram_id, get_or_create_settings, update_setting

logger = logging.getLogger(__name__)

# Conversation states
MAIN_MENU, EDIT_VALUE, ADD_WALLET = range(3)

# Setting display config
SETTING_LABELS = {
    "allocated_capital": ("💰 Capital alloué", "USDC"),
    "sizing_mode": ("📊 Mode de sizing", ""),
    "fixed_amount": ("💵 Mise fixe", "USDC"),
    "percent_per_trade": ("📈 % par trade", "%"),
    "multiplier": ("🎚️ Multiplicateur", "x"),
    "stop_loss_pct": ("🛑 Stop-loss global", "%"),
    "take_profit_pct": ("🎯 Take-profit global", "%"),
    "max_trade_usdc": ("✅ Mise max", "USDC"),
    "min_trade_usdc": ("❌ Mise min", "USDC"),
    "copy_delay_seconds": ("⏱️ Délai de copie", "s"),
    "manual_confirmation": ("🔔 Confirmation manuelle", ""),
    "confirmation_threshold_usdc": ("🔔 Seuil confirmation", "USDC"),
    "auto_bridge_sol": ("🌉 Auto-bridge SOL", ""),
}

# Descriptions détaillées avec exemples pour chaque option
SETTING_DESCRIPTIONS = {
    "allocated_capital": (
        "💰 **Capital alloué**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Le budget total que le bot peut utiliser pour copier des trades.\n\n"
        "**Comment ça marche :**\n"
        "Le bot utilise ce montant pour calculer la taille de vos positions "
        "(en mode % ou proportionnel).\n\n"
        "**Exemple :**\n"
        "Capital = 500 USDC, mode % à 10%\n"
        "→ Chaque trade fera 50 USDC\n\n"
        "📊 Valeurs possibles : **0.01 — 1 000 000 USDC**\n\n"
        "Envoyez le nouveau montant en USDC :"
    ),
    "fixed_amount": (
        "💵 **Mise fixe par trade**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Chaque trade copié utilisera exactement ce montant.\n\n"
        "**Comment ça marche :**\n"
        "Peu importe combien le master trader mise, votre trade "
        "sera toujours de ce montant fixe.\n\n"
        "**Exemple :**\n"
        "Mise fixe = 10 USDC\n"
        "→ Le master mise 500 USDC ? Vous misez 10 USDC\n"
        "→ Le master mise 5 USDC ? Vous misez 10 USDC\n\n"
        "📊 Valeurs possibles : **0.01 — 100 000 USDC**\n\n"
        "Envoyez le nouveau montant en USDC :"
    ),
    "percent_per_trade": (
        "📈 **Pourcentage par trade**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Chaque trade = un pourcentage de votre capital alloué.\n\n"
        "**Comment ça marche :**\n"
        "Le bot calcule automatiquement la mise à partir de votre "
        "capital alloué et du % choisi.\n\n"
        "**Exemple :**\n"
        "Capital = 1000 USDC, % par trade = 5%\n"
        "→ Chaque trade fera 50 USDC (5% de 1000)\n\n"
        "📊 Valeurs possibles : **0.1 — 100 %**\n\n"
        "Envoyez le nouveau pourcentage :"
    ),
    "multiplier": (
        "🎚️ **Multiplicateur**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Multiplie la taille de vos trades par rapport au master.\n\n"
        "**Comment ça marche :**\n"
        "En mode proportionnel ou Kelly, la taille calculée est "
        "multipliée par ce facteur.\n\n"
        "**Exemples :**\n"
        "• 0.5x = moitié de la taille du master\n"
        "• 1.0x = même taille que le master\n"
        "• 2.0x = le double du master\n\n"
        "📊 Valeurs possibles : **0.1 — 5.0**\n\n"
        "Envoyez le nouveau multiplicateur :"
    ),
    "stop_loss_pct": (
        "🛑 **Seuil du Stop-Loss**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Si vos pertes totales dépassent ce pourcentage de votre "
        "capital, le bot arrête automatiquement de copier.\n\n"
        "**Comment ça marche :**\n"
        "Le bot calcule la perte globale sur TOUTES vos positions. "
        "Si elle dépasse le seuil → arrêt complet du copytrading.\n\n"
        "**Exemple :**\n"
        "Capital = 1000 USDC, Stop-loss = 20%\n"
        "→ Arrêt si vos pertes atteignent 200 USDC\n\n"
        "📊 Valeurs possibles : **1 — 100 %**\n\n"
        "Envoyez le nouveau seuil en % :"
    ),
    "take_profit_pct": (
        "🎯 **Seuil du Take-Profit**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Si vos gains totaux dépassent ce pourcentage de votre "
        "capital, le bot arrête automatiquement de copier.\n\n"
        "**Comment ça marche :**\n"
        "Le bot calcule le gain global sur TOUTES vos positions. "
        "Si il dépasse le seuil → arrêt du copytrading pour "
        "sécuriser les profits.\n\n"
        "**Exemple :**\n"
        "Capital = 1000 USDC, Take-profit = 50%\n"
        "→ Arrêt si vos gains atteignent 500 USDC\n\n"
        "📊 Valeurs possibles : **1 — 1000 %**\n\n"
        "Envoyez le nouveau seuil en % :"
    ),
    "max_trade_usdc": (
        "✅ **Mise maximale par trade**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Plafond de sécurité : aucun trade ne dépassera ce montant.\n\n"
        "**Comment ça marche :**\n"
        "Peu importe le mode de sizing, si le montant calculé "
        "dépasse cette limite, il sera réduit au plafond.\n\n"
        "**Exemple :**\n"
        "Mode % = 10%, Capital = 5000 USDC, Mise max = 100 USDC\n"
        "→ Calcul = 500 USDC, mais plafond = 100 USDC\n\n"
        "📊 Valeurs possibles : **0.1 — 100 000 USDC**\n\n"
        "Envoyez le nouveau plafond en USDC :"
    ),
    "min_trade_usdc": (
        "❌ **Mise minimale par trade**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Plancher de sécurité : les trades trop petits sont ignorés.\n\n"
        "**Comment ça marche :**\n"
        "Si le montant calculé est en-dessous de cette limite, "
        "le trade n'est pas copié (trop petit pour être rentable "
        "après les frais).\n\n"
        "**Exemple :**\n"
        "Mise min = 5 USDC\n"
        "→ Un trade de 2 USDC sera ignoré\n"
        "→ Un trade de 10 USDC sera copié\n\n"
        "📊 Valeurs possibles : **0.01 — 10 000 USDC**\n\n"
        "Envoyez le nouveau plancher en USDC :"
    ),
    "copy_delay_seconds": (
        "⏱️ **Délai de copie**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Temps d'attente avant de copier un trade du master.\n\n"
        "**Comment ça marche :**\n"
        "Quand le master ouvre une position, le bot attend ce "
        "délai avant de copier. Utile pour éviter de copier des "
        "trades que le master annule rapidement.\n\n"
        "**Exemples :**\n"
        "• 0s = copie instantanée (recommandé)\n"
        "• 30s = attend 30 secondes avant de copier\n"
        "• 300s = attend 5 minutes\n\n"
        "📊 Valeurs possibles : **0 — 3600 secondes**\n\n"
        "Envoyez le nouveau délai en secondes :"
    ),
    "confirmation_threshold_usdc": (
        "🔔 **Seuil de confirmation**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Au-dessus de ce montant, le bot demande votre confirmation "
        "avant de copier un trade.\n\n"
        "**Comment ça marche :**\n"
        "Même si la confirmation manuelle est désactivée, les trades "
        "dépassant ce seuil nécessitent votre approbation.\n\n"
        "**Exemple :**\n"
        "Seuil = 50 USDC\n"
        "→ Trade de 30 USDC = copié automatiquement\n"
        "→ Trade de 100 USDC = le bot vous demande avant\n\n"
        "📊 Valeurs possibles : **0.01 — 100 000 USDC**\n\n"
        "Envoyez le nouveau seuil en USDC :"
    ),
    "max_expiry_days": (
        "📅 **Expiration maximale**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Ne copie que les marchés qui expirent dans les N prochains jours.\n\n"
        "**Comment ça marche :**\n"
        "Filtre les marchés trop lointains pour concentrer le capital "
        "sur des résolutions proches.\n\n"
        "**Exemple :**\n"
        "Expiry max = 30 jours\n"
        "→ Marché qui expire dans 7 jours = copié\n"
        "→ Marché qui expire dans 90 jours = ignoré\n\n"
        "📊 Valeurs possibles : **1 — 365 jours**\n\n"
        "Envoyez le nombre de jours maximum :"
    ),
}


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Afficher le menu principal des paramètres.

    Fonctionne à la fois via /settings et via le bouton \"⚙️ Paramètres\" du menu.
    """
    tg_user = update.effective_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user:
            # Commande directe
            if update.message:
                await update.message.reply_text(
                    "❌ Compte non trouvé. Utilisez /start pour vous inscrire."
                )
            else:
                # Depuis un callback, on édite simplement le message courant
                query = update.callback_query
                if query:
                    await query.answer()
                    await query.edit_message_text(
                        "❌ Compte non trouvé. Utilisez /start pour vous inscrire."
                    )
            return ConversationHandler.END

        us = await get_or_create_settings(session, user)
        text, keyboard = _build_main_menu(us, user.paper_trading)

    # Si on vient d'une commande /settings
    if update.message:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # Si on vient du bouton \"⚙️ Paramètres\" (callback menu_settings)
        query = update.callback_query
        if query:
            await query.answer()
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
            )

    return MAIN_MENU


def _build_main_menu(us, paper_trading: bool) -> tuple[str, list]:
    """Build the settings display text and keyboard."""

    wallets = us.followed_wallets or []
    wallet_display = f"**{len(wallets)}** trader(s)" if wallets else "**Aucun**"

    # Ligne descriptive selon le mode de sizing
    if us.sizing_mode == SizingMode.FIXED:
        sizing_line = (
            f"📊 Sizing : **Fixe** — **{us.fixed_amount:.2f} USDC** par trade\n"
        )
    elif us.sizing_mode == SizingMode.PERCENT:
        sizing_line = (
            f"📊 Sizing : **% du capital** — "
            f"**{us.percent_per_trade:.1f}%** de {us.allocated_capital:.0f} USDC\n"
        )
    elif us.sizing_mode == SizingMode.PROPORTIONAL:
        sizing_line = (
            f"📊 Sizing : **Proportionnel** — **{us.multiplier}x** du master\n"
        )
    else:
        sizing_line = (
            f"📊 Sizing : **Kelly** — multiplicateur **{us.multiplier}x**\n"
        )

    # Stop-loss display
    sl_enabled = getattr(us, "stop_loss_enabled", True)
    if sl_enabled:
        sl_line = f"🛑 Stop-loss : **Activé — {us.stop_loss_pct:.0f}%**\n"
    else:
        sl_line = "🛑 Stop-loss : **Désactivé**\n"

    # Take-profit display
    tp_enabled = getattr(us, "take_profit_enabled", False)
    tp_pct = getattr(us, "take_profit_pct", 50.0)
    if tp_enabled:
        tp_line = f"🎯 Take-profit : **Activé — {tp_pct:.0f}%**\n"
    else:
        tp_line = "🎯 Take-profit : **Désactivé**\n"

    text = (
        "⚙️ **PARAMÈTRES DE COPYTRADE**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Traders suivis : {wallet_display}\n"
        f"💰 Capital alloué : **{us.allocated_capital:.2f} USDC**\n"
        f"{sizing_line}"
        f"{sl_line}"
        f"{tp_line}"
        f"📏 Bornes : **{us.min_trade_usdc:.2f}** — **{us.max_trade_usdc:.2f} USDC**\n"
        f"⏱️ Délai de copie : **{us.copy_delay_seconds}s**\n"
        f"🔔 Confirmation : **{'Oui' if us.manual_confirmation else 'Non'}**\n"
        f"🌉 Auto-bridge SOL : **{'Activé' if us.auto_bridge_sol else 'Désactivé'}**\n"
        f"📝 Paper Trading : **{'Oui (simulation)' if paper_trading else 'Non (réel)'}**\n"
    )

    keyboard: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("👤 Traders suivis", callback_data="set_followed")],
        [
            InlineKeyboardButton("💰 Capital", callback_data="set_allocated_capital"),
            InlineKeyboardButton("📊 Sizing", callback_data="set_sizing_mode"),
        ],
    ]

    # Bouton dédié pour régler le montant clé selon le mode choisi
    if us.sizing_mode == SizingMode.FIXED:
        keyboard.append(
            [InlineKeyboardButton("💵 Montant fixe par trade", callback_data="set_fixed_amount")]
        )
    elif us.sizing_mode == SizingMode.PERCENT:
        keyboard.append(
            [InlineKeyboardButton("📈 % du capital par trade", callback_data="set_percent_per_trade")]
        )

    # SL / TP
    sl_btn_label = f"🛑 Stop-loss {'ON' if sl_enabled else 'OFF'}"
    tp_btn_label = f"🎯 Take-profit {'ON' if tp_enabled else 'OFF'}"
    keyboard.append([
        InlineKeyboardButton(sl_btn_label, callback_data="set_stop_loss_menu"),
        InlineKeyboardButton(tp_btn_label, callback_data="set_take_profit_menu"),
    ])

    keyboard.extend([
        [InlineKeyboardButton("📏 Bornes min/max", callback_data="set_advanced_limits")],
        [
            InlineKeyboardButton("⏱️ Délai copie", callback_data="set_copy_delay_seconds"),
            InlineKeyboardButton("🔔 Confirmation", callback_data="set_manual_confirmation"),
        ],
        [
            InlineKeyboardButton("🌉 Bridge SOL", callback_data="set_auto_bridge_sol"),
            InlineKeyboardButton(
                f"📝 Paper {'ON' if paper_trading else 'OFF'}",
                callback_data="set_paper_trading",
            ),
        ],
        [InlineKeyboardButton("⚙️ Avancé", callback_data="set_advanced")],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="set_close")],
    ])
    return text, keyboard


async def setting_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle a setting button press."""
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g., "set_allocated_capital"
    field = data.replace("set_", "")

    if field == "close":
        from bot.handlers.menu import _send_main_menu
        await _send_main_menu(query.message, query.from_user)
        return ConversationHandler.END

    if field == "advanced":
        keyboard = [
            [
                InlineKeyboardButton("📋 Catégories", callback_data="set_categories"),
                InlineKeyboardButton("🚫 Blacklist", callback_data="set_blacklisted_markets"),
            ],
            [
                InlineKeyboardButton("📅 Expiry max", callback_data="set_max_expiry_days"),
                InlineKeyboardButton("🔔 Seuil confirm.", callback_data="set_confirmation_threshold_usdc"),
            ],
            [
                InlineKeyboardButton(
                    "🔍 Gamma ON/OFF", callback_data="set_use_gamma_monitor"
                ),
                InlineKeyboardButton(
                    "🔍 WebSocket ON/OFF", callback_data="set_use_ws_monitor"
                ),
            ],
            [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
        ]
        await query.edit_message_text(
            "⚙️ **Paramètres avancés**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "**📋 Catégories** — Filtrer par catégorie de marché\n"
            "**🚫 Blacklist** — Exclure des marchés spécifiques\n"
            "**📅 Expiry max** — Ne copier que les marchés proches\n"
            "**🔔 Seuil confirm.** — Montant au-dessus duquel le bot demande confirmation\n"
            "**🔍 Gamma/WebSocket** — Choisir comment suivre les masters",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    if field == "advanced_limits":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)

        keyboard = [
            [InlineKeyboardButton(
                f"✅ Mise max : {us.max_trade_usdc:.2f} USDC",
                callback_data="set_max_trade_usdc",
            )],
            [InlineKeyboardButton(
                f"❌ Mise min : {us.min_trade_usdc:.2f} USDC",
                callback_data="set_min_trade_usdc",
            )],
            [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
        ]
        await query.edit_message_text(
            "📏 **Bornes de mise par trade**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Ces limites s'appliquent quel que soit le mode de sizing.\n"
            "Elles servent de garde-fou pour protéger votre capital.\n\n"
            f"• **Mise max** : plafond à **{us.max_trade_usdc:.2f} USDC**\n"
            "  → Aucun trade ne dépassera ce montant\n\n"
            f"• **Mise min** : plancher à **{us.min_trade_usdc:.2f} USDC**\n"
            "  → Les trades trop petits seront ignorés",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    # ── Stop-loss sub-menu ──
    if field == "stop_loss_menu":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)

        sl_enabled = getattr(us, "stop_loss_enabled", True)
        toggle_label = "❌ Désactiver" if sl_enabled else "✅ Activer"

        keyboard = [
            [InlineKeyboardButton(toggle_label, callback_data="set_stop_loss_toggle")],
            [InlineKeyboardButton(
                f"✏️ Modifier le seuil ({us.stop_loss_pct:.0f}%)",
                callback_data="set_stop_loss_pct",
            )],
            [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
        ]

        status = f"**Activé — {us.stop_loss_pct:.0f}%**" if sl_enabled else "**Désactivé**"
        await query.edit_message_text(
            "🛑 **Stop-Loss Global**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"État actuel : {status}\n\n"
            "**Comment ça marche :**\n"
            "Si vos pertes totales sur TOUTES vos positions dépassent "
            "le seuil choisi, le bot arrête automatiquement de copier "
            "de nouveaux trades. Vos positions existantes restent ouvertes.\n\n"
            "**Exemple :**\n"
            f"Capital = {us.allocated_capital:.0f} USDC, Stop-loss = {us.stop_loss_pct:.0f}%\n"
            f"→ Arrêt si vos pertes atteignent "
            f"{us.allocated_capital * us.stop_loss_pct / 100:.0f} USDC\n\n"
            "💡 Le stop-loss se réinitialise automatiquement après 1h.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    if field == "stop_loss_toggle":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)
            current = getattr(us, "stop_loss_enabled", True)
            await update_setting(session, us, "stop_loss_enabled", not current)
            await session.refresh(us)
            text, keyboard = _build_main_menu(us, user.paper_trading)
        status = "désactivé" if current else "activé"
        await query.edit_message_text(
            f"✅ Stop-loss **{status}**\n\n" + text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    # ── Take-profit sub-menu ──
    if field == "take_profit_menu":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)

        tp_enabled = getattr(us, "take_profit_enabled", False)
        tp_pct = getattr(us, "take_profit_pct", 50.0)
        toggle_label = "❌ Désactiver" if tp_enabled else "✅ Activer"

        keyboard = [
            [InlineKeyboardButton(toggle_label, callback_data="set_take_profit_toggle")],
            [InlineKeyboardButton(
                f"✏️ Modifier le seuil ({tp_pct:.0f}%)",
                callback_data="set_take_profit_pct",
            )],
            [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
        ]

        status = f"**Activé — {tp_pct:.0f}%**" if tp_enabled else "**Désactivé**"
        await query.edit_message_text(
            "🎯 **Take-Profit Global**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"État actuel : {status}\n\n"
            "**Comment ça marche :**\n"
            "Si vos gains totaux sur TOUTES vos positions dépassent "
            "le seuil choisi, le bot arrête automatiquement de copier "
            "pour sécuriser vos profits. Vos positions restent ouvertes.\n\n"
            "**Exemple :**\n"
            f"Capital = {us.allocated_capital:.0f} USDC, Take-profit = {tp_pct:.0f}%\n"
            f"→ Arrêt si vos gains atteignent "
            f"{us.allocated_capital * tp_pct / 100:.0f} USDC\n\n"
            "💡 Idéal pour sécuriser les gains et réinvestir manuellement.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    if field == "take_profit_toggle":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)
            current = getattr(us, "take_profit_enabled", False)
            await update_setting(session, us, "take_profit_enabled", not current)
            await session.refresh(us)
            text, keyboard = _build_main_menu(us, user.paper_trading)
        status = "désactivé" if current else "activé"
        await query.edit_message_text(
            f"✅ Take-profit **{status}**\n\n" + text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    if field == "back_main":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)
            text, keyboard = _build_main_menu(us, user.paper_trading)
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return MAIN_MENU

    # Toggle fields (boolean)
    if field in ("manual_confirmation", "auto_bridge_sol", "use_gamma_monitor", "use_ws_monitor"):
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)
            current = getattr(us, field)

            if field == "use_gamma_monitor" and current and not getattr(us, "use_ws_monitor", False):
                await update_setting(session, us, "use_gamma_monitor", False)
                await update_setting(session, us, "use_ws_monitor", True)
            elif field == "use_ws_monitor" and current and not getattr(us, "use_gamma_monitor", True):
                await update_setting(session, us, "use_ws_monitor", False)
                await update_setting(session, us, "use_gamma_monitor", True)
            else:
                new_val = not current
                await update_setting(session, us, field, new_val)

            await session.refresh(us)
            text, keyboard = _build_main_menu(us, user.paper_trading)
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return MAIN_MENU

    if field == "paper_trading":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            user.paper_trading = not user.paper_trading
            await session.commit()
            us = await get_or_create_settings(session, user)
            text, keyboard = _build_main_menu(us, user.paper_trading)

        mode = "simulation" if user.paper_trading else "réel"
        await query.edit_message_text(
            f"✅ Paper Trading **{mode}**\n\n" + text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    # Sizing mode — special multi-choice
    if field == "sizing_mode":
        keyboard = [
            [InlineKeyboardButton("💵 Fixe", callback_data="sizing_fixed")],
            [InlineKeyboardButton("📈 % Capital", callback_data="sizing_percent")],
            [InlineKeyboardButton("📊 Proportionnel", callback_data="sizing_proportional")],
            [InlineKeyboardButton("🧮 Kelly", callback_data="sizing_kelly")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
        ]
        await query.edit_message_text(
            "📊 **Mode de sizing**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Choisissez comment calculer la taille de vos positions :\n\n"
            "**💵 Fixe** — Même montant USDC à chaque trade\n"
            "  _Ex: 10 USDC par trade, peu importe le master_\n\n"
            "**📈 % Capital** — Pourcentage de votre capital alloué\n"
            "  _Ex: 5% de 1000 USDC = 50 USDC par trade_\n\n"
            "**📊 Proportionnel** — Proportionnel au master trader\n"
            "  _Ex: Le master mise 2% de son capital → vous aussi_\n\n"
            "**🧮 Kelly** — Critère de Kelly (avancé)\n"
            "  _Optimise la taille selon les probabilités du marché_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    # Numeric input — ask user for value with detailed description
    label = SETTING_LABELS.get(field, (field, ""))[0]
    context.user_data["editing_field"] = field

    # Use rich description if available, else fallback
    description = SETTING_DESCRIPTIONS.get(field)
    if description:
        edit_text = description
    else:
        edit_text = (
            f"✏️ **Modifier : {label}**\n\n"
            "Envoyez la nouvelle valeur :"
        )

    keyboard = [
        [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
    ]
    await query.edit_message_text(
        edit_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return EDIT_VALUE


async def followed_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the followed traders management menu."""
    query = update.callback_query
    await query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        us = await get_or_create_settings(session, user)
        wallets = us.followed_wallets or []

    if wallets:
        lines = []
        for i, w in enumerate(wallets, 1):
            lines.append(f"  {i}. `{w[:6]}...{w[-4:]}`")
        wallet_text = "\n".join(lines)
    else:
        wallet_text = "  _Aucun trader suivi_"

    keyboard = [
        [InlineKeyboardButton("➕ Ajouter un trader", callback_data="follow_add")],
    ]
    for i, w in enumerate(wallets):
        label = f"❌ Retirer {w[:6]}...{w[-4:]}"
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"follow_rm_{i}")]
        )
    keyboard.append(
        [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")]
    )

    await query.edit_message_text(
        "👤 **TRADERS SUIVIS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{wallet_text}\n\n"
        "Ajoutez l'adresse Polygon (0x...) d'un trader Polymarket "
        "dont vous voulez copier les positions.\n\n"
        "💡 Trouvez l'adresse sur le profil Polymarket du trader.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return MAIN_MENU


async def follow_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask user to send a wallet address to follow."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
    ]
    await query.edit_message_text(
        "➕ **Ajouter un trader à suivre**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Envoyez l'adresse Polygon (0x...) du trader à copier.\n\n"
        "**Où trouver l'adresse :**\n"
        "1. Allez sur le profil Polymarket du trader\n"
        "2. Copiez l'adresse de son wallet Polygon\n"
        "3. Collez-la ici\n\n"
        "**Format attendu :**\n"
        "`0x1234...abcd` (42 caractères, commence par 0x)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ADD_WALLET


async def follow_add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and validate a wallet address to follow."""
    address = update.message.text.strip()

    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text(
            "❌ Adresse invalide. Elle doit commencer par `0x` et faire 42 caractères.\n\n"
            "Réessayez ou cliquez sur « ⚙️ Paramètres » dans le menu principal pour annuler.",
            parse_mode="Markdown",
        )
        return ADD_WALLET

    try:
        int(address, 16)
    except ValueError:
        await update.message.reply_text(
            "❌ Adresse invalide — caractères non-hexadécimaux.\n\nRéessayez :",
        )
        return ADD_WALLET

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        us = await get_or_create_settings(session, user)
        wallets = list(us.followed_wallets or [])

        addr_lower = address.lower()
        if addr_lower in [w.lower() for w in wallets]:
            await update.message.reply_text(
                "⚠️ Vous suivez déjà ce trader !",
            )
            text, keyboard = _build_main_menu(us, user.paper_trading)
            await update.message.reply_text(
                text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return MAIN_MENU

        wallets.append(address)
        await update_setting(session, us, "followed_wallets", wallets)

        await update.message.reply_text(
            f"✅ Trader `{address[:6]}...{address[-4:]}` ajouté !\n\n"
            f"Vous suivez maintenant **{len(wallets)}** trader(s).",
            parse_mode="Markdown",
        )

        text, keyboard = _build_main_menu(us, user.paper_trading)
        await update.message.reply_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    return MAIN_MENU


async def follow_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Remove a followed wallet by index."""
    query = update.callback_query
    await query.answer()

    idx_str = query.data.replace("follow_rm_", "")
    try:
        idx = int(idx_str)
    except ValueError:
        return MAIN_MENU

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        us = await get_or_create_settings(session, user)
        wallets = list(us.followed_wallets or [])

        if 0 <= idx < len(wallets):
            removed = wallets.pop(idx)
            await update_setting(session, us, "followed_wallets", wallets)
            await query.edit_message_text(
                f"✅ Trader `{removed[:6]}...{removed[-4:]}` retiré.\n\n"
                f"Vous suivez maintenant **{len(wallets)}** trader(s).",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text("❌ Index invalide.")

        text, keyboard = _build_main_menu(us, user.paper_trading)
        await query.message.reply_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    return MAIN_MENU


async def sizing_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle sizing mode selection."""
    query = update.callback_query
    await query.answer()

    mode_map = {
        "sizing_fixed": SizingMode.FIXED,
        "sizing_percent": SizingMode.PERCENT,
        "sizing_proportional": SizingMode.PROPORTIONAL,
        "sizing_kelly": SizingMode.KELLY,
    }
    mode = mode_map.get(query.data)
    if not mode:
        return MAIN_MENU

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, query.from_user.id)
        us = await get_or_create_settings(session, user)
        await update_setting(session, us, "sizing_mode", mode)
        text, keyboard = _build_main_menu(us, user.paper_trading)

    await query.edit_message_text(
        f"✅ Mode de sizing mis à jour : **{mode.value}**\n\n" + text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return MAIN_MENU


async def receive_setting_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive a numeric value for a setting."""
    field = context.user_data.get("editing_field")
    if not field:
        await update.message.reply_text("❌ Erreur — pas de paramètre en cours d'édition.")
        return ConversationHandler.END

    raw = update.message.text.strip()

    # Parse value
    try:
        if field in ("copy_delay_seconds", "max_expiry_days"):
            value = int(raw)
        else:
            value = float(raw)
    except ValueError:
        await update.message.reply_text("❌ Valeur invalide. Envoyez un nombre.")
        return EDIT_VALUE

    # Validation
    validations = {
        "allocated_capital": (0.01, 1_000_000),
        "fixed_amount": (0.01, 100_000),
        "percent_per_trade": (0.1, 100),
        "multiplier": (0.1, 5.0),
        "stop_loss_pct": (1, 100),
        "take_profit_pct": (1, 1000),
        "max_trade_usdc": (0.1, 100_000),
        "min_trade_usdc": (0.01, 10_000),
        "copy_delay_seconds": (0, 3600),
        "confirmation_threshold_usdc": (0.01, 100_000),
        "max_expiry_days": (1, 365),
    }

    bounds = validations.get(field)
    if bounds:
        min_val, max_val = bounds
        if not (min_val <= value <= max_val):
            await update.message.reply_text(
                f"❌ Valeur hors limites. Min: {min_val}, Max: {max_val}"
            )
            return EDIT_VALUE

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        us = await get_or_create_settings(session, user)
        await update_setting(session, us, field, value)
        text, keyboard = _build_main_menu(us, user.paper_trading)

    label = SETTING_LABELS.get(field, (field, ""))[0]
    await update.message.reply_text(
        f"✅ **{label}** mis à jour : **{value}**\n\n" + text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    context.user_data.pop("editing_field", None)
    return MAIN_MENU


async def settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel settings edit."""
    await update.message.reply_text("✅ Paramètres fermés.")
    return ConversationHandler.END


def get_settings_handler() -> ConversationHandler:
    """Build the /settings conversation handler."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("settings", settings_command),
            CallbackQueryHandler(settings_command, pattern="^menu_settings$"),
        ],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(followed_menu, pattern="^set_followed$"),
                CallbackQueryHandler(follow_add_prompt, pattern="^follow_add$"),
                CallbackQueryHandler(follow_remove, pattern="^follow_rm_"),
                CallbackQueryHandler(sizing_selected, pattern="^sizing_"),
                CallbackQueryHandler(setting_selected, pattern="^set_"),
            ],
            EDIT_VALUE: [
                CallbackQueryHandler(setting_selected, pattern="^set_back_main$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_setting_value),
            ],
            ADD_WALLET: [
                CallbackQueryHandler(setting_selected, pattern="^set_back_main$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, follow_add_receive),
            ],
        },
        fallbacks=[CommandHandler("settings", settings_command)],
        per_user=True,
    )
