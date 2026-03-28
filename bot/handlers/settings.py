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
from bot.models.settings import SizingMode, GasMode, GAS_MODE_LABELS, GAS_PRIORITY_FEES
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
    # ── V3 Smart Analysis ──
    "min_signal_score": ("🎯 Score minimum", "/100"),
    "cold_trader_threshold": ("🥶 Seuil trader froid", "%"),
    "hot_streak_boost": ("🔥 Boost hot streak", "x"),
    "trailing_stop_pct": ("📉 Trailing stop", "%"),
    "time_exit_hours": ("⏰ Sortie temps", "h"),
    "scale_out_pct": ("📊 Scale-out", "%"),
    "max_positions": ("📦 Max positions", ""),
    "max_category_exposure_pct": ("📂 Max par catégorie", "%"),
    "max_direction_bias_pct": ("⚖️ Max biais direction", "%"),
    "min_trader_winrate_for_type": ("📈 Win rate min trader", "%"),
    "min_trader_trades_for_type": ("🔢 Trades min pour filtre", ""),
    "min_conviction_pct": ("💪 Conviction min", "%"),
    "max_price_drift_pct": ("📏 Drift prix max", "%"),
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
    # ═══════════════════════════════════════════
    # V3 — SMART ANALYSIS DESCRIPTIONS
    # ═══════════════════════════════════════════
    "min_signal_score": (
        "🎯 **Score minimum pour copier**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Chaque signal reçoit un score de 0 à 100 basé sur 6 critères :\n"
        "• Spread (bid-ask serré ?)\n"
        "• Liquidité (volume du marché)\n"
        "• Conviction (taille vs portfolio du trader)\n"
        "• Forme du trader (win rate 7 jours)\n"
        "• Timing (distance à l'expiry)\n"
        "• Consensus (d'autres traders font pareil ?)\n\n"
        "Seuls les signaux au-dessus de ce seuil sont copiés.\n\n"
        "**Exemples :**\n"
        "• 30 = permissif — copie la plupart\n"
        "• 50 = modéré — filtre les mauvais\n"
        "• 70 = strict — ne prend que le top\n\n"
        "📊 Valeurs : **0 — 100**\n\n"
        "Envoyez le score minimum :"
    ),
    "cold_trader_threshold": (
        "🥶 **Seuil de win rate — trader froid**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Si un trader passe en-dessous de ce win rate sur 15+ trades, "
        "il est automatiquement mis en pause.\n\n"
        "**Comment ça marche :**\n"
        "Le bot recalcule les stats toutes les 15 min. Si le win rate "
        "7 jours tombe sous ce seuil → plus aucun trade copié de ce "
        "trader jusqu'à ce qu'il remonte.\n\n"
        "**Exemple :**\n"
        "Seuil = 40% → un trader à 35% de win rate est pausé\n\n"
        "📊 Valeurs : **10 — 60 %**\n\n"
        "Envoyez le seuil en % :"
    ),
    "hot_streak_boost": (
        "🔥 **Boost de sizing — trader en forme**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Multiplicateur appliqué aux trades quand un trader est "
        "en hot streak (win rate > 65% sur 10+ trades).\n\n"
        "**Comment ça marche :**\n"
        "Un trader performant mérite plus de capital. Ce multiplicateur "
        "augmente automatiquement la taille des trades copiés.\n\n"
        "**Exemples :**\n"
        "• 1.0x = pas de boost (même taille)\n"
        "• 1.5x = 50% de plus quand le trader est chaud\n"
        "• 2.0x = le double\n\n"
        "📊 Valeurs : **1.0 — 3.0**\n\n"
        "Envoyez le multiplicateur :"
    ),
    "trailing_stop_pct": (
        "📉 **Trailing stop — pourcentage**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Le trailing stop suit le prix à la hausse. Si le prix "
        "redescend de X% depuis son plus haut → vente automatique.\n\n"
        "**Comment ça marche :**\n"
        "Contrairement au stop-loss fixe, le trailing stop s'ajuste "
        "à la hausse. Il protège les gains acquis.\n\n"
        "**Exemple :**\n"
        "Trailing = 10%, entrée à $0.60\n"
        "→ Prix monte à $0.80 → trailing à $0.72\n"
        "→ Prix monte à $0.90 → trailing à $0.81\n"
        "→ Prix redescend à $0.81 → VENDU (gain sécurisé)\n\n"
        "📊 Valeurs : **2 — 50 %**\n\n"
        "Envoyez le pourcentage :"
    ),
    "time_exit_hours": (
        "⏰ **Sortie automatique — durée max**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Si une position est ouverte depuis plus de N heures "
        "et n'a pas bougé (< 2% de variation) → vente automatique.\n\n"
        "**Pourquoi ?**\n"
        "Le capital mort bloqué dans une position plate ne rapporte rien. "
        "Mieux vaut le libérer pour de meilleures opportunités.\n\n"
        "**Exemples :**\n"
        "• 12h = agressif — sort vite les positions mortes\n"
        "• 24h = modéré (recommandé)\n"
        "• 48h = patient\n\n"
        "📊 Valeurs : **1 — 168 heures** (1h à 7 jours)\n\n"
        "Envoyez la durée en heures :"
    ),
    "scale_out_pct": (
        "📊 **Scale-out — prise de profit partielle**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Quand le take-profit est atteint, ne vend que X% de la position. "
        "Le reste continue de courir.\n\n"
        "**Comment ça marche :**\n"
        "Au lieu de tout vendre d'un coup au TP, le bot sécurise une "
        "partie des gains et laisse le reste pour un potentiel gain plus grand.\n\n"
        "**Exemple :**\n"
        "Scale-out = 50%, position de 100 shares au TP\n"
        "→ Vend 50 shares (profit sécurisé)\n"
        "→ Garde 50 shares (laisse courir)\n\n"
        "📊 Valeurs : **10 — 90 %**\n\n"
        "Envoyez le pourcentage à vendre au TP :"
    ),
    "max_positions": (
        "📦 **Nombre max de positions ouvertes**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Limite le nombre total de positions simultanées.\n\n"
        "**Pourquoi ?**\n"
        "Trop de positions = capital dilué + risque de corrélation. "
        "Mieux vaut concentrer sur les meilleures opportunités.\n\n"
        "**Exemples :**\n"
        "• 5 = très concentré — seulement le meilleur\n"
        "• 15 = diversifié (recommandé)\n"
        "• 30 = très large\n\n"
        "📊 Valeurs : **1 — 50**\n\n"
        "Envoyez le nombre maximum :"
    ),
    "max_category_exposure_pct": (
        "📂 **Exposition max par catégorie**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Pourcentage maximum du portfolio dans une seule catégorie "
        "(Crypto, Politique, Sport, etc.).\n\n"
        "**Pourquoi ?**\n"
        "Évite d'avoir 80% de votre capital sur du BTC. "
        "La diversification protège contre les mauvaises journées.\n\n"
        "**Exemple :**\n"
        "Max catégorie = 30%\n"
        "→ Avec 10 positions, max 3 peuvent être en Crypto\n"
        "→ Le 4ème trade Crypto sera bloqué\n\n"
        "📊 Valeurs : **10 — 100 %**\n\n"
        "Envoyez le pourcentage maximum :"
    ),
    "max_direction_bias_pct": (
        "⚖️ **Biais directionnel max**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Pourcentage maximum de positions dans la même direction "
        "(toutes YES ou toutes NO).\n\n"
        "**Pourquoi ?**\n"
        "Si 90% de vos positions sont YES et que le marché se "
        "retourne, vous perdez partout. Le biais directionnel "
        "force la diversification.\n\n"
        "**Exemple :**\n"
        "Max biais = 70%\n"
        "→ Sur 10 positions, max 7 peuvent être YES\n"
        "→ Le 8ème YES sera bloqué (forcé de prendre du NO)\n\n"
        "📊 Valeurs : **50 — 100 %**\n\n"
        "Envoyez le pourcentage maximum :"
    ),
    "min_trader_winrate_for_type": (
        "📈 **Win rate minimum du trader par type de marché**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Ne copie un trade que si le trader a un win rate prouvé "
        "sur CE TYPE de marché (ex: BTC 5min, Politique US...).\n\n"
        "**Comment ça marche :**\n"
        "Le bot track le win rate de chaque trader par type de marché. "
        "Si le trader est bon en crypto mais nul en politique, "
        "on ne copie que ses trades crypto.\n\n"
        "**Exemple :**\n"
        "Min WR = 55%\n"
        "→ Trader à 70% sur BTC 5min → copié\n"
        "→ Trader à 45% sur Sports NFL → ignoré\n\n"
        "📊 Valeurs : **40 — 80 %**\n\n"
        "Envoyez le win rate minimum :"
    ),
    "min_trader_trades_for_type": (
        "🔢 **Nombre min de trades pour activer le filtre**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Le filtre win rate ne s'active qu'après N trades sur ce type "
        "de marché. Avant, les trades sont autorisés par défaut.\n\n"
        "**Pourquoi ?**\n"
        "Avec seulement 3 trades, un win rate de 33% ne veut rien dire. "
        "Il faut assez de données pour juger.\n\n"
        "**Exemples :**\n"
        "• 5 = rapide mais moins fiable\n"
        "• 10 = bon compromis (recommandé)\n"
        "• 20 = très prudent, beaucoup de data\n\n"
        "📊 Valeurs : **3 — 50**\n\n"
        "Envoyez le nombre minimum :"
    ),
    "min_conviction_pct": (
        "💪 **Conviction minimale du trader**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Pourcentage minimum du portfolio du trader que représente "
        "son trade. Filtre les petits trades sans conviction.\n\n"
        "**Comment ça marche :**\n"
        "Si un trader met seulement 0.5% de son capital, c'est peut-être "
        "un test. On ne copie que s'il y met un minimum.\n\n"
        "**Exemples :**\n"
        "• 1% = copie quasi tout\n"
        "• 2% = filtre les micro-trades (recommandé)\n"
        "• 5% = ne prend que les grosses convictions\n\n"
        "📊 Valeurs : **0.5 — 20 %**\n\n"
        "Envoyez le pourcentage minimum :"
    ),
    "max_price_drift_pct": (
        "📏 **Drift de prix maximum**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Si le prix a bougé de plus de X% depuis l'entrée du trader, "
        "le trade n'est pas copié (trop tard).\n\n"
        "**Comment ça marche :**\n"
        "Entre le moment où le master trade et celui où le bot copie, "
        "le prix peut avoir changé. Si le drift est trop grand, "
        "le trade n'est plus intéressant.\n\n"
        "**Exemple :**\n"
        "Drift max = 5%\n"
        "→ Master achète à $0.60, prix actuel $0.62 (3%) = copié\n"
        "→ Master achète à $0.60, prix actuel $0.68 (13%) = ignoré\n\n"
        "📊 Valeurs : **1 — 20 %**\n\n"
        "Envoyez le pourcentage maximum :"
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

    # Gas mode display
    gas_mode = getattr(us, "gas_mode", GasMode.FAST)
    gas_label = GAS_MODE_LABELS.get(gas_mode, "🚀 Fast")

    # ── V3 Smart Analysis status ──
    scoring_on = getattr(us, "signal_scoring_enabled", True)
    smart_on = getattr(us, "smart_filter_enabled", True)
    trailing_on = getattr(us, "trailing_stop_enabled", False)
    min_score = getattr(us, "min_signal_score", 40)
    max_pos = getattr(us, "max_positions", 15)
    notif_mode = getattr(us, "notification_mode", "dm")
    notif_label = {"dm": "DM", "group": "Groupe", "both": "DM + Groupe"}.get(
        notif_mode, "DM"
    )

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
        f"⛽ Gas : **{gas_label}**\n"
        f"📝 Paper Trading : **{'Oui (simulation)' if paper_trading else 'Non (réel)'}**\n"
        f"\n"
        f"🧠 **V3** : scoring {'✅' if scoring_on else '❌'}"
        f" · filtre {'✅' if smart_on else '❌'}"
        f" · trailing {'✅' if trailing_on else '❌'}"
        f" · notifs {notif_label}\n"
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
            InlineKeyboardButton("⛽ Vitesse Gas", callback_data="set_gas_mode"),
            InlineKeyboardButton("🌉 Bridge SOL", callback_data="set_auto_bridge_sol"),
        ],
        [
            InlineKeyboardButton(
                f"📝 Paper {'ON' if paper_trading else 'OFF'}",
                callback_data="set_paper_trading",
            ),
        ],
        # ── V3 Sub-menus (with descriptions) ──
        [InlineKeyboardButton(
            "🧠 Smart Analysis — scoring, filtres, tracking",
            callback_data="set_v3_smart",
        )],
        [InlineKeyboardButton(
            "📉 Positions — trailing stop, sortie auto",
            callback_data="set_v3_positions",
        )],
        [
            InlineKeyboardButton("📦 Risque", callback_data="set_v3_portfolio"),
            InlineKeyboardButton("📬 Notifs", callback_data="set_v3_notif"),
        ],
        [InlineKeyboardButton("⚙️ Avancé", callback_data="set_advanced")],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="set_close")],
    ])
    return text, keyboard


