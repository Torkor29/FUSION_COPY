"""Notification templates for Telegram messages."""

from bot.models.trade import Trade, TradeSide
from bot.services.fees import FeeResult


def format_trade_notification(
    trade: Trade,
    fee_result: FeeResult,
    execution_time_s: float = 0.0,
    bridge_used: bool = False,
    master_pnl: float = 0.0,
) -> str:
    """Format a new trade notification for Telegram."""
    side_emoji = "🟢" if trade.side == TradeSide.BUY else "🔴"
    side_label = "YES" if trade.side == TradeSide.BUY else "NO"
    question = trade.market_question or trade.market_id
    paper_label = " 📝 PAPER" if trade.is_paper else ""

    shares = trade.shares if trade.shares else fee_result.net_amount / trade.price if trade.price > 0 else 0

    bridge_label = "Oui" if bridge_used else "Non (USDC dispo)"
    master_pnl_str = f"+{master_pnl:.1f}%" if master_pnl >= 0 else f"{master_pnl:.1f}%"

    return (
        f"{side_emoji} **NOUVEAU TRADE COPIÉ**{paper_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Marché : \"{question}\"\n"
        f"🎯 Position : {side_label} @ {trade.price:.2f} USDC\n"
        f"💵 Mise brute     : {fee_result.gross_amount:.2f} USDC\n"
        f"💸 Frais ({fee_result.fee_rate:.0%})     : -{fee_result.fee_amount:.2f} USDC\n"
        f"✅ Mise nette     : {fee_result.net_amount:.2f} USDC\n"
        f"📊 Shares         : {shares:.2f}\n"
        f"⏱️ Exécuté en     : {execution_time_s:.1f}s\n"
        f"🌉 Bridge utilisé : {bridge_label}\n"
        f"📈 P&L master     : {master_pnl_str} depuis ouverture"
    )


def format_trade_error(
    market_question: str,
    error_message: str,
) -> str:
    """Format a trade error notification."""
    return (
        "🔴 **ERREUR DE TRADE**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Marché : \"{market_question}\"\n"
        f"❌ Erreur : {error_message}\n\n"
        "Le trade n'a pas été exécuté. Vérifiez vos paramètres via le bouton "
        "« ⚙️ Paramètres » du menu principal."
    )


def format_bridge_notification(
    amount_sol: float,
    amount_usdc: float,
    bridge_provider: str,
    fee_usd: float,
    tx_hash: str,
    status: str = "completed",
) -> str:
    """Format a bridge operation notification."""
    status_emoji = "✅" if status == "completed" else "🟡" if status == "pending" else "🔴"

    return (
        f"🌉 **BRIDGE SOL → USDC**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"☀️ Envoyé     : {amount_sol:.4f} SOL\n"
        f"💵 Reçu       : {amount_usdc:.2f} USDC (Polygon)\n"
        f"🔄 Provider   : {bridge_provider}\n"
        f"💸 Frais      : {fee_usd:.2f} USD\n"
        f"📋 TX         : `{tx_hash[:10]}...{tx_hash[-6:]}`\n"
        f"{status_emoji} Statut      : {status}"
    )
