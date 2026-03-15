"""PDF report generation — detailed paper/live trading performance report."""

import io
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
)

logger = logging.getLogger(__name__)


@dataclass
class TimeframeStats:
    """Performance stats for a specific timeframe."""
    label: str
    trades_count: int
    buys: int
    sells: int
    volume_usdc: float
    realized_pnl: float
    wins: int
    losses: int
    max_capital_deployed: float  # peak capital in open positions


@dataclass
class PositionSnapshot:
    """Current open position."""
    market_question: str
    side: str
    entry_price: float
    current_price: float
    invested: float
    current_value: float
    pnl_usdc: float
    pnl_pct: float
    shares: float
    opened_at: Optional[datetime] = None
    is_paper: bool = False


@dataclass
class ReportData:
    """All data needed to generate a report."""
    username: str
    wallet_short: str
    is_paper: bool
    generated_at: datetime

    # Balances
    paper_balance: float
    paper_initial: float
    portfolio_value: float
    total_pnl: float
    total_pnl_pct: float

    # Timeframe stats
    stats_1h: TimeframeStats
    stats_5h: TimeframeStats
    stats_24h: TimeframeStats
    stats_7d: TimeframeStats
    stats_all: TimeframeStats

    # Positions
    open_positions: list[PositionSnapshot]
    settled_trades_count: int
    settled_pnl: float
    overall_win_rate: float


def _color_for_pnl(val: float) -> colors.Color:
    """Green for profit, red for loss."""
    return colors.HexColor("#22c55e") if val >= 0 else colors.HexColor("#ef4444")


def _pnl_str(val: float) -> str:
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}"