async def setting_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle a setting button press."""
    query = update.callback_query
    await query.answer()

    # Support re-rendering a sub-menu after toggle/preset
    reload_field = context.user_data.pop("_reload_field", None)
    if reload_field:
        data = f"set_{reload_field}"
    else:
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

    # ═══════════════════════════════════════════
    # V3 SUB-MENUS
    # ═══════════════════════════════════════════

    if field == "v3_smart":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)

        scoring_on = getattr(us, "signal_scoring_enabled", True)
        smart_on = getattr(us, "smart_filter_enabled", True)
        min_score = getattr(us, "min_signal_score", 40)
        auto_pause = getattr(us, "auto_pause_cold_traders", True)
        cold_thr = getattr(us, "cold_trader_threshold", 40)
        hot_boost = getattr(us, "hot_streak_boost", 1.5)
        skip_flip = getattr(us, "skip_coin_flip", True)
        min_wr = getattr(us, "min_trader_winrate_for_type", 55)
        min_trades = getattr(us, "min_trader_trades_for_type", 10)
        min_conv = getattr(us, "min_conviction_pct", 2)
        max_drift = getattr(us, "max_price_drift_pct", 5)

        keyboard = [
            [
                InlineKeyboardButton(
                    f"🧠 Scoring {'ON' if scoring_on else 'OFF'}",
                    callback_data="set_signal_scoring_enabled",
                ),
                InlineKeyboardButton(
                    f"🎯 Score min: {min_score:.0f}",
                    callback_data="set_min_signal_score",
                ),
            ],
            [InlineKeyboardButton(
                "📐 Choisir les critères du scoring",
                callback_data="set_v3_criteria",
            )],
            [
                InlineKeyboardButton(
                    f"🎯 Filtre smart {'ON' if smart_on else 'OFF'}",
                    callback_data="set_smart_filter_enabled",
                ),
                InlineKeyboardButton(
                    f"🪙 Skip coin-flip {'ON' if skip_flip else 'OFF'}",
                    callback_data="set_skip_coin_flip",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"🥶 Auto-pause {'ON' if auto_pause else 'OFF'}",
                    callback_data="set_auto_pause_cold_traders",
                ),
                InlineKeyboardButton(
                    f"🥶 Seuil: {cold_thr:.0f}%",
                    callback_data="set_cold_trader_threshold",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"🔥 Boost hot: {hot_boost:.1f}x",
                    callback_data="set_hot_streak_boost",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"📈 WR min: {min_wr:.0f}%",
                    callback_data="set_min_trader_winrate_for_type",
                ),
                InlineKeyboardButton(
                    f"🔢 Trades min: {min_trades}",
                    callback_data="set_min_trader_trades_for_type",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"💪 Conviction: {min_conv:.0f}%",
                    callback_data="set_min_conviction_pct",
                ),
                InlineKeyboardButton(
                    f"📏 Drift max: {max_drift:.0f}%",
                    callback_data="set_max_price_drift_pct",
                ),
            ],
            [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
        ]
        await query.edit_message_text(
            "🧠 **SMART ANALYSIS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Le cerveau du bot. Chaque signal est noté 0-100 "
            "sur 6 critères avant d'être copié._\n\n"
            "**📊 Comment ça marche ?**\n"
            "Le score est la somme pondérée de :\n"
            "• Spread bid-ask (15%) — serré = bon\n"
            "• Volume 24h du marché (15%)\n"
            "• Conviction du trader (20%) — gros trade = confiant\n"
            "• Win rate 7j du trader (20%)\n"
            "• Timing d'expiry (15%) — zone idéale 2-48h\n"
            "• Consensus entre traders (15%)\n\n"
            f"*Scoring:* **{'Actif ✅' if scoring_on else 'Inactif ❌'}** | "
            f"Seuil: **{min_score:.0f}/100**\n"
            f"_Seuls les signaux ≥ {min_score:.0f} sont copiés_\n\n"
            f"*Filtre smart:* **{'Actif ✅' if smart_on else 'Inactif ❌'}** | "
            f"Coin-flip: **{'bloqué' if skip_flip else 'autorisé'}**\n"
            f"_Bloque les marchés à ~$0.50, les traders sans edge, "
            f"et les drifts > {max_drift:.0f}%_\n\n"
            f"*Tracking traders:*\n"
            f"  Auto-pause si WR < {cold_thr:.0f}% sur 15+ trades\n"
            f"  Boost sizing **{hot_boost:.1f}x** si WR > 65%\n"
            f"  WR min par type: **{min_wr:.0f}%** sur {min_trades}+ trades\n"
            f"  Conviction min: **{min_conv:.0f}%** du portfolio trader",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    if field == "v3_criteria":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)

        from bot.services.signal_scorer import DEFAULT_CRITERIA
        criteria = getattr(us, "scoring_criteria", None) or dict(DEFAULT_CRITERIA)

        CRITERIA_LABELS = {
            "spread": ("📏 Spread", "Écart bid/ask — serré = facile à exécuter"),
            "liquidity": ("💧 Liquidité", "Volume 24h — élevé = marché actif"),
            "conviction": ("💪 Conviction", "Taille du trade vs portfolio du trader"),
            "trader_form": ("📈 Forme trader", "Win rate des 7 derniers jours"),
            "timing": ("⏱ Timing", "Distance à l'expiry — idéal 2-48h"),
            "consensus": ("👥 Consensus", "Autres traders sur le même marché"),
        }

        keyboard = []
        status_lines = []
        for key in ["spread", "liquidity", "conviction", "trader_form", "timing", "consensus"]:
            cfg = criteria.get(key, {"on": True, "w": DEFAULT_CRITERIA[key]["w"]})
            is_on = cfg.get("on", True)
            weight = cfg.get("w", 15)
            label, desc = CRITERIA_LABELS[key]
            emoji = "✅" if is_on else "❌"
            keyboard.append([InlineKeyboardButton(
                f"{emoji} {label} — poids: {weight}%",
                callback_data=f"set_crit_toggle_{key}",
            )])
            status_lines.append(
                f"{emoji} *{label}* (poids {weight}%)\n   _{desc}_"
            )

        # Presets row
        keyboard.append([
            InlineKeyboardButton("⚖️ Tout ON", callback_data="set_crit_preset_all"),
            InlineKeyboardButton("🎯 Trader seul", callback_data="set_crit_preset_trader"),
        ])
        keyboard.append([
            InlineKeyboardButton("📊 Marché seul", callback_data="set_crit_preset_market"),
            InlineKeyboardButton("🔥 Minimal", callback_data="set_crit_preset_minimal"),
        ])
        keyboard.append([InlineKeyboardButton("⬅️ Retour", callback_data="set_v3_smart")])

        await query.edit_message_text(
            "📐 **CRITÈRES DU SCORING**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Cochez/décochez les critères utilisés pour noter "
            "chaque signal. Les poids se redistribuent automatiquement._\n\n"
            + "\n".join(status_lines) + "\n\n"
            "**Presets :**\n"
            "⚖️ *Tout ON* — les 6 critères, poids par défaut\n"
            "🎯 *Trader seul* — forme + conviction seulement\n"
            "📊 *Marché seul* — spread + liquidité + timing\n"
            "🔥 *Minimal* — forme trader + spread (le plus rapide)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    # Handle criteria toggle
    if field.startswith("crit_toggle_"):
        crit_key = field.replace("crit_toggle_", "")
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)

            from bot.services.signal_scorer import DEFAULT_CRITERIA
            criteria = getattr(us, "scoring_criteria", None) or dict(DEFAULT_CRITERIA)
            if crit_key in criteria:
                criteria[crit_key]["on"] = not criteria[crit_key].get("on", True)
            else:
                criteria[crit_key] = {"on": False, "w": DEFAULT_CRITERIA.get(crit_key, {}).get("w", 15)}

            await update_setting(session, us, "scoring_criteria", criteria)

        # Re-render criteria menu
        context.user_data["_reload_field"] = "v3_criteria"
        return await setting_selected(update, context)

    # Handle criteria presets
    if field.startswith("crit_preset_"):
        preset = field.replace("crit_preset_", "")
        from bot.services.signal_scorer import DEFAULT_CRITERIA

        if preset == "all":
            criteria = dict(DEFAULT_CRITERIA)
        elif preset == "trader":
            criteria = {k: {"on": k in ("trader_form", "conviction"), "w": v["w"]}
                        for k, v in DEFAULT_CRITERIA.items()}
        elif preset == "market":
            criteria = {k: {"on": k in ("spread", "liquidity", "timing"), "w": v["w"]}
                        for k, v in DEFAULT_CRITERIA.items()}
        elif preset == "minimal":
            criteria = {k: {"on": k in ("trader_form", "spread"), "w": v["w"]}
                        for k, v in DEFAULT_CRITERIA.items()}
        else:
            criteria = dict(DEFAULT_CRITERIA)

        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)
            await update_setting(session, us, "scoring_criteria", criteria)

        # Re-render criteria menu
        context.user_data["_reload_field"] = "v3_criteria"
        return await setting_selected(update, context)

    if field == "v3_positions":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)

        sl_on = getattr(us, "stop_loss_enabled", True)
        tp_on = getattr(us, "take_profit_enabled", False)
        trail_on = getattr(us, "trailing_stop_enabled", False)
        trail_pct = getattr(us, "trailing_stop_pct", 10)
        time_on = getattr(us, "time_exit_enabled", False)
        time_h = getattr(us, "time_exit_hours", 24)
        scale_on = getattr(us, "scale_out_enabled", False)
        scale_pct = getattr(us, "scale_out_pct", 50)

        keyboard = [
            [
                InlineKeyboardButton(
                    f"📉 Trailing {'ON' if trail_on else 'OFF'}",
                    callback_data="set_trailing_stop_enabled",
                ),
                InlineKeyboardButton(
                    f"📉 Trail: {trail_pct:.0f}%",
                    callback_data="set_trailing_stop_pct",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"⏰ Time exit {'ON' if time_on else 'OFF'}",
                    callback_data="set_time_exit_enabled",
                ),
                InlineKeyboardButton(
                    f"⏰ Après: {time_h}h",
                    callback_data="set_time_exit_hours",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"📊 Scale-out {'ON' if scale_on else 'OFF'}",
                    callback_data="set_scale_out_enabled",
                ),
                InlineKeyboardButton(
                    f"📊 Vendre: {scale_pct:.0f}%",
                    callback_data="set_scale_out_pct",
                ),
            ],
            [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
        ]
        await query.edit_message_text(
            "📉 **GESTION DES POSITIONS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Gestion active de vos positions ouvertes. "
            "Le bot vérifie les prix toutes les 15 secondes._\n\n"
            "**Trailing Stop** — Suit le prix à la hausse\n"
            f"  État: **{'Actif' if trail_on else 'Inactif'}** | "
            f"Seuil: **{trail_pct:.0f}%** sous le plus haut\n"
            "  _Ex: entrée $0.60, pic $0.90 → vente si redescend à "
            f"${0.90 * (1 - trail_pct/100):.2f}_\n\n"
            "**Time Exit** — Sort les positions mortes\n"
            f"  État: **{'Actif' if time_on else 'Inactif'}** | "
            f"Après: **{time_h}h** si < 2% de mouvement\n"
            "  _Libère le capital bloqué dans des positions plates_\n\n"
            "**Scale-Out** — Prise de profit partielle\n"
            f"  État: **{'Actif' if scale_on else 'Inactif'}** | "
            f"Vend **{scale_pct:.0f}%** au take-profit\n"
            "  _Sécurise une partie, laisse le reste courir_\n\n"
            "💡 _Le SL/TP classique se configure dans le menu principal_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    if field == "v3_portfolio":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)

        max_pos = getattr(us, "max_positions", 15)
        max_cat = getattr(us, "max_category_exposure_pct", 30)
        max_bias = getattr(us, "max_direction_bias_pct", 70)

        keyboard = [
            [InlineKeyboardButton(
                f"📦 Max positions: {max_pos}",
                callback_data="set_max_positions",
            )],
            [InlineKeyboardButton(
                f"📂 Max par catégorie: {max_cat:.0f}%",
                callback_data="set_max_category_exposure_pct",
            )],
            [InlineKeyboardButton(
                f"⚖️ Biais max: {max_bias:.0f}%",
                callback_data="set_max_direction_bias_pct",
            )],
            [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
        ]
        await query.edit_message_text(
            "📦 **PORTFOLIO & RISQUE**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Contrôles au niveau du portfolio. Empêche la surexposition._\n\n"
            f"**Max positions** : **{max_pos}**\n"
            "  Nombre total de positions ouvertes simultanément.\n"
            "  _Au-delà, les nouveaux trades sont ignorés._\n\n"
            f"**Max par catégorie** : **{max_cat:.0f}%**\n"
            "  Limite l'exposition dans une catégorie (Crypto, Politique...).\n"
            "  _Ex: sur 10 positions, max 3 en Crypto si réglé à 30%_\n\n"
            f"**Biais directionnel** : **{max_bias:.0f}%**\n"
            "  Évite d'avoir toutes les positions dans la même direction.\n"
            "  _Ex: si 70%, max 7 positions YES sur 10 au total_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    if field == "v3_notif":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)

        mode = getattr(us, "notification_mode", "dm")
        notif_labels = {"dm": "DM", "group": "Groupe", "both": "DM + Groupe"}

        keyboard = [
            [InlineKeyboardButton(
                f"{'✅' if mode == 'dm' else '⬜'} DM uniquement",
                callback_data="set_notif_dm",
            )],
            [InlineKeyboardButton(
                f"{'✅' if mode == 'group' else '⬜'} Groupe uniquement",
                callback_data="set_notif_group",
            )],
            [InlineKeyboardButton(
                f"{'✅' if mode == 'both' else '⬜'} DM + Groupe",
                callback_data="set_notif_both",
            )],
            [InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")],
        ]
        await query.edit_message_text(
            "📬 **NOTIFICATIONS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "_Où recevoir les notifications automatiques (trades copiés, "
            "alertes SL/TP, settlements, etc.)_\n\n"
            f"Mode actuel : **{notif_labels.get(mode, 'DM')}**\n\n"
            "**📱 DM uniquement** — Tout en messages privés\n"
            "  _Classique, comme avant_\n\n"
            "**👥 Groupe uniquement** — Dans les topics du groupe\n"
            "  _Signaux → topic Signals, Alertes → topic Alerts, etc._\n\n"
            "**📱+👥 DM + Groupe** — Les deux en parallèle\n"
            "  _Maximum de visibilité_\n\n"
            "💡 _Les commandes interactives (settings, wallet, etc.) "
            "restent toujours en DM._",
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

    # ── Gas mode sub-menu ──
    if field == "gas_mode":
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)

        current_mode = getattr(us, "gas_mode", GasMode.FAST)
        keyboard = []
        for mode in GasMode:
            label = GAS_MODE_LABELS[mode]
            if mode == current_mode:
                label = f"✅ {label}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"set_gas_{mode.value}")])
        keyboard.append([InlineKeyboardButton("⬅️ Retour", callback_data="set_back_main")])

        await query.edit_message_text(
            "⛽ **Vitesse du Gas (Priority Fee)**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Contrôle la vitesse de confirmation de vos transactions sur Polygon.\n\n"
            "**Comment ça marche :**\n"
            "Plus le priority fee est élevé, plus vos transactions sont "
            "incluses rapidement dans un bloc. Cela coûte un peu plus de POL.\n\n"
            f"**Mode actuel : {GAS_MODE_LABELS[current_mode]}**\n\n"
            "💡 **Normal** = ~0.001 POL/tx\n"
            "💡 **Instant** = ~0.005 POL/tx\n\n"
            "La différence est minime (< $0.01), mais le gain de vitesse "
            "peut être significatif pour le copytrading.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    # Handle gas mode selection (set_gas_normal, set_gas_fast, etc.)
    if field.startswith("gas_"):
        gas_value = field.replace("gas_", "")
        try:
            new_mode = GasMode(gas_value)
        except ValueError:
            await query.edit_message_text("❌ Mode de gas invalide.")
            return MAIN_MENU

        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)
            await update_setting(session, us, "gas_mode", new_mode)
            await session.refresh(us)
            text, keyboard = _build_main_menu(us, user.paper_trading)
        await query.edit_message_text(
            f"✅ Gas passé en **{GAS_MODE_LABELS[new_mode]}**\n\n" + text,
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

    # ── V3 Notification mode selector ──
    if field in ("notif_dm", "notif_group", "notif_both"):
        mode_map = {"notif_dm": "dm", "notif_group": "group", "notif_both": "both"}
        new_mode = mode_map[field]
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            us = await get_or_create_settings(session, user)
            await update_setting(session, us, "notification_mode", new_mode)
            await session.refresh(us)
            text, keyboard = _build_main_menu(us, user.paper_trading)
        label = {"dm": "DM", "group": "Groupe", "both": "DM + Groupe"}[new_mode]
        await query.edit_message_text(
            f"✅ Notifications passées en **{label}**\n\n" + text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    # Toggle fields (boolean) — V1 + V3
    V3_TOGGLE_FIELDS = (
        "signal_scoring_enabled", "smart_filter_enabled", "auto_pause_cold_traders",
        "skip_coin_flip", "trailing_stop_enabled", "time_exit_enabled", "scale_out_enabled",
    )
    if field in ("manual_confirmation", "auto_bridge_sol", "use_gamma_monitor", "use_ws_monitor") + V3_TOGGLE_FIELDS:
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

            if user.paper_trading:
                # Switching from PAPER → LIVE: show warning, require confirmation
                keyboard = [
                    [InlineKeyboardButton(
                        "⚠️ OUI, passer en LIVE (argent réel)",
                        callback_data="set_confirm_live_mode",
                    )],
                    [InlineKeyboardButton("❌ Non, rester en Paper", callback_data="set_back_main")],
                ]
                await query.edit_message_text(
                    "🚨 **ATTENTION — MODE LIVE (ARGENT RÉEL)**\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "⚠️ **Vous êtes sur le point de passer en mode LIVE.**\n\n"
                    "En mode LIVE :\n"
                    "• Le bot utilisera vos **vrais USDC** pour trader\n"
                    "• Les frais de gas (POL) seront **réellement dépensés**\n"
                    "• Les trades seront **irréversibles**\n"
                    "• 1% de frais sera prélevé **en USDC réel**\n\n"
                    "💰 Votre wallet actif : `" + (user.wallet_address or "non configuré")[:20] + "...`\n\n"
                    "**Êtes-vous absolument sûr(e) ?**",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return MAIN_MENU
            else:
                # Switching from LIVE → PAPER: safe, no confirmation needed
                user.paper_trading = True
                user.live_mode_confirmed = False
                await session.commit()
                us = await get_or_create_settings(session, user)
                text, keyboard = _build_main_menu(us, user.paper_trading)

        await query.edit_message_text(
            "✅ Mode **Paper Trading (simulation)** activé.\n"
            "Vos vrais USDC ne seront plus utilisés.\n\n" + text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return MAIN_MENU

    if field == "confirm_live_mode":
        # Second confirmation step — actually switch to live
        async with async_session() as session:
            user = await get_user_by_telegram_id(session, query.from_user.id)
            user.paper_trading = False
            user.live_mode_confirmed = True
            await session.commit()
            us = await get_or_create_settings(session, user)
            text, keyboard = _build_main_menu(us, user.paper_trading)

        logger.warning(
            f"⚠️ User {query.from_user.id} switched to LIVE mode "
            f"(wallet: {user.wallet_address})"
        )

        await query.edit_message_text(
            "🔴 **MODE LIVE ACTIVÉ**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Le bot utilisera désormais vos **vrais USDC** pour copier les trades.\n\n"
            "💡 Pour revenir en mode simulation, appuyez sur "
            "« 📝 Paper OFF » dans les paramètres.\n\n" + text,
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
    """Go directly to the add-trader prompt (the list is managed in menu_traders)."""
    # Skip the intermediate list — menu_traders already shows it.
    # Go straight to add prompt.
    return await follow_add_prompt(update, context)


async def follow_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask user to send a wallet address to follow."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("👥 Annuler — Traders suivis", callback_data="menu_traders")],
    ]
    await query.edit_message_text(
        "➕ **AJOUTER UN TRADER**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Envoyez l'adresse Polygon (0x…) du trader à copier.\n\n"
        "**Où trouver l'adresse :**\n"
        "1. Allez sur le profil Polymarket du trader\n"
        "2. Copiez l'adresse de son wallet\n"
        "3. Collez-la ici\n\n"
        "**Format :** `0x1234...abcd` (42 caractères)",
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
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👥 Traders suivis", callback_data="menu_traders")],
                ]),
            )
            return MAIN_MENU

        wallets.append(address)
        await update_setting(session, us, "followed_wallets", wallets)

        await update.message.reply_text(
            f"✅ Trader `{address[:6]}...{address[-4:]}` ajouté !\n\n"
            f"Vous suivez maintenant **{len(wallets)}** trader(s).",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 Traders suivis", callback_data="menu_traders")],
                [InlineKeyboardButton("➕ Ajouter un autre", callback_data="follow_add")],
            ]),
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

        # Sync allocated_capital → paper_balance when in paper mode
        if field == "allocated_capital" and user.paper_trading:
            user.paper_balance = value
            user.paper_initial_balance = value
            # Reset daily spent since capital changed
            user.daily_spent_usdc = 0.0
            await session.commit()

        text, keyboard = _build_main_menu(us, user.paper_trading)

    label = SETTING_LABELS.get(field, (field, ""))[0]
    extra = ""
    if field == "allocated_capital" and user.paper_trading:
        extra = f"\n💰 Solde paper mis à jour : **{value:.0f} USDC**\n"
    await update.message.reply_text(
        f"✅ **{label}** mis à jour : **{value}**\n{extra}\n" + text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    context.user_data.pop("editing_field", None)
    return MAIN_MENU


async def _exit_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exit ConversationHandler and let menu.py handle the callback."""
    # End the conversation so menu_traders handler in menu.py can pick it up
    return ConversationHandler.END


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
                CallbackQueryHandler(_exit_to_menu, pattern="^menu_traders$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, follow_add_receive),
            ],
        },
        fallbacks=[
            CommandHandler("settings", settings_command),
            CallbackQueryHandler(_exit_to_menu, pattern="^menu_traders$"),
        ],
        per_user=True,
        per_message=False,
    )
