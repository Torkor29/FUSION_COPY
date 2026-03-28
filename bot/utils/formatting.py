"""Module de formatage visuel commun — V3.

Fournit des fonctions de formatage cohérentes pour tous les écrans :
- Barres de progression
- Badges de statut
- Formatage PNL
- Formatage d'adresses wallet
- Micro-sparklines
- Séparateurs et headers
"""

from datetime import datetime, timedelta


# ═══════════════════════════════════════════
# CONSTANTES VISUELLES
# ═══════════════════════════════════════════

SEP = "━━━━━━━━━━━━━━━━━━━━"
SEP_LIGHT = "─ ─ ─ ─ ─ ─ ─ ─ ─ ─"
SEP_DOUBLE = "═══════════════════"

# Block characters for mini-charts (ordered by height)
BLOCKS = " ▁▂▃▄▅▆▇█"


# ═══════════════════════════════════════════
# BARRES DE PROGRESSION
# ═══════════════════════════════════════════

def bar(value: float, max_val: float = 100, width: int = 10) -> str:
    """Barre de progression visuelle.

    Args:
        value: Valeur actuelle (0 à max_val)
        max_val: Valeur maximum
        width: Nombre de caractères total

    Returns:
        "████████░░" (8/10 filled)
    """
    if max_val <= 0:
        return "░" * width
    ratio = min(1.0, max(0.0, value / max_val))
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


def bar_bicolor(positive: float, negative: float, total: float, width: int = 10) -> str:
    """Barre bicolore (vert/rouge) pour win/loss.

    Returns:
        "🟢🟢🟢🟢🟢🟢🔴🔴🔴🔴"
    """
    if total <= 0:
        return "⬜" * width
    pos_ratio = min(1.0, positive / total)
    pos_chars = int(pos_ratio * width)
    return "🟢" * pos_chars + "🔴" * (width - pos_chars)


def sparkline(values: list[float], width: int = 8) -> str:
    """Mini sparkline en caractères blocks.

    Args:
        values: Liste de valeurs numériques
        width: Nombre de caractères

    Returns:
        "▂▃▅▇█▆▃▁" (trend visualization)
    """
    if not values:
        return "─" * width

    # Sample values to fit width
    if len(values) > width:
        step = len(values) / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values + [values[-1]] * (width - len(values))

    mn, mx = min(sampled), max(sampled)
    rng = mx - mn if mx > mn else 1

    return "".join(
        BLOCKS[max(1, min(8, int((v - mn) / rng * 7) + 1))] for v in sampled
    )


# ═══════════════════════════════════════════
# FORMATAGE MONÉTAIRE ET PNL
# ═══════════════════════════════════════════

def fmt_usd(amount: float, decimals: int = 2) -> str:
    """Format USDC amount: $1,234.56"""
    if abs(amount) >= 1_000_000:
        return f"${amount/1_000_000:,.1f}M"
    if abs(amount) >= 10_000:
        return f"${amount/1_000:,.1f}K"
    return f"${amount:,.{decimals}f}"


def fmt_pnl(pnl_usdc: float = 0, pnl_pct: float = 0, show_both: bool = True) -> str:
    """Format PNL avec emoji trend.

    Returns:
        "📈 +$45.20 (+2.3%)" or "📉 -$12.50 (-1.8%)"
    """
    emoji = "📈" if pnl_usdc >= 0 else "📉"
    sign = "+" if pnl_usdc >= 0 else ""

    if show_both and pnl_pct != 0:
        return f"{emoji} {sign}{fmt_usd(pnl_usdc)} ({sign}{pnl_pct:.1f}%)"
    elif pnl_pct != 0:
        return f"{emoji} {sign}{pnl_pct:.1f}%"
    else:
        return f"{emoji} {sign}{fmt_usd(pnl_usdc)}"


def fmt_pnl_compact(pnl_pct: float) -> str:
    """PNL compact avec couleur: "+2.3%" ou "-1.8%"."""
    if pnl_pct >= 0:
        return f"+{pnl_pct:.1f}%"
    return f"{pnl_pct:.1f}%"


# ═══════════════════════════════════════════
# BADGES DE STATUT
# ═══════════════════════════════════════════

def badge_trader_status(win_rate: float, trade_count: int) -> str:
    """Badge de statut trader."""
    if trade_count < 5:
        return "⬜ Nouveau"
    if win_rate >= 65:
        return "🔥 Hot"
    if win_rate >= 50:
        return "✅ Stable"
    if win_rate >= 40:
        return "⚠️ Moyen"
    return "🥶 Froid"


