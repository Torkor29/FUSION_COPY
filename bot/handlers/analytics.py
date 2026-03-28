"""Analytics V3 — tableau de bord d'intelligence trading.

Commandes /analytics et boutons associés :
- Trader Stats : performance des traders suivis
- Portfolio : positions ouvertes + métriques de risque
- Signal History : historique des signaux scorés
- Smart Filters : statistiques de filtrage
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler

from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id, get_or_create_settings

logger = logging.getLogger(__name__)


async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Afficher le tableau de bord Analytics V3."""
    keyboard = [
        [
            InlineKeyboardButton("👤 Traders", callback_data="v3_trader_stats"),
            InlineKeyboardButton("💼 Portfolio", callback_data="v3_portfolio"),
        ],
        [
            InlineKeyboardButton("📊 Signaux", callback_data="v3_signal_history"),
            InlineKeyboardButton("🎯 Filtres", callback_data="v3_filter_stats"),
        ],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
    ]

    text = (
        "📈 **ANALYTICS V3**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "_Votre tableau de bord d'intelligence trading._\n\n"
        "**👤 Traders** — Performance de chaque trader suivi\n"
        "  _Win rate, PNL, streaks, catégories fortes/faibles_\n\n"
        "**💼 Portfolio** — Vue d'ensemble de vos positions\n"
        "  _Exposition, PNL non réalisé, répartition_\n\n"
        "**📊 Signaux** — Historique des signaux scorés\n"
        "  _Score 0-100, breakdown par critère_\n\n"
        "**🎯 Filtres** — Efficacité du filtre intelligent\n"
        "  _Combien de trades bloqués vs acceptés_"
    )

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def cb_trader_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Afficher les performances des traders suivis."""
    await update.callback_query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if not user:
            await update.callback_query.edit_message_text(
                "❌ Compte non trouvé. Utilisez /start."
            )
            return
        user_settings = await get_or_create_settings(session, user)
        wallets = user_settings.followed_wallets or []

    if not wallets:
        await update.callback_query.edit_message_text(
            "👤 **TRADERS SUIVIS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Aucun trader suivi pour le moment.\n\n"
            "Ajoutez des traders dans ⚙️ **Paramètres** → **Traders suivis** "
            "pour voir leurs performances ici.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Paramètres", callback_data="menu_settings")],
                [InlineKeyboardButton("⬅️ Analytics", callback_data="v3_analytics")],
            ]),
        )
        return

    from bot.services.trader_tracker import TraderTracker
    tracker = TraderTracker()

    lines = [
        "👤 **PERFORMANCE DES TRADERS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "_Stats calculées sur vos trades copiés et résolus._\n"
    ]

    for wallet in wallets[:10]:
        try:
            report = await tracker.format_trader_report(wallet)
            lines.append(report)
            lines.append("")
        except Exception:
            short = f"{wallet[:6]}...{wallet[-4:]}"
            lines.append(f"📊 `{short}` — _Pas assez de données_\n")

    keyboard = [
        [InlineKeyboardButton("🔄 Rafraichir", callback_data="v3_trader_stats")],
        [InlineKeyboardButton("⬅️ Analytics", callback_data="v3_analytics")],
    ]

    await update.callback_query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cb_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Afficher la vue portfolio."""
    await update.callback_query.answer()

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if not user:
            await update.callback_query.edit_message_text(
                "❌ Compte non trouvé. Utilisez /start."
            )
            return

    from bot.services.portfolio_manager import PortfolioManager
    pm = PortfolioManager()

    try:
        report = await pm.format_portfolio_report(user.id)
    except Exception as e:
        report = (
            "💼 **PORTFOLIO**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"❌ Erreur lors du chargement : {str(e)[:100]}\n\n"
            "_Vérifiez que vous avez des positions ouvertes._"
        )

    keyboard = [
        [InlineKeyboardButton("🔄 Rafraichir", callback_data="v3_portfolio")],
        [InlineKeyboardButton("⬅️ Analytics", callback_data="v3_analytics")],
    ]

    await update.callback_query.edit_message_text(
        report,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cb_signal_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Afficher l'historique des signaux scorés."""
    await update.callback_query.answer()

    from sqlalchemy import select
    from bot.models.signal_score import SignalScore

    async with async_session() as session:
        stmt = (
            select(SignalScore)
            .order_by(SignalScore.created_at.desc())
            .limit(10)
        )
        scores = (await session.execute(stmt)).scalars().all()

    if not scores:
        text = (
            "📊 **HISTORIQUE DES SIGNAUX**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Aucun signal scoré pour le moment.\n\n"
            "_Les signaux apparaitront ici dès qu'un trader suivi "
            "ouvrira une position._\n\n"
            "**Comment fonctionne le score :**\n"
            "Chaque signal est noté de 0 à 100 sur 6 critères :\n\n"
            "📏 *Spread* (15%) — écart bid/ask\n"
            "   _< 1% = 100pts | 1-2% = 80 | 2-3% = 60 | > 5% = 0_\n"
            "💧 *Liquidité* (15%) — volume 24h du marché\n"
            "   _> $500K = 100 | > $100K = 80 | > $50K = 60 | < $10K = 10_\n"
            "💪 *Conviction* (20%) — taille trade vs portfolio trader\n"
            "   _> 10% = 100 | > 5% = 80 | > 2% = 60 | < 2% = 20_\n"
            "📈 *Forme trader* (20%) — win rate 7 jours\n"
            "   _> 70% = 100 | > 60% = 80 | > 50% = 60 | < 40% = 0_\n"
            "⏱ *Timing* (15%) — distance à l'expiry\n"
            "   _2-48h = 100 | 2j-1sem = 80 | 1sem-1mois = 60_\n"
            "👥 *Consensus* (15%) — autres traders sur le même marché\n"
            "   _3+ traders = 100 | 2 = 70 | 1 = 40 | 0 = 20_\n\n"
            "Le score total = somme pondérée de ces 6 notes."
        )
    else:
        lines = [
            "📊 **DERNIERS SIGNAUX SCORÉS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
        ]
        for s in scores:
            if s.total_score >= 75:
                grade = "🟢"
            elif s.total_score >= 50:
                grade = "🟡"
            elif s.total_score >= 30:
                grade = "🟠"
            else:
                grade = "🔴"

            short_wallet = f"{s.master_wallet[:6]}..{s.master_wallet[-4:]}"
            comp = s.components or {}

            # Extract reasons from each component
            details = []
            for key, label in [
                ("spread", "Spread"), ("liquidity", "Liquidité"),
                ("conviction", "Conviction"), ("trader_form", "Forme"),
                ("timing", "Timing"), ("consensus", "Consensus"),
            ]:
                c = comp.get(key, {})
                if isinstance(c, dict):
                    sc = c.get("score", 50)
                    reason = c.get("reason", "")
                    if reason:
                        icon = "✅" if sc >= 70 else ("⚠️" if sc >= 40 else "❌")
                        details.append(f"  {icon} {reason}")

            lines.append(
                f"{grade} **{s.total_score:.0f}**/100 — "
                f"{s.side} par `{short_wallet}`"
            )
            if details:
                lines.extend(details[:4])  # Max 4 details to keep it readable
            lines.append("")

        text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("🔄 Rafraichir", callback_data="v3_signal_history")],
        [InlineKeyboardButton("⬅️ Analytics", callback_data="v3_analytics")],
    ]

    await update.callback_query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cb_filter_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Afficher les statistiques du filtre intelligent."""
    await update.callback_query.answer()

    from sqlalchemy import select, func
    from bot.models.signal_score import SignalScore

    async with async_session() as session:
        total = (
            await session.execute(select(func.count(SignalScore.id)))
        ).scalar() or 0
        passed = (
            await session.execute(
                select(func.count(SignalScore.id)).where(
                    SignalScore.passed == True  # noqa: E712
                )
            )
        ).scalar() or 0

    blocked = total - passed
    pass_rate = (passed / total * 100) if total > 0 else 0
    block_rate = 100 - pass_rate

    # Visual bar
    bar_len = 20
    passed_bars = int(pass_rate / 100 * bar_len)
    bar = "🟢" * passed_bars + "🔴" * (bar_len - passed_bars)

    text = (
        "🎯 **EFFICACITÉ DU FILTRE INTELLIGENT**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Total signaux analysés : **{total}**\n\n"
        f"✅ Acceptés : **{passed}** ({pass_rate:.0f}%)\n"
        f"❌ Bloqués : **{blocked}** ({block_rate:.0f}%)\n\n"
        f"{bar}\n\n"
    )

    if total == 0:
        text += (
            "_Aucun signal encore. Les stats apparaitront dès que "
            "des signaux seront analysés par le scoring engine._"
        )
    elif block_rate > 60:
        text += (
            "📈 **Filtre très sélectif** — seuls les meilleurs trades passent.\n"
            "_Moins de trades, mais meilleure qualité._"
        )
    elif block_rate > 30:
        text += (
            "⚖️ **Filtre équilibré** — bon compromis quantité/qualité.\n"
            "_La plupart des mauvais trades sont filtrés._"
        )
    else:
        text += (
            "⚠️ **Filtre permissif** — beaucoup de trades passent.\n"
            "_Augmentez le score minimum dans ⚙️ Paramètres → Smart Analysis "
            "pour être plus sélectif._"
        )

    keyboard = [
        [InlineKeyboardButton("🔄 Rafraichir", callback_data="v3_filter_stats")],
        [InlineKeyboardButton("🧠 Régler les filtres", callback_data="set_v3_smart")],
        [InlineKeyboardButton("⬅️ Analytics", callback_data="v3_analytics")],
    ]

    await update.callback_query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def get_analytics_handlers():
    """Retourne tous les handlers Analytics V3."""
    return [
        CommandHandler("analytics", cmd_analytics),
        CallbackQueryHandler(cmd_analytics, pattern="^v3_analytics$"),
        CallbackQueryHandler(cb_trader_stats, pattern="^v3_trader_stats$"),
        CallbackQueryHandler(cb_portfolio, pattern="^v3_portfolio$"),
        CallbackQueryHandler(cb_signal_history, pattern="^v3_signal_history$"),
        CallbackQueryHandler(cb_filter_stats, pattern="^v3_filter_stats$"),
    ]
