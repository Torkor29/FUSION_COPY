"""Templates de notifications Telegram — V3 enrichies.

Chaque notification inclut désormais :
- Le score du signal (si disponible)
- L'indication du topic de destination
- Des détails V3 (trailing stop, position management)
"""

from bot.models.trade import Trade, TradeSide
from bot.services.fees import FeeResult


def format_trade_notification(
    trade: Trade,
    fee_result: FeeResult,
    execution_time_s: float = 0.0,
    bridge_used: bool = False,
    master_pnl: float = 0.0,
    signal_score: float = 0.0,
    score_grade: str = "",
    sl_price: float = 0.0,
    tp_price: float = 0.0,
) -> str:
    """Formater une notification de trade copié — version V3.

    Inclut le score du signal, le grade, et les niveaux SL/TP si actifs.
    """
    side_emoji = "🟢" if trade.side == TradeSide.BUY else "🔴"
    side_label = "YES" if trade.side == TradeSide.BUY else "NO"
    question = trade.market_question or trade.market_id
    paper_label = " 📝 PAPER" if trade.is_paper else " 💵 LIVE"

    shares = (
        trade.shares
        if trade.shares
        else (fee_result.net_amount / trade.price if trade.price > 0 else 0)
    )

    master_pnl_str = (
        f"+{master_pnl:.1f}%" if master_pnl >= 0 else f"{master_pnl:.1f}%"
    )

    # V3: Score line
    score_line = ""
    if signal_score > 0:
        if not score_grade:
            if signal_score >= 75:
                score_grade = "🟢 EXCELLENT"
            elif signal_score >= 50:
                score_grade = "🟡 BON"
            else:
                score_grade = "🟠 FAIBLE"
        score_line = f"🧠 Score signal   : **{signal_score:.0f}/100** {score_grade}\n"

    # V3: Risk management lines
    risk_lines = ""
    if trade.side == TradeSide.BUY:
        if sl_price > 0:
            risk_lines += f"🛑 Stop-loss      : ${sl_price:.4f}\n"
        if tp_price > 0:
            risk_lines += f"🎯 Take-profit    : ${tp_price:.4f}\n"

    return (
        f"{side_emoji} **TRADE COPIÉ**{paper_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 _{question}_\n\n"
        f"🎯 Position       : **{side_label}** @ ${trade.price:.4f}\n"
        f"💵 Mise brute     : {fee_result.gross_amount:.2f} USDC\n"
        f"💸 Frais ({fee_result.fee_rate:.0%})     : -{fee_result.fee_amount:.2f} USDC\n"
        f"✅ Mise nette     : **{fee_result.net_amount:.2f} USDC**\n"
        f"📊 Shares         : {shares:.2f}\n"
        f"{score_line}"
        f"{risk_lines}"
        f"⏱️ Exécuté en     : {execution_time_s:.1f}s\n"
        f"📈 P&L master     : {master_pnl_str}"
    )


def format_trade_error(
    market_question: str,
    error_message: str,
) -> str:
    """Formater une notification d'erreur de trade."""
    return (
        "🚨 **ERREUR DE TRADE**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 _{market_question}_\n\n"
        f"❌ {error_message}\n\n"
        "💡 Le trade n'a pas été exécuté.\n"
        "Vérifiez vos paramètres via ⚙️ **Paramètres**."
    )


def format_bridge_notification(
    amount_sol: float,
    amount_usdc: float,
    bridge_provider: str,
    fee_usd: float,
    tx_hash: str,
    status: str = "completed",
) -> str:
    """Formater une notification de bridge SOL → USDC."""
    status_emoji = (
        "✅" if status == "completed" else "🟡" if status == "pending" else "🔴"
    )

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


def format_signal_blocked(
    market_question: str,
    reason: str,
    score: float = 0.0,
) -> str:
    """Formater une notification de signal bloqué par les filtres V3."""
    return (
        "🚫 **SIGNAL FILTRÉ**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 _{market_question}_\n\n"
        f"🧠 Score : **{score:.0f}/100**\n"
        f"❌ Raison : {reason}\n\n"
        "💡 _Ce trade n'a pas été copié. Le filtre intelligent "
        "a jugé que le rapport risque/récompense n'était pas favorable._"
    )


def format_position_exit(
    market_question: str,
    reason: str,
    entry_price: float,
    exit_price: float,
    pnl_pct: float,
    shares: float,
) -> str:
    """Formater une notification de sortie de position (SL/TP/trailing)."""
    reason_labels = {
        "sl_hit": "🔴 Stop-Loss déclenché",
        "tp_hit": "🟢 Take-Profit atteint",
        "trailing_stop": "🟡 Trailing Stop activé",
        "time_exit": "⏰ Sortie temporelle (position plate)",
        "scale_out": "📊 Prise de profit partielle",
        "manual": "👤 Fermeture manuelle",
    }
    reason_label = reason_labels.get(reason, reason)
    pnl_emoji = "📈" if pnl_pct >= 0 else "📉"

    return (
        f"🚨 **SORTIE DE POSITION**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 _{market_question}_\n\n"
        f"**{reason_label}**\n\n"
        f"📍 Entrée  : ${entry_price:.4f}\n"
        f"📍 Sortie  : ${exit_price:.4f}\n"
        f"{pnl_emoji} P&L     : **{pnl_pct:+.1f}%**\n"
        f"📊 Shares  : {shares:.2f}"
    )