def badge_score(score: float) -> str:
    """Badge de score signal."""
    if score >= 75:
        return "🟢 EXCELLENT"
    if score >= 50:
        return "🟡 BON"
    if score >= 30:
        return "🟠 FAIBLE"
    return "🔴 IGNORÉ"


def badge_position_status(pnl_pct: float) -> str:
    """Badge de statut position."""
    if pnl_pct >= 10:
        return "🚀"
    if pnl_pct >= 5:
        return "🟢"
    if pnl_pct >= 0:
        return "🔵"
    if pnl_pct >= -5:
        return "🟡"
    if pnl_pct >= -10:
        return "🟠"
    return "🔴"


def badge_gas_status(pol_balance: float) -> str:
    """Badge de statut gas POL."""
    if pol_balance >= 1.0:
        return "✅ OK"
    if pol_balance >= 0.1:
        return "⚠️ Faible"
    return "🔴 Insuffisant"


# ═══════════════════════════════════════════
# FORMATAGE WALLET / ADRESSES
# ═══════════════════════════════════════════

def short_addr(address: str, chars: int = 6) -> str:
    """Raccourcir une adresse: 0xab12...ef89"""
    if not address or len(address) < chars * 2 + 3:
        return address or "?"
    return f"{address[:chars]}...{address[-4:]}"


def short_wallet(address: str) -> str:
    """Alias pour short_addr avec 6 chars."""
    return short_addr(address, 6)


# ═══════════════════════════════════════════
# FORMATAGE TEMPOREL
# ═══════════════════════════════════════════

def time_ago(dt: datetime) -> str:
    """Format relatif: "il y a 3h", "il y a 2j"."""
    if not dt:
        return "?"
    now = datetime.utcnow()
    delta = now - dt

    if delta.total_seconds() < 60:
        return "à l'instant"
    if delta.total_seconds() < 3600:
        return f"il y a {int(delta.total_seconds() / 60)}min"
    if delta.total_seconds() < 86400:
        return f"il y a {int(delta.total_seconds() / 3600)}h"
    return f"il y a {delta.days}j"


def time_remaining(dt: datetime) -> str:
    """Temps restant: "dans 3h", "dans 2j", "expiré"."""
    if not dt:
        return "?"
    now = datetime.utcnow()
    delta = dt - now

    if delta.total_seconds() <= 0:
        return "expiré"
    if delta.total_seconds() < 3600:
        return f"{int(delta.total_seconds() / 60)}min"
    if delta.total_seconds() < 86400:
        return f"{int(delta.total_seconds() / 3600)}h"
    return f"{delta.days}j"


def fmt_duration(seconds: float) -> str:
    """Format durée: "2.3s", "45s", "12min", "3h"."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}min"
    return f"{seconds / 3600:.1f}h"


# ═══════════════════════════════════════════
# HEADERS ET SECTIONS
# ═══════════════════════════════════════════

def header(title: str, emoji: str = "") -> str:
    """Header de section standardisé."""
    if emoji:
        return f"{emoji} **{title}**\n{SEP}"
    return f"**{title}**\n{SEP}"


def section(title: str) -> str:
    """Sous-section avec séparateur léger."""
    return f"\n*{title}*\n{SEP_LIGHT}"


def card_header(title: str, subtitle: str = "", badge: str = "") -> str:
    """Header de carte trader/position."""
    parts = [f"**{title}**"]
    if badge:
        parts.append(badge)
    if subtitle:
        parts.append(f"| _{subtitle}_")
    return " ".join(parts)


# ═══════════════════════════════════════════
# WIN RATE FORMATTING
# ═══════════════════════════════════════════

def fmt_winrate(wins: int, total: int, show_bar: bool = True) -> str:
    """Format win rate avec barre visuelle.

    Returns:
        "62% (15t) ██████░░░░" or "62% (15 trades)"
    """
    if total == 0:
        return "N/A"
    wr = wins / total * 100
    text = f"{wr:.0f}% ({total}t)"
    if show_bar:
        text += f" {bar(wr, 100, 10)}"
    return text


def fmt_streak(streak: int) -> str:
    """Format streak: "+3W" or "-2L"."""
    if streak > 0:
        return f"+{streak}W"
    if streak < 0:
        return f"{streak}L"
    return "0"
