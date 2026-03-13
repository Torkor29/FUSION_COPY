"""Balance handler — /balance and /positions commands."""

import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.db.session import async_session
from bot.services.user_service import get_user_by_telegram_id
from bot.models.trade import Trade, TradeStatus

from sqlalchemy import select, func

logger = logging.getLogger(__name__)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's balance and wallet info."""
    tg_user = update.effective_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user:
            await update.message.reply_text(
                "❌ Compte non trouvé. Utilisez /start pour vous inscrire."
            )
            return

        # Si aucun wallet Polygon n'est encore configuré, proposer directement
        # l'import ou la création d'un wallet dédié.
        if not user.wallet_address:
            text = (
                "💰 **SOLDES & PORTEFEUILLE**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Tu n'as pas encore de **wallet Polygon** configuré pour le copy-trading.\n\n"
                "Choisis une option :\n"
                "• 📩 *J'ai déjà un wallet Polygon* → importer ton wallet existant\n"
                "• 🆕 *Me créer un wallet Polygon* → le bot génère un wallet dédié\n"
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "📩 J'ai déjà un wallet Polygon",
                        callback_data="menu_wallet_import",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🆕 Me créer un wallet Polygon",
                        callback_data="menu_wallet_create",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🌉 J'ai de la crypto ailleurs (bridge)",
                        callback_data="menu_wallet_bridge",
                    )
                ],
                [InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back")],
            ]

            await update.message.reply_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        # Count open positions
        open_count = await session.scalar(
            select(func.count(Trade.id)).where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
            )
        ) or 0

        # Total invested
        total_invested = await session.scalar(
            select(func.sum(Trade.net_amount_usdc)).where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
            )
        ) or 0.0

        # Total fees paid
        from bot.models.fee import FeeRecord
        total_fees = await session.scalar(
            select(func.sum(FeeRecord.fee_amount)).where(
                FeeRecord.user_id == user.id,
            )
        ) or 0.0

        wallet_display = "Non configuré"
        if user.wallet_address:
            w = user.wallet_address
            wallet_display = f"`{w[:6]}...{w[-4:]}`"

        sol_display = "Non configuré"
        if user.solana_wallet_address:
            s = user.solana_wallet_address
            sol_display = f"`{s[:4]}...{s[-4:]}`"

        text = (
            "💰 **SOLDES & PORTEFEUILLE**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔷 **Polygon (Polymarket)**\n"
            f"   📬 Wallet : {wallet_display}\n"
            f"   💵 USDC disponible : *chargement...*\n\n"
            f"🟣 **Solana**\n"
            f"   📬 Wallet : {sol_display}\n"
            f"   ☀️ SOL disponible : *chargement...*\n\n"
            f"📊 **Positions**\n"
            f"   📈 Positions ouvertes : **{open_count}**\n"
            f"   💰 Total investi : **{total_invested:.2f} USDC**\n"
            f"   💸 Frais payés (total) : **{total_fees:.2f} USDC**\n\n"
            f"📝 Mode : **{'Paper Trading' if user.paper_trading else 'Trading réel'}**\n"
            f"⏸️ Statut : **{'En pause' if user.is_paused else 'Actif'}**"
        )

        keyboard = [
            [
                InlineKeyboardButton("📊 Positions", callback_data="menu_positions"),
                InlineKeyboardButton("📜 Historique", callback_data="menu_history"),
            ],
            [
                InlineKeyboardButton("💳 Déposer", callback_data="menu_deposit"),
                InlineKeyboardButton("💸 Retirer", callback_data="menu_withdraw"),
            ],
            [
                InlineKeyboardButton("🏠 Menu principal", callback_data="menu_back"),
            ],
        ]

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show active copytrading positions."""
    tg_user = update.effective_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user:
            await update.message.reply_text("❌ Compte non trouvé. /start pour s'inscrire.")
            return

        result = await session.execute(
            select(Trade)
            .where(
                Trade.user_id == user.id,
                Trade.status == TradeStatus.FILLED,
            )
            .order_by(Trade.created_at.desc())
            .limit(20)
        )
        trades = result.scalars().all()

    if not trades:
        await update.message.reply_text(
            "📊 **Positions actives**\n\n"
            "Aucune position ouverte pour le moment.\n"
            "Les trades seront copiés automatiquement quand le master tradera.",
            parse_mode="Markdown",
        )
        return

    lines = ["📊 **POSITIONS ACTIVES**\n━━━━━━━━━━━━━━━━━━━━\n"]
    for t in trades:
        emoji = "🟢" if t.side.value == "buy" else "🔴"
        question = t.market_question or t.market_id
        if len(question) > 40:
            question = question[:37] + "..."
        lines.append(
            f"{emoji} **{question}**\n"
            f"   {t.side.value.upper()} @ {t.price:.2f} | "
            f"Net: {t.net_amount_usdc:.2f} USDC | "
            f"Shares: {t.shares:.1f}\n"
        )

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show trade history with P&L and fees."""
    tg_user = update.effective_user

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, tg_user.id)
        if not user:
            await update.message.reply_text("❌ Compte non trouvé. /start pour s'inscrire.")
            return

        result = await session.execute(
            select(Trade)
            .where(Trade.user_id == user.id)
            .order_by(Trade.created_at.desc())
            .limit(20)
        )
        trades = result.scalars().all()

        # Total fees
        from bot.models.fee import FeeRecord
        total_fees = await session.scalar(
            select(func.sum(FeeRecord.fee_amount)).where(
                FeeRecord.user_id == user.id,
            )
        ) or 0.0

    if not trades:
        await update.message.reply_text(
            "📜 **Historique des trades**\n\nAucun trade enregistré.",
            parse_mode="Markdown",
        )
        return

    lines = [
        "📜 **HISTORIQUE DES TRADES**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    ]

    status_emoji = {
        TradeStatus.FILLED: "✅",
        TradeStatus.FAILED: "❌",
        TradeStatus.CANCELLED: "🚫",
        TradeStatus.PENDING: "🟡",
        TradeStatus.EXECUTING: "🔄",
    }

    for t in trades:
        emoji = status_emoji.get(t.status, "❓")
        question = t.market_question or t.market_id
        if len(question) > 35:
            question = question[:32] + "..."
        date_str = t.created_at.strftime("%d/%m %H:%M") if t.created_at else "?"
        paper = " 📝" if t.is_paper else ""
        lines.append(
            f"{emoji} {date_str} | {t.side.value.upper()} | "
            f"{t.net_amount_usdc:.2f} USDC | "
            f"Fee: {t.fee_amount_usdc:.2f}{paper}\n"
            f"   {question}\n"
        )

    lines.append(f"\n💸 **Total frais payés : {total_fees:.2f} USDC**")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


def get_balance_handlers() -> list:
    """Return all balance-related command handlers."""
    return [
        CommandHandler("balance", balance_command),
        CommandHandler("positions", positions_command),
        CommandHandler("history", history_command),
    ]