def generate_report_pdf(data: ReportData) -> io.BytesIO:
    """Generate a complete PDF report and return as BytesIO buffer."""

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontSize=20,
        spaceAfter=4,
        textColor=colors.HexColor("#1e293b"),
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#64748b"),
        spaceAfter=12,
    )
    section_style = ParagraphStyle(
        "SectionTitle",
        parent=styles["Heading2"],
        fontSize=13,
        spaceBefore=16,
        spaceAfter=6,
        textColor=colors.HexColor("#1e293b"),
        borderWidth=0,
        borderPadding=0,
    )
    body_style = ParagraphStyle(
        "BodyText",
        parent=styles["Normal"],
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#334155"),
    )
    small_style = ParagraphStyle(
        "SmallText",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#94a3b8"),
    )

    elements = []

    # ═══════════════════════════════════════════
    # HEADER
    # ═══════════════════════════════════════════
    mode_label = "PAPER TRADING" if data.is_paper else "LIVE TRADING"
    elements.append(Paragraph("WENPOLYMARKET", title_style))
    elements.append(Paragraph(
        f"Rapport de performance — {mode_label}<br/>"
        f"Utilisateur : {data.username} | Wallet : {data.wallet_short}<br/>"
        f"Généré le {data.generated_at.strftime('%d/%m/%Y à %H:%M UTC')}",
        subtitle_style,
    ))
    elements.append(HRFlowable(
        width="100%", thickness=1,
        color=colors.HexColor("#e2e8f0"), spaceAfter=8,
    ))

    # ═══════════════════════════════════════════
    # PORTFOLIO SUMMARY
    # ═══════════════════════════════════════════
    elements.append(Paragraph("PORTEFEUILLE", section_style))

    pnl_color = "#22c55e" if data.total_pnl >= 0 else "#ef4444"
    summary_data = [
        ["Capital initial", f"{data.paper_initial:.2f} USDC"],
        ["Cash disponible", f"{data.paper_balance:.2f} USDC"],
        [
            "Portefeuille total",
            f"<b>{data.portfolio_value:.2f} USDC</b>",
        ],
        [
            "PNL total",
            f'<font color="{pnl_color}"><b>'
            f"{_pnl_str(data.total_pnl)} USDC "
            f"({_pnl_str(data.total_pnl_pct)}%)</b></font>",
        ],
        [
            "Win rate global",
            f"{data.overall_win_rate:.0f}%"
            if data.overall_win_rate >= 0
            else "N/A",
        ],
    ]

    summary_table = Table(
        [[Paragraph(r[0], body_style), Paragraph(r[1], body_style)]
         for r in summary_data],
        colWidths=[120, 200],
    )
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f8fafc")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(summary_table)

    # ═══════════════════════════════════════════
    # PERFORMANCE BY TIMEFRAME
    # ═══════════════════════════════════════════
    elements.append(Paragraph("PERFORMANCE PAR PERIODE", section_style))

    tf_header = [
        "Période", "Trades", "Buy/Sell", "Volume",
        "PNL réalisé", "W/L", "Win Rate", "Capital max",
    ]
    tf_rows = [tf_header]
    for s in [data.stats_1h, data.stats_5h, data.stats_24h, data.stats_7d, data.stats_all]:
        wr = f"{(s.wins / (s.wins + s.losses) * 100):.0f}%" if (s.wins + s.losses) > 0 else "—"
        tf_rows.append([
            s.label,
            str(s.trades_count),
            f"{s.buys}B / {s.sells}S",
            f"{s.volume_usdc:.2f}",
            _pnl_str(s.realized_pnl),
            f"{s.wins}W / {s.losses}L",
            wr,
            f"{s.max_capital_deployed:.2f}",
        ])

    tf_table = Table(
        [[Paragraph(str(c), body_style if i > 0 else ParagraphStyle(
            "th", parent=body_style, fontSize=8, textColor=colors.white,
        )) for c in row] for i, row in enumerate(tf_rows)],
        colWidths=[55, 38, 52, 55, 62, 52, 42, 60],
        repeatRows=1,
    )
    tf_table.setStyle(TableStyle([
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        # Body
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#ffffff")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
            colors.HexColor("#ffffff"), colors.HexColor("#f8fafc"),
        ]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]))
    elements.append(tf_table)

    # ═══════════════════════════════════════════
    # OPEN POSITIONS
    # ═══════════════════════════════════════════
    elements.append(Paragraph(
        f"POSITIONS OUVERTES ({len(data.open_positions)})", section_style
    ))

    if data.open_positions:
        pos_header = [
            "Marché", "Entry", "Now", "Investi",
            "Valeur", "PNL", "%", "Shares",
        ]
        pos_rows = [pos_header]
        total_inv = 0.0
        total_val = 0.0

        for p in data.open_positions:
            total_inv += p.invested
            total_val += p.current_value
            q = p.market_question
            if len(q) > 30:
                q = q[:27] + "..."

            pnl_c = "#22c55e" if p.pnl_usdc >= 0 else "#ef4444"
            pos_rows.append([
                q,
                f"{p.entry_price:.2f}",
                f"{p.current_price:.2f}",
                f"{p.invested:.2f}",
                f"{p.current_value:.2f}",
                _pnl_str(p.pnl_usdc),
                f"{_pnl_str(p.pnl_pct)}%",
                f"{p.shares:.1f}",
            ])

        # Total row
        t_pnl = total_val - total_inv
        pos_rows.append([
            "TOTAL", "", "",
            f"{total_inv:.2f}",
            f"{total_val:.2f}",
            _pnl_str(t_pnl),
            f"{_pnl_str((t_pnl / total_inv * 100) if total_inv > 0 else 0)}%",
            "",
        ])

        pos_table = Table(
            [[Paragraph(str(c), body_style if i > 0 else ParagraphStyle(
                "th2", parent=body_style, fontSize=8, textColor=colors.white,
            )) for c in row] for i, row in enumerate(pos_rows)],
            colWidths=[95, 35, 35, 45, 45, 45, 40, 38],
            repeatRows=1,
        )
        pos_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f1f5f9")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [
                colors.HexColor("#ffffff"), colors.HexColor("#f8fafc"),
            ]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
        ]))
        elements.append(pos_table)
    else:
        elements.append(Paragraph("Aucune position ouverte.", body_style))

    # ═══════════════════════════════════════════
    # FOOTER
    # ═══════════════════════════════════════════
    elements.append(Spacer(1, 20))
    elements.append(HRFlowable(
        width="100%", thickness=0.5,
        color=colors.HexColor("#e2e8f0"), spaceAfter=6,
    ))
    elements.append(Paragraph(
        f"WENPOLYMARKET — Rapport généré automatiquement le "
        f"{data.generated_at.strftime('%d/%m/%Y %H:%M UTC')}. "
        f"Les performances passées ne garantissent pas les résultats futurs.",
        small_style,
    ))

    doc.build(elements)
    buffer.seek(0)
    return buffer


