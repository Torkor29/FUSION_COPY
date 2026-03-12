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
    "max_trade_usdc": ("✅ Mise max", "USDC"),
    "min_trade_usdc": ("❌ Mise min", "USDC"),
    "copy_delay_seconds": ("⏱️ Délai de copie", "s"),
    "manual_confirmation": ("🔔 Confirmation manuelle", ""),
    "confirmation_threshold_usdc": ("🔔 Seuil confirmation", "USDC"),
    "auto_bridge_sol": ("🌉 Auto-bridge SOL", ""),
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
    """Build the settings display text and keyboard.

    On met en avant le paramètre clé selon le mode de sizing.
    """
    mode_display = {
        SizingMode.FIXED: "Fixe (montant par trade)",
        SizingMode.PERCENT: "% du capital",
        SizingMode.PROPORTIONAL: "Proportionnel au master",
        SizingMode.KELLY: "Kelly (avancé)",
    }

    wallets = us.followed_wallets or []
    wallet_display = f"**{len(wallets)}** trader(s)" if wallets else "**Aucun**"

    # Ligne descriptive principale selon le mode
    if us.sizing_mode == SizingMode.FIXED:
        sizing_line = (
            f"📊 Mode de sizing       : **Fixe** — "
            f"**{us.fixed_amount:.2f} USDC** par trade\n"
        )
    elif us.sizing_mode == SizingMode.PERCENT:
        sizing_line = (
            f"📊 Mode de sizing       : **% du capital** — "
            f"**{us.percent_per_trade:.2f}%** de {us.allocated_capital:.2f} USDC\n"
        )
    elif us.sizing_mode == SizingMode.PROPORTIONAL:
        sizing_line = (
            "📊 Mode de sizing       : **Proportionnel au master**\n"
            f"   → Multiplicateur      : **{us.multiplier}x**\n"
        )
    else:  # Kelly ou autre
        sizing_line = (
            "📊 Mode de sizing       : **Kelly (avancé)**\n"
            f"   → Multiplicateur      : **{us.multiplier}x**\n"
        )

    text = (
        "⚙️ **PARAMÈTRES DE COPYTRADE**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Traders suivis       : {wallet_display}\n"
        f"💰 Capital alloué       : **{us.allocated_capital:.2f} USDC**\n"
        f"{sizing_line}"
        f"🛑 Stop-loss global     : **{us.stop_loss_pct}%** "
        "(optionnel, sur la perte TOTALE)\n"
        f"✅ Mise max (sécurité)  : **{us.max_trade_usdc:.2f} USDC**\n"
        f"❌ Mise min (sécurité)  : **{us.min_trade_usdc:.2f} USDC**\n"
        f"⏱️ Délai de copie       : **{us.copy_delay_seconds}s**\n"
        f"🔔 Confirmation manuelle: **{'Oui' if us.manual_confirmation else 'Non'}**\n"
        f"🌉 Auto-bridge SOL     : **{'Activé' if us.auto_bridge_sol else 'Désactivé'}**\n"
        f"📝 Paper Trading        : **{'Oui' if paper_trading else 'Non'}**\n"
    )

    keyboard: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("👤 Gérer les traders suivis", callback_data="set_followed")],
        [
            InlineKeyboardButton("💰 Capital", callback_data="set_allocated_capital"),
            InlineKeyboardButton("📊 Mode de sizing", callback_data="set_sizing_mode"),
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

    # Reste des paramètres de risque / comportement
    keyboard.extend(
        [
            [
                InlineKeyboardButton("🛑 Stop-loss global", callback_data="set_stop_loss_pct"),
                InlineKeyboardButton("🔔 Confirm.", callback_data="set_manual_confirmation"),
            ],
            [
                InlineKeyboardButton(
                    "✅/❌ Bornes par trade", callback_data="set_advanced_limits"
                ),
            ],
            [
                InlineKeyboardButton("⏱️ Délai copie", callback_data="set_copy_delay_seconds"),
            ],
            [
                InlineKeyboardButton("🌉 Bridge SOL", callback_data="set_auto_bridge_sol"),
                InlineKeyboardButton("📝 Paper Mode", callback_data="set_paper_trading"),
            ],
            [InlineKeyboardButton("⚙️ Avancé", callback_data="set_advanced")],
            [InlineKeyboardButton("🏠 Menu principal", callback_data="set_close")],
        ]
    )
    return text, keyboard


async def setting_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle a setting button press."""
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g., "set_allocated_capital"
    field = data.replace("set_", "")

    if field == "close":
        # Retour direct au menu principal global pour simplifier la navigation
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
            [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
        ]
        await query.edit_message_text(
            "⚙️ **Paramètres avancés**\n\nSélectionnez un paramètre à modifier :",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    if field == "advanced_limits":
        # Sous-menu dédié aux bornes min/max par trade pour plus de clarté
        keyboard = [
            [
                InlineKeyboardButton("✅ Mise max (sécurité)", callback_data="set_max_trade_usdc"),
            ],
            [
                InlineKeyboardButton("❌ Mise min (sécurité)", callback_data="set_min_trade_usdc"),
            ],
            [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
        ]
        await query.edit_message_text(
            "📏 **Bornes de mise par trade**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Ces limites s'appliquent quel que soit le mode de sizing :\n\n"
            "• **Mise max** : plafonne la taille d'un trade (sécurité haute).\n"
            "• **Mise min** : évite les micro-trades trop petits.\n\n"
            "Elles viennent en plus du montant fixe / % que vous avez choisi.",
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
    if field in ("manual_confirmation", "auto_bridge_sol"):
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)
            new_val = not getattr(us, field)
            await update_setting(session, us, field, new_val)
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
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
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
            "📊 **Mode de sizing**\n\n"
            "Choisissez comment calculer la taille de vos positions :\n\n"
            "• **Fixe** — Même montant USDC à chaque trade\n"
            "• **% Capital** — Pourcentage de votre capital alloué\n"
            "• **Proportionnel** — Proportionnel au master trader\n"
            "• **Kelly** — Critère de Kelly (avancé)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    # Numeric input — ask user for value
    label = SETTING_LABELS.get(field, (field, ""))[0]
    context.user_data["editing_field"] = field

    await query.edit_message_text(
        f"✏️ **Modifier : {label}**\n\n"
        "Envoyez la nouvelle valeur :",
        parse_mode="Markdown",
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
        f"👤 **TRADERS SUIVIS**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{wallet_text}\n\n"
        f"Ajoutez l'adresse Polygon (0x...) d'un trader Polymarket "
        f"dont vous voulez copier les positions.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return MAIN_MENU


async def follow_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask user to send a wallet address to follow."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "➕ **Ajouter un trader à suivre**\n\n"
        "Envoyez l'adresse Polygon (0x...) du trader à copier :\n\n"
        "💡 Vous pouvez la trouver sur le profil Polymarket du trader.",
        parse_mode="Markdown",
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
            # Permet d'ouvrir les paramètres directement depuis le bouton du menu principal
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
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_setting_value),
            ],
            ADD_WALLET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, follow_add_receive),
            ],
        },
        fallbacks=[CommandHandler("settings", settings_command)],
        per_user=True,
    )
