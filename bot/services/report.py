"""PDF report generation — two report types:

1. **Trader Report (Dashboard)**: Real performance of followed traders on Polymarket
   - Per-trader breakdown with 1h, 24h, 7j stats
   - Open positions = unrealized PNL
   - Resolved positions within timeframe = realized PNL
   - Global totals

2. **Recap Report (Our Trades)**: What the bot copied for us (paper or live)
   - Same timeframe structure (1h, 24h, 7j)
   - Our own trades with settlement PNL
"""

import io
import logging
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    PageBreak,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════

@dataclass
class TimeframeStats:
    """Performance stats for a specific timeframe."""
    label: str
    trades_count: int = 0
    buys: int = 0
    sells: int = 0
    volume_usdc: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    invested: float = 0.0
    current_value: float = 0.0


@dataclass
class PositionSnapshot:
    """A single position (open or resolved)."""
    title: str
    outcome: str
    side: str = "BUY"
    entry_price: float = 0.0
    current_price: float = 0.0
    invested: float = 0.0
    current_value: float = 0.0
    pnl_usdc: float = 0.0
    pnl_pct: float = 0.0
    shares: float = 0.0
    redeemable: bool = False
    is_paper: bool = False


@dataclass
class TraderSection:
    """Data for one followed trader."""
    wallet: str
    wallet_short: str
    stats_1h: TimeframeStats = field(default_factory=lambda: TimeframeStats("1h"))
    stats_24h: TimeframeStats = field(default_factory=lambda: TimeframeStats("24h"))
    stats_7d: TimeframeStats = field(default_factory=lambda: TimeframeStats("7j"))
    open_positions: list[PositionSnapshot] = field(default_factory=list)
    resolved_positions: list[PositionSnapshot] = field(default_factory=list)
    total_unrealized: float = 0.0
    total_realized: float = 0.0
    total_invested: float = 0.0
    total_current: float = 0.0


@dataclass
class TraderReportData:
    """Full data for the Dashboard (trader performance) PDF."""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    username: str = ""
    traders: list[TraderSection] = field(default_factory=list)
    # Global totals
    grand_unrealized: float = 0.0
    grand_realized: float = 0.0
    grand_invested: float = 0.0
    grand_current: float = 0.0
    total_open_positions: int = 0


@dataclass
class RecapReportData:
    """Full data for the Recap (our copied trades) PDF."""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    username: str = ""
    wallet_short: str = ""
    is_paper: bool = True
    paper_balance: float = 0.0
    paper_initial: float = 1000.0
    portfolio_value: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    stats_1h: TimeframeStats = field(default_factory=lambda: TimeframeStats("1h"))
    stats_24h: TimeframeStats = field(default_factory=lambda: TimeframeStats("24h"))
    stats_7d: TimeframeStats = field(default_factory=lambda: TimeframeStats("7j"))
    stats_all: TimeframeStats = field(default_factory=lambda: TimeframeStats("Tout"))
    open_positions: list[PositionSnapshot] = field(default_factory=list)
    settled_trades_count: int = 0
    settled_pnl: float = 0.0
    overall_win_rate: float = -1


# ═══════════════════════════════════════════════════════════════
# PDF styling helpers
# ═══════════════════════════════════════════════════════════════

def _pnl_str(val: float) -> str:
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}"


def _pnl_color(val: float) -> str:
    return "#22c55e" if val >= 0 else "#ef4444"