async def build_report_data(
    user,
    user_settings,
    trades: list,
    current_prices: dict[str, float],
) -> ReportData:
    """Build ReportData from user, settings, and trades.

    Args:
        user: User model instance
        user_settings: UserSettings instance
        trades: List of Trade objects (all FILLED trades for user)
        current_prices: Dict mapping token_id -> current price
    """
    from bot.models.trade import TradeSide

    now = datetime.now(timezone.utc)
    wallet_short = (
        f"{user.wallet_address[:6]}...{user.wallet_address[-4:]}"
        if user.wallet_address else "N/A"
    )

    # ── Build position snapshots ──
    open_positions = []
    open_buys = [
        t for t in trades
        if t.side == TradeSide.BUY and not t.is_settled
    ]

    for t in open_buys:
        invested = t.net_amount_usdc
        shares = t.shares or (invested / t.price if t.price > 0 else 0)
        cur_price = current_prices.get(t.token_id, 0)
        current_val = shares * cur_price if cur_price > 0 else invested
        pnl = current_val - invested
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0

        open_positions.append(PositionSnapshot(
            market_question=t.market_question or t.market_id or "?",
            side=t.side.value,
            entry_price=t.price,
            current_price=cur_price,
            invested=invested,
            current_value=current_val,
            pnl_usdc=pnl,
            pnl_pct=pnl_pct,
            shares=shares,
            opened_at=t.created_at,
            is_paper=t.is_paper,
        ))

    # ── Timeframe stats ──
    timeframes = [
        ("1h", timedelta(hours=1)),
        ("5h", timedelta(hours=5)),
        ("24h", timedelta(hours=24)),
        ("7j", timedelta(days=7)),
        ("Tout", timedelta(days=36500)),  # ~100 years = all time
    ]

    tf_stats = []
    for label, delta in timeframes:
        cutoff = now - delta
        tf_trades = [t for t in trades if t.created_at and t.created_at >= cutoff]

        buys = [t for t in tf_trades if t.side == TradeSide.BUY]
        sells = [t for t in tf_trades if t.side == TradeSide.SELL]
        volume = sum(t.gross_amount_usdc for t in tf_trades)

        # Realized PNL from sells
        buy_prices: dict[str, list[float]] = {}
        for t in tf_trades:
            if t.side == TradeSide.BUY:
                buy_prices.setdefault(t.token_id, []).append(t.price)

        realized_pnl = 0.0
        wins = 0
        losses = 0
        for t in sells:
            if t.token_id in buy_prices and buy_prices[t.token_id]:
                avg_buy = sum(buy_prices[t.token_id]) / len(buy_prices[t.token_id])
                pnl = (t.price - avg_buy) * t.shares
                realized_pnl += pnl
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1

        # Also count settled trades PNL
        for t in tf_trades:
            if t.is_settled and t.settlement_pnl is not None:
                realized_pnl += t.settlement_pnl
                if t.settlement_pnl > 0:
                    wins += 1
                elif t.settlement_pnl < 0:
                    losses += 1

        # Max capital deployed = peak concurrent invested amount
        # Simple approach: sum of all open BUY amounts at any point in timeframe
        max_capital = _compute_max_capital(tf_trades, cutoff, now)

        tf_stats.append(TimeframeStats(
            label=label,
            trades_count=len(tf_trades),
            buys=len(buys),
            sells=len(sells),
            volume_usdc=volume,
            realized_pnl=realized_pnl,
            wins=wins,
            losses=losses,
            max_capital_deployed=max_capital,
        ))

    # ── Portfolio totals ──
    total_current_value = sum(p.current_value for p in open_positions)
    portfolio_value = user.paper_balance + total_current_value
    total_pnl = portfolio_value - user.paper_initial_balance
    total_pnl_pct = (
        (total_pnl / user.paper_initial_balance * 100)
        if user.paper_initial_balance > 0 else 0
    )

    # Overall win rate from settled + sells
    all_wins = tf_stats[-1].wins  # "All time" stats
    all_losses = tf_stats[-1].losses
    overall_wr = (
        (all_wins / (all_wins + all_losses) * 100)
        if (all_wins + all_losses) > 0 else -1
    )

    settled_trades = [t for t in trades if t.is_settled]
    settled_pnl = sum(t.settlement_pnl or 0 for t in settled_trades)

    return ReportData(
        username=user.telegram_username or f"User {user.telegram_id}",
        wallet_short=wallet_short,
        is_paper=user.paper_trading,
        generated_at=now,
        paper_balance=user.paper_balance,
        paper_initial=user.paper_initial_balance,
        portfolio_value=portfolio_value,
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        stats_1h=tf_stats[0],
        stats_5h=tf_stats[1],
        stats_24h=tf_stats[2],
        stats_7d=tf_stats[3],
        stats_all=tf_stats[4],
        open_positions=open_positions,
        settled_trades_count=len(settled_trades),
        settled_pnl=settled_pnl,
        overall_win_rate=overall_wr,
    )


def _compute_max_capital(trades: list, start: datetime, end: datetime) -> float:
    """Compute the peak capital deployed in open positions during a timeframe.

    Simulates the timeline: each BUY adds invested capital, each SELL removes it.
    Track the maximum at any point.
    """
    from bot.models.trade import TradeSide

    events: list[tuple[datetime, float]] = []
    for t in trades:
        if not t.created_at:
            continue
        if t.side == TradeSide.BUY:
            events.append((t.created_at, +t.net_amount_usdc))
        elif t.side == TradeSide.SELL:
            events.append((t.created_at, -t.net_amount_usdc))

    events.sort(key=lambda e: e[0])

    current_capital = 0.0
    max_capital = 0.0
    for _, amount in events:
        current_capital += amount
        if current_capital < 0:
            current_capital = 0  # Can't go negative
        if current_capital > max_capital:
            max_capital = current_capital

    return max_capital
