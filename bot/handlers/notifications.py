"""Templates de notifications Telegram — V3 refonte visuelle.

Toutes les notifications utilisent le module formatting.py
pour des barres, badges et formats cohérents.
"""

from bot.models.trade import Trade, TradeSide
from bot.services.fees import FeeResult
from bot.utils.formatting import (
    SEP, fmt_usd, fmt_pnl, badge_score, badge_position_status,
    short_wallet, bar, fmt_duration,
)


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
    """Notification de trade copié — format compact et visuel."""
    side_emoji = "🟢" if trade.side == TradeSide.BUY else "🔴"
    side_label = "YES" if trade.side == TradeSide.BUY else "NO"
    question = trade.market_question or trade.market_id[:30]
    mode = "📝 PAPER" if trade.is_paper else "💵 LIVE"

    shares = (
        trade.shares
        if trade.shares
        else (fee_result.net_amount / trade.price if trade.price > 0 else 0)
    )

    master_sign = "+" if master_pnl >= 0 else ""

    # Score line
    score_line = ""
    if signal_score > 0:
        if not score_grade:
            score_grade = badge_score(signal_score)
        score_bar = bar(signal_score, 100, 10)
        score_line = f"🧠 {score_bar} *{signal_score:.0f}/100* {score_grade}\n"

    # Risk lines (compact)
    risk_line = ""
    if trade.side == TradeSide.BUY and (sl_price > 0 or tp_price > 0):
        parts = []
        if sl_price > 0:
            parts.append(f"SL ${sl_price:.4f}")
        if tp_price > 0:
            parts.append(f"TP ${tp_price:.4f}")
        risk_line = f"🛡 {' | '.join(parts)}\n"

    return (
        f"{side_emoji} *TRADE COPIÉ* {mode}\n"
        f"{SEP}\n"
        f"📋 _{question}_\n\n"
        f"*{side_label}* @ ${trade.price:.4f} | "
        f"*{fmt_usd(fee_result.net_amount)}* net | "
        f"{shares:.1f} shares\n"
        f"{score_line}"
        f"{risk_line}"
        f"⏱ {fmt_duration(execution_time_s)} | "
        f"Fee {fmt_usd(fee_result.fee_amount)} ({fee_result.fee_rate:.0%}) | "
        f"Master {master_sign}{master_pnl:.1f}%"
    )


def format_trade_error(
    market_question: str,
    error_message: str,
) -> str:
    """Notification d'erreur de trade — concise."""
    return (
        f"🚨 *ERREUR*\n"
        f"{SEP}\n"
        f"📋 _{market_question}_\n\n"
        f"❌ {error_message}\n\n"
        f"_Trade non exécuté — vérifiez ⚙️ Paramètres_"
    )


def format_bridge_notification(
    amount_sol: float,
    amount_usdc: float,
    bridge_provider: str,
    fee_usd: float,
    tx_hash: str,
    status: str = "completed",
) -> str:
    """Notification de bridge SOL → USDC."""
    status_map = {
        "completed": "✅ Terminé",
        "pending": "🟡 En cours",
        "failed": "🔴 Échoué",
    }
    status_label = status_map.get(status, status)

    return (
        f"🌉 *BRIDGE SOL → USDC*\n"
        f"{SEP}\n"
        f"☀️ {amount_sol:.4f} SOL → 💵 {fmt_usd(amount_usdc)}\n"
        f"🔄 {bridge_provider} | Fee {fmt_usd(fee_usd)}\n"
        f"📋 `{tx_hash[:10]}...{tx_hash[-6:]}`\n"
        f"{status_label}"
    )


def format_signal_blocked(
    market_question: str,
    reason: str,
    score: float = 0.0,
) -> str:
    """Notification de signal bloqué par les filtres V3."""
    grade = badge_score(score)
    score_bar = bar(score, 100, 10) if score > 0 else ""

    return (
        f"🚫 *SIGNAL FILTRÉ*\n"
        f"{SEP}\n"
        f"📋 _{market_question}_\n\n"
        f"🧠 {score_bar} *{score:.0f}/100* {grade}\n"
        f"❌ {reason}\n\n"
        f"_Trade non copié — ajustez les filtres dans_\n"
        f"_⚙️ → 🧠 Smart Analysis si nécessaire_"
    )


def format_position_exit(
    market_question: str,
    reason: str,
    entry_price: float,
    exit_price: float,
    pnl_pct: float,
    shares: float,
    pnl_usdc: float = 0,
    holding_duration: str = "",
) -> str:
    """Notification de sortie de position (SL/TP/trailing)."""
    reason_labels = {
        "sl_hit": "🔴 Stop-Loss déclenché",
        "tp_hit": "🟢 Take-Profit atteint",
        "trailing_stop": "🟡 Trailing Stop activé",
        "time_exit": "⏰ Sortie temporelle",
        "scale_out": "📊 Prise de profit partielle",
        "manual": "👤 Fermeture manuelle",
    }
    reason_label = reason_labels.get(reason, reason)
    status = badge_position_status(pnl_pct)

    # PNL line
    sign = "+" if pnl_pct >= 0 else ""
    pnl_line = f"{status} *{sign}{pnl_pct:.1f}%*"
    if pnl_usdc != 0:
        pnl_line += f" ({sign}{fmt_usd(pnl_usdc)})"

    # Duration
    dur_line = f" | Durée: {holding_duration}" if holding_duration else ""

    return (
        f"🚨 *SORTIE DE POSITION*\n"
        f"{SEP}\n"
        f"*{reason_label}*\n\n"
        f"📋 _{market_question}_\n"
        f"📍 ${entry_price:.4f} → ${exit_price:.4f}\n"
        f"{pnl_line}\n"
        f"📊 {shares:.1f} shares{dur_line}"
    )


def format_settlement(
    question: str,
    outcome: str,
    won: bool,
    invested: float,
    payout: float,
    pnl: float,
    is_paper: bool = False,
) -> str:
    """Notification de settlement — marché résolu."""
    emoji = "🏆" if won else "💔"
    result = "GAGNÉ" if won else "PERDU"
    mode = " 📝" if is_paper else ""

    return (
        f"{emoji} *MARCHÉ RÉSOLU*{mode}\n"
        f"{SEP}\n"
        f"📋 _{question}_\n"
        f"🏆 *{outcome}* → *{result}*\n\n"
        f"💵 Mise: {fmt_usd(invested)} → Payout: {fmt_usd(payout)}\n"
        f"{fmt_pnl(pnl, show_both=False)}"
    )