def _get_styles():
    """Return all custom styles for the PDF."""
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"],
        fontSize=20, spaceAfter=4,
        textColor=colors.HexColor("#1e293b"),
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle", parent=styles["Normal"],
        fontSize=10, textColor=colors.HexColor("#64748b"), spaceAfter=12,
    )
    section_style = ParagraphStyle(
        "SectionTitle", parent=styles["Heading2"],
        fontSize=13, spaceBefore=16, spaceAfter=6,
        textColor=colors.HexColor("#1e293b"),
    )
    subsection_style = ParagraphStyle(
        "SubSection", parent=styles["Heading3"],
        fontSize=11, spaceBefore=10, spaceAfter=4,
        textColor=colors.HexColor("#334155"),
    )
    body_style = ParagraphStyle(
        "BodyText2", parent=styles["Normal"],
        fontSize=9, leading=13,
        textColor=colors.HexColor("#334155"),
    )
    small_style = ParagraphStyle(
        "SmallText", parent=styles["Normal"],
        fontSize=8, textColor=colors.HexColor("#94a3b8"),
    )
    header_cell = ParagraphStyle(
        "HeaderCell", parent=body_style,
        fontSize=8, textColor=colors.white,
    )

    return {
        "title": title_style,
        "subtitle": subtitle_style,
        "section": section_style,
        "subsection": subsection_style,
        "body": body_style,
        "small": small_style,
        "header_cell": header_cell,
    }


_TABLE_STYLE = TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
        colors.HexColor("#ffffff"), colors.HexColor("#f8fafc"),
    ]),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
])


def _hr():
    return HRFlowable(
        width="100%", thickness=1,
        color=colors.HexColor("#e2e8f0"), spaceAfter=8,
    )


def _timeframe_table(stats_list: list[TimeframeStats], s) -> Table:
    """Build a timeframe performance table."""
    header = ["Periode", "Trades", "B/S", "Volume", "PNL non-realise", "PNL realise", "W/L"]
    rows = [
        [Paragraph(h, s["header_cell"]) for h in header]
    ]
    for st in stats_list:
        wr = f"{st.wins}W/{st.losses}L" if (st.wins + st.losses) > 0 else "-"
        c_unr = _pnl_color(st.unrealized_pnl)
        c_rea = _pnl_color(st.realized_pnl)
        rows.append([
            Paragraph(st.label, s["body"]),
            Paragraph(str(st.trades_count), s["body"]),
            Paragraph(f"{st.buys}B/{st.sells}S", s["body"]),
            Paragraph(f"{st.volume_usdc:.0f}$", s["body"]),
            Paragraph(f'<font color="{c_unr}">{_pnl_str(st.unrealized_pnl)}$</font>', s["body"]),
            Paragraph(f'<font color="{c_rea}">{_pnl_str(st.realized_pnl)}$</font>', s["body"]),
            Paragraph(wr, s["body"]),
        ])

    table = Table(rows, colWidths=[40, 40, 42, 50, 75, 70, 48], repeatRows=1)
    table.setStyle(_TABLE_STYLE)
    return table


def _positions_table(positions: list[PositionSnapshot], s, max_rows: int = 15) -> Table:
    """Build a positions table."""
    header = ["Marche", "Side", "Entry", "Now", "Investi", "Valeur", "PNL", "%"]
    rows = [
        [Paragraph(h, s["header_cell"]) for h in header]
    ]

    total_inv = 0.0
    total_val = 0.0

    for p in positions[:max_rows]:
        total_inv += p.invested
        total_val += p.current_value
        title = p.title[:22] + ".." if len(p.title) > 22 else p.title
        c = _pnl_color(p.pnl_usdc)
        rows.append([
            Paragraph(f"{title} ({p.outcome})", s["body"]),
            Paragraph(p.side, s["body"]),
            Paragraph(f"{p.entry_price:.2f}", s["body"]),
            Paragraph(f"{p.current_price:.2f}", s["body"]),
            Paragraph(f"{p.invested:.1f}$", s["body"]),
            Paragraph(f"{p.current_value:.1f}$", s["body"]),
            Paragraph(f'<font color="{c}">{_pnl_str(p.pnl_usdc)}$</font>', s["body"]),
            Paragraph(f'<font color="{c}">{_pnl_str(p.pnl_pct)}%</font>', s["body"]),
        ])

    if len(positions) > max_rows:
        remaining = positions[max_rows:]
        r_inv = sum(p.invested for p in remaining)
        r_val = sum(p.current_value for p in remaining)
        r_pnl = r_val - r_inv
        total_inv += r_inv
        total_val += r_val
        rows.append([
            Paragraph(f"<i>+{len(remaining)} autres</i>", s["body"]),
            Paragraph("", s["body"]),
            Paragraph("", s["body"]),
            Paragraph("", s["body"]),
            Paragraph(f"{r_inv:.1f}$", s["body"]),
            Paragraph(f"{r_val:.1f}$", s["body"]),
            Paragraph(f"{_pnl_str(r_pnl)}$", s["body"]),
            Paragraph("", s["body"]),
        ])

    # Total row
    t_pnl = total_val - total_inv
    t_pct = (t_pnl / total_inv * 100) if total_inv > 0 else 0
    c = _pnl_color(t_pnl)
    rows.append([
        Paragraph("<b>TOTAL</b>", s["body"]),
        Paragraph("", s["body"]),
        Paragraph("", s["body"]),
        Paragraph("", s["body"]),
        Paragraph(f"<b>{total_inv:.1f}$</b>", s["body"]),
        Paragraph(f"<b>{total_val:.1f}$</b>", s["body"]),
        Paragraph(f'<b><font color="{c}">{_pnl_str(t_pnl)}$</font></b>', s["body"]),
        Paragraph(f'<b><font color="{c}">{_pnl_str(t_pct)}%</font></b>', s["body"]),
    ])

    table = Table(rows, colWidths=[85, 30, 35, 35, 42, 42, 48, 42], repeatRows=1)
    style = TableStyle([
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
    ])
    table.setStyle(style)
    return table


def _footer(elements, generated_at: datetime, s):
    """Add footer to PDF."""
    elements.append(Spacer(1, 20))
    elements.append(HRFlowable(
        width="100%", thickness=0.5,
        color=colors.HexColor("#e2e8f0"), spaceAfter=6,
    ))
    elements.append(Paragraph(
        f"WENPOLYMARKET -- Rapport genere automatiquement le "
        f"{generated_at.strftime('%d/%m/%Y %H:%M UTC')}. "
        f"Les performances passees ne garantissent pas les resultats futurs.",
        s["small"],
    ))


# ═══════════════════════════════════════════════════════════════
# 1) TRADER REPORT (Dashboard) — PDF generation
# ═══════════════════════════════════════════════════════════════

def generate_trader_report_pdf(data: TraderReportData) -> io.BytesIO:
    """Generate a PDF report of followed traders' performance."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
    )
    s = _get_styles()
    elements = []

    # ── Header ──
    elements.append(Paragraph("WENPOLYMARKET", s["title"]))
    elements.append(Paragraph(
        f"Rapport Dashboard -- Performance des Traders Suivis<br/>"
        f"Utilisateur : {data.username}<br/>"
        f"Genere le {data.generated_at.strftime('%d/%m/%Y a %H:%M UTC')}",
        s["subtitle"],
    ))
    elements.append(_hr())

    # ── Global Summary ──
    elements.append(Paragraph("RESUME GLOBAL", s["section"]))

    total_pnl = data.grand_unrealized + data.grand_realized
    c = _pnl_color(total_pnl)
    pct = (data.grand_unrealized / data.grand_invested * 100) if data.grand_invested > 0 else 0

    summary = [
        ["Traders suivis", str(len(data.traders))],
        ["Positions ouvertes", str(data.total_open_positions)],
        ["Capital investi (ouvert)", f"{data.grand_invested:.2f} USDC"],
        ["Valeur actuelle", f"{data.grand_current:.2f} USDC"],
        [
            "PNL non-realise (ouvert)",
            f'<font color="{_pnl_color(data.grand_unrealized)}">'
            f"<b>{_pnl_str(data.grand_unrealized)} USDC ({_pnl_str(pct)}%)</b></font>",
        ],
        [
            "PNL realise (resolus)",
            f'<font color="{_pnl_color(data.grand_realized)}">'
            f"<b>{_pnl_str(data.grand_realized)} USDC</b></font>",
        ],
        [
            "PNL TOTAL",
            f'<font color="{c}"><b>{_pnl_str(total_pnl)} USDC</b></font>',
        ],
    ]
    summary_table = Table(
        [[Paragraph(r[0], s["body"]), Paragraph(r[1], s["body"])] for r in summary],
        colWidths=[140, 220],
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

    # ── Per-Trader Sections ──
    for i, trader in enumerate(data.traders):
        if i > 0:
            elements.append(Spacer(1, 8))
        elements.append(_hr())
        elements.append(Paragraph(
            f"TRADER : {trader.wallet_short}", s["section"]
        ))

        # Trader summary line
        t_total = trader.total_unrealized + trader.total_realized
        c_t = _pnl_color(t_total)
        elements.append(Paragraph(
            f'Positions ouvertes : {len(trader.open_positions)} | '
            f'Resolues : {len(trader.resolved_positions)} | '
            f'Investi : {trader.total_invested:.0f}$ | '
            f'PNL : <font color="{c_t}"><b>{_pnl_str(t_total)}$</b></font>',
            s["body"],
        ))
        elements.append(Spacer(1, 6))

        # Timeframe stats
        elements.append(Paragraph("Performance par periode", s["subsection"]))
        elements.append(_timeframe_table(
            [trader.stats_1h, trader.stats_24h, trader.stats_7d], s
        ))

        # Open positions
        if trader.open_positions:
            elements.append(Paragraph(
                f"Positions ouvertes ({len(trader.open_positions)}) "
                f"-- PNL non-realise", s["subsection"]
            ))
            elements.append(_positions_table(trader.open_positions, s, max_rows=10))

        # Resolved positions
        if trader.resolved_positions:
            elements.append(Paragraph(
                f"Positions resolues ({len(trader.resolved_positions)}) "
                f"-- PNL realise", s["subsection"]
            ))
            elements.append(_positions_table(trader.resolved_positions, s, max_rows=10))

    # ── Footer ──
    _footer(elements, data.generated_at, s)

    doc.build(elements)
    buffer.seek(0)
    return buffer


async def build_trader_report_data(username: str, followed_wallets: list[str]) -> TraderReportData:
    """Build TraderReportData by fetching real data from Polymarket API.

    For each trader:
    - Positions API -> open + resolved positions with PNL
    - Activity API -> trades per timeframe (1h, 24h, 7j)
    """
    from bot.services.polymarket import polymarket_client

    now = datetime.now(timezone.utc)
    now_ts = int(time.time())

    report = TraderReportData(
        generated_at=now,
        username=username,
    )

    for wallet in followed_wallets:
        w_short = f"{wallet[:6]}...{wallet[-4:]}"
        trader = TraderSection(wallet=wallet, wallet_short=w_short)

        # Fetch positions + activity in parallel
        positions = await polymarket_client.get_positions_by_address(wallet)

        # Separate open vs resolved
        open_pos = [p for p in positions if not p.redeemable]
        settled_pos = [p for p in positions if p.redeemable]

        # Build position snapshots
        for p in open_pos:
            pnl = p.cash_pnl
            pnl_pct = p.pnl_pct
            trader.open_positions.append(PositionSnapshot(
                title=p.title,
                outcome=p.outcome,
                side="BUY",
                entry_price=p.avg_price,
                current_price=p.current_price,
                invested=p.initial_value,
                current_value=p.current_value,
                pnl_usdc=pnl,
                pnl_pct=pnl_pct,
                shares=p.size,
                redeemable=False,
            ))

        for p in settled_pos:
            pnl = p.realized_pnl if p.realized_pnl != 0 else p.cash_pnl
            pnl_pct = p.percent_realized_pnl if p.percent_realized_pnl != 0 else p.pnl_pct
            trader.resolved_positions.append(PositionSnapshot(
                title=p.title,
                outcome=p.outcome,
                side="BUY",
                entry_price=p.avg_price,
                current_price=p.current_price,
                invested=p.initial_value,
                current_value=p.current_value,
                pnl_usdc=pnl,
                pnl_pct=pnl_pct,
                shares=p.size,
                redeemable=True,
            ))

        # Sort by absolute PNL
        trader.open_positions.sort(key=lambda p: abs(p.pnl_usdc), reverse=True)
        trader.resolved_positions.sort(key=lambda p: abs(p.pnl_usdc), reverse=True)

        # Totals for this trader
        trader.total_unrealized = sum(p.cash_pnl for p in open_pos)
        trader.total_realized = sum(p.realized_pnl for p in settled_pos)
        trader.total_invested = sum(p.initial_value for p in open_pos)
        trader.total_current = sum(p.current_value for p in open_pos)

        # Activity per timeframe
        # Fetch activity for the last 7 days
        activity_7d = await polymarket_client.get_activity_by_address(
            wallet, limit=500, start=now_ts - (7 * 86400)
        )

        for tf_stats, secs in [
            (trader.stats_1h, 3600),
            (trader.stats_24h, 86400),
            (trader.stats_7d, 7 * 86400),
        ]:
            cutoff = now_ts - secs
            tf_acts = [a for a in activity_7d if a.timestamp >= cutoff]
            tf_stats.trades_count = len(tf_acts)
            tf_stats.buys = sum(1 for a in tf_acts if a.side == "BUY")
            tf_stats.sells = sum(1 for a in tf_acts if a.side == "SELL")
            tf_stats.volume_usdc = sum(a.usdc_size for a in tf_acts)

            # Unrealized PNL = from open positions (same for all timeframes)
            tf_stats.unrealized_pnl = trader.total_unrealized
            tf_stats.invested = trader.total_invested
            tf_stats.current_value = trader.total_current

            # Realized PNL = from resolved positions visible in API
            tf_stats.realized_pnl = trader.total_realized

        report.traders.append(trader)

        # Accumulate global
        report.grand_unrealized += trader.total_unrealized
        report.grand_realized += trader.total_realized
        report.grand_invested += trader.total_invested
        report.grand_current += trader.total_current
        report.total_open_positions += len(trader.open_positions)

    return report


# ═══════════════════════════════════════════════════════════════
# 2) RECAP REPORT (Our Trades) — PDF generation
# ═══════════════════════════════════════════════════════════════

def generate_recap_report_pdf(data: RecapReportData) -> io.BytesIO:
    """Generate a PDF report of our own copied trades."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
    )
    s = _get_styles()
    elements = []

    # ── Header ──
    mode_label = "PAPER TRADING" if data.is_paper else "LIVE TRADING"
    elements.append(Paragraph("WENPOLYMARKET", s["title"]))
    elements.append(Paragraph(
        f"Rapport Recap -- {mode_label}<br/>"
        f"Utilisateur : {data.username} | Wallet : {data.wallet_short}<br/>"
        f"Genere le {data.generated_at.strftime('%d/%m/%Y a %H:%M UTC')}",
        s["subtitle"],
    ))
    elements.append(_hr())

    # ── Portfolio Summary ──
    elements.append(Paragraph("PORTEFEUILLE", s["section"]))

    pnl_c = _pnl_color(data.total_pnl)
    summary = [
        ["Capital initial", f"{data.paper_initial:.2f} USDC"],
        ["Cash disponible", f"{data.paper_balance:.2f} USDC"],
        ["Portefeuille total", f"<b>{data.portfolio_value:.2f} USDC</b>"],
        [
            "PNL total",
            f'<font color="{pnl_c}"><b>'
            f"{_pnl_str(data.total_pnl)} USDC "
            f"({_pnl_str(data.total_pnl_pct)}%)</b></font>",
        ],
        [
            "Trades resolus",
            f"{data.settled_trades_count} trades | PNL : {_pnl_str(data.settled_pnl)} USDC",
        ],
        [
            "Win rate",
            f"{data.overall_win_rate:.0f}%" if data.overall_win_rate >= 0 else "N/A",
        ],
    ]

    summary_table = Table(
        [[Paragraph(r[0], s["body"]), Paragraph(r[1], s["body"])] for r in summary],
        colWidths=[130, 230],
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

    # ── Performance by Timeframe ──
    elements.append(Paragraph("PERFORMANCE PAR PERIODE", s["section"]))
    elements.append(_timeframe_table(
        [data.stats_1h, data.stats_24h, data.stats_7d, data.stats_all], s
    ))

    # ── Open Positions ──
    elements.append(Paragraph(
        f"POSITIONS ACTIVES ({len(data.open_positions)})", s["section"]
    ))

    if data.open_positions:
        elements.append(_positions_table(data.open_positions, s, max_rows=20))
    else:
        elements.append(Paragraph("Aucune position ouverte.", s["body"]))

    # ── Footer ──
    _footer(elements, data.generated_at, s)

    doc.build(elements)
    buffer.seek(0)
    return buffer


async def build_recap_report_data(
    user,
    user_settings,
    trades: list,
    current_prices: dict[str, float],
) -> RecapReportData:
    """Build RecapReportData from user, settings, and trades.

    Args:
        user: User model instance
        user_settings: UserSettings instance
        trades: List of Trade objects (all FILLED trades for user)
        current_prices: Dict mapping token_id -> current price
    """
    from bot.models.trade import TradeSide

    now = datetime.now(timezone.utc)

    def _aware(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

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
        current_val = shares * cur_price
        pnl = current_val - invested
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0

        if cur_price > 0:
            open_positions.append(PositionSnapshot(
                title=t.market_question or t.market_id or "?",
                outcome=t.outcome if hasattr(t, 'outcome') and t.outcome else "?",
                side=t.side.value,
                entry_price=t.price,
                current_price=cur_price,
                invested=invested,
                current_value=current_val,
                pnl_usdc=pnl,
                pnl_pct=pnl_pct,
                shares=shares,
                is_paper=t.is_paper,
            ))

    # ── Timeframe stats ──
    timeframes = [
        ("1h", timedelta(hours=1)),
        ("24h", timedelta(hours=24)),
        ("7j", timedelta(days=7)),
        ("Tout", timedelta(days=36500)),
    ]

    tf_stats_list = []
    for label, delta in timeframes:
        cutoff = now - delta
        tf_trades = [t for t in trades if t.created_at and _aware(t.created_at) >= cutoff]

        buys = [t for t in tf_trades if t.side == TradeSide.BUY]
        sells = [t for t in tf_trades if t.side == TradeSide.SELL]
        volume = sum(t.gross_amount_usdc for t in tf_trades)

        # Realized PNL from settled trades within this timeframe
        realized_pnl = 0.0
        wins = 0
        losses = 0
        for t in tf_trades:
            if t.is_settled and t.settlement_pnl is not None:
                realized_pnl += t.settlement_pnl
                if t.settlement_pnl > 0:
                    wins += 1
                elif t.settlement_pnl < 0:
                    losses += 1

        # Unrealized = open positions PNL
        unrealized = sum(p.pnl_usdc for p in open_positions)

        tf_stats_list.append(TimeframeStats(
            label=label,
            trades_count=len(tf_trades),
            buys=len(buys),
            sells=len(sells),
            volume_usdc=volume,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized,
            wins=wins,
            losses=losses,
        ))

    # ── Portfolio totals ──
    active_value = sum(p.current_value for p in open_positions)
    portfolio_value = user.paper_balance + active_value
    total_pnl = portfolio_value - user.paper_initial_balance
    total_pnl_pct = (
        (total_pnl / user.paper_initial_balance * 100)
        if user.paper_initial_balance > 0 else 0
    )

    # Win rate
    all_wins = tf_stats_list[-1].wins
    all_losses = tf_stats_list[-1].losses
    overall_wr = (
        (all_wins / (all_wins + all_losses) * 100)
        if (all_wins + all_losses) > 0 else -1
    )

    settled_trades = [t for t in trades if t.is_settled]
    settled_pnl = sum(t.settlement_pnl or 0 for t in settled_trades)

    return RecapReportData(
        generated_at=now,
        username=user.telegram_username or f"User {user.telegram_id}",
        wallet_short=wallet_short,
        is_paper=user.paper_trading,
        paper_balance=user.paper_balance,
        paper_initial=user.paper_initial_balance,
        portfolio_value=portfolio_value,
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        stats_1h=tf_stats_list[0],
        stats_24h=tf_stats_list[1],
        stats_7d=tf_stats_list[2],
        stats_all=tf_stats_list[3],
        open_positions=open_positions,
        settled_trades_count=len(settled_trades),
        settled_pnl=settled_pnl,
        overall_win_rate=overall_wr,
    )
