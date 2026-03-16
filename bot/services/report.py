"""PDF report generation — two report types:

1. **Trader Report (Dashboard)**: Real performance of followed traders on Polymarket
   - Per-trader breakdown with activity stats (1h, 24h, 7j)
   - Open positions = unrealized PNL (accurate from API)
   - Note: historical PNL not available via public API

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
    total_unrealized: float = 0.0
    total_invested: float = 0.0
    total_current: float = 0.0
    # Profile stats (from page scraping)
    username: str = ""
    pseudonym: str = ""
    pnl_total: float = 0.0
    pnl_1d: float = 0.0
    pnl_1w: float = 0.0
    pnl_1m: float = 0.0
    volume: float = 0.0
    markets_traded: int = 0
    has_profile: bool = False  # True if profile data was fetched


@dataclass
class TraderReportData:
    """Full data for the Dashboard (trader performance) PDF."""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    username: str = ""
    traders: list[TraderSection] = field(default_factory=list)
    # Global totals (open positions only — accurate)
    grand_unrealized: float = 0.0
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
    note_style = ParagraphStyle(
        "NoteText", parent=styles["Normal"],
        fontSize=8, textColor=colors.HexColor("#6b7280"),
        spaceAfter=6,
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
        "note": note_style,
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


def _activity_table(stats_list: list[TimeframeStats], s) -> Table:
    """Build an activity-only timeframe table (trades, volume, B/S)."""
    header = ["Periode", "Trades", "Buys", "Sells", "Volume"]
    rows = [
        [Paragraph(h, s["header_cell"]) for h in header]
    ]
    for st in stats_list:
        trades_str = f"{st.trades_count}+" if st.trades_count >= 500 else str(st.trades_count)
        rows.append([
            Paragraph(st.label, s["body"]),
            Paragraph(trades_str, s["body"]),
            Paragraph(str(st.buys), s["body"]),
            Paragraph(str(st.sells), s["body"]),
            Paragraph(f"{st.volume_usdc:.0f}$", s["body"]),
        ])

    table = Table(rows, colWidths=[50, 50, 50, 50, 65], repeatRows=1)
    table.setStyle(_TABLE_STYLE)
    return table


def _recap_timeframe_table(stats_list: list[TimeframeStats], s) -> Table:
    """Build a timeframe table for recap report (includes PNL columns)."""
    header = ["Periode", "Trades", "B/S", "Volume", "PNL non-realise", "PNL realise", "W/L"]
    rows = [
        [Paragraph(h, s["header_cell"]) for h in header]
    ]
    for st in stats_list:
        wr = f"{st.wins}W/{st.losses}L" if (st.wins + st.losses) > 0 else "-"
        c_unr = _pnl_color(st.unrealized_pnl)
        c_rea = _pnl_color(st.realized_pnl)
        trades_str = f"{st.trades_count}+" if st.trades_count >= 500 else str(st.trades_count)
        rows.append([
            Paragraph(st.label, s["body"]),
            Paragraph(trades_str, s["body"]),
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
    """Build a positions table. PNL uses pnl_usdc field (not recomputed)."""
    header = ["Marche", "Side", "Entry", "Now", "Investi", "Valeur", "PNL", "%"]
    rows = [
        [Paragraph(h, s["header_cell"]) for h in header]
    ]

    total_pnl = 0.0
    total_inv = 0.0
    total_val = 0.0

    for p in positions[:max_rows]:
        total_inv += p.invested
        total_val += p.current_value
        total_pnl += p.pnl_usdc
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
        r_pnl = sum(p.pnl_usdc for p in remaining)
        total_inv += r_inv
        total_val += r_val
        total_pnl += r_pnl
        c_r = _pnl_color(r_pnl)
        rows.append([
            Paragraph(f"<i>+{len(remaining)} autres</i>", s["body"]),
            Paragraph("", s["body"]),
            Paragraph("", s["body"]),
            Paragraph("", s["body"]),
            Paragraph(f"{r_inv:.1f}$", s["body"]),
            Paragraph(f"{r_val:.1f}$", s["body"]),
            Paragraph(f'<font color="{c_r}">{_pnl_str(r_pnl)}$</font>', s["body"]),
            Paragraph("", s["body"]),
        ])

    # Total row — uses sum of pnl_usdc (NOT total_val - total_inv)
    t_pct = (total_pnl / total_inv * 100) if total_inv > 0 else 0
    c = _pnl_color(total_pnl)
    rows.append([
        Paragraph("<b>TOTAL</b>", s["body"]),
        Paragraph("", s["body"]),
        Paragraph("", s["body"]),
        Paragraph("", s["body"]),
        Paragraph(f"<b>{total_inv:.1f}$</b>", s["body"]),
        Paragraph(f"<b>{total_val:.1f}$</b>", s["body"]),
        Paragraph(f'<b><font color="{c}">{_pnl_str(total_pnl)}$</font></b>', s["body"]),
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
    """Generate a PDF report of followed traders' performance.

    Only shows OPEN positions (accurate from API).
    Historical PNL is not available via public API.
    """
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

    pct = (data.grand_unrealized / data.grand_invested * 100) if data.grand_invested > 0 else 0
    c_unr = _pnl_color(data.grand_unrealized)

    summary = [
        ["Traders suivis", str(len(data.traders))],
        ["Positions ouvertes", str(data.total_open_positions)],
        ["Capital investi (ouvert)", f"{data.grand_invested:.2f} USDC"],
        ["Valeur actuelle", f"{data.grand_current:.2f} USDC"],
        [
            "PNL positions ouvertes",
            f'<font color="{c_unr}">'
            f"<b>{_pnl_str(data.grand_unrealized)} USDC ({_pnl_str(pct)}%)</b></font>",
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
    elements.append(Spacer(1, 4))

    # Check if any trader has profile data
    has_profiles = any(t.has_profile for t in data.traders)
    if has_profiles:
        elements.append(Paragraph(
            "<i>PNL total, 24h, 7j et 30j sont extraits des profils Polymarket. "
            "Les positions ouvertes montrent le PNL non-realise en temps reel.</i>",
            s["note"],
        ))
    else:
        elements.append(Paragraph(
            "<i>Note : seules les positions ouvertes sont affichees. "
            "Le PNL historique sera disponible si le profil Polymarket est public.</i>",
            s["note"],
        ))

    # ── Per-Trader Sections ──
    for i, trader in enumerate(data.traders):
        if i > 0:
            elements.append(Spacer(1, 8))
        elements.append(_hr())

        # Title with username if available
        title = f"TRADER : {trader.wallet_short}"
        if trader.pseudonym:
            title = f"TRADER : {trader.pseudonym} ({trader.wallet_short})"
        elements.append(Paragraph(title, s["section"]))

        # ── Profile stats (from Polymarket page) ──
        if trader.has_profile:
            profile_rows = [
                ["PNL Total", f'<font color="{_pnl_color(trader.pnl_total)}"><b>{_pnl_str(trader.pnl_total)}$</b></font>'],
                ["PNL 24h", f'<font color="{_pnl_color(trader.pnl_1d)}"><b>{_pnl_str(trader.pnl_1d)}$</b></font>'],
                ["PNL 7 jours", f'<font color="{_pnl_color(trader.pnl_1w)}"><b>{_pnl_str(trader.pnl_1w)}$</b></font>'],
                ["PNL 30 jours", f'<font color="{_pnl_color(trader.pnl_1m)}"><b>{_pnl_str(trader.pnl_1m)}$</b></font>'],
                ["Volume total", f"{trader.volume:,.0f}$"],
                ["Marches trades", str(trader.markets_traded)],
            ]
            profile_table = Table(
                [[Paragraph(r[0], s["body"]), Paragraph(r[1], s["body"])] for r in profile_rows],
                colWidths=[100, 160],
            )
            profile_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f8fafc")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ]))
            elements.append(profile_table)
            elements.append(Spacer(1, 6))
        else:
            # Fallback: show open positions PNL only
            c_t = _pnl_color(trader.total_unrealized)
            t_pct = (trader.total_unrealized / trader.total_invested * 100) if trader.total_invested > 0 else 0
            elements.append(Paragraph(
                f'Positions ouvertes : {len(trader.open_positions)} | '
                f'Investi : {trader.total_invested:.0f}$ | '
                f'PNL ouvert : <font color="{c_t}"><b>{_pnl_str(trader.total_unrealized)}$ '
                f'({_pnl_str(t_pct)}%)</b></font>',
                s["body"],
            ))
            elements.append(Spacer(1, 6))

        # Activity per timeframe
        elements.append(Paragraph("Activite de trading", s["subsection"]))
        elements.append(_activity_table(
            [trader.stats_1h, trader.stats_24h, trader.stats_7d], s
        ))

        # Open positions
        if trader.open_positions:
            elements.append(Paragraph(
                f"Positions ouvertes ({len(trader.open_positions)})",
                s["subsection"]
            ))
            elements.append(_positions_table(trader.open_positions, s, max_rows=15))
        else:
            elements.append(Paragraph(
                "<i>Aucune position ouverte.</i>", s["body"]
            ))

    # ── Footer ──
    _footer(elements, data.generated_at, s)

    doc.build(elements)
    buffer.seek(0)
    return buffer


async def build_trader_report_data(username: str, followed_wallets: list[str]) -> TraderReportData:
    """Build TraderReportData by fetching real data from Polymarket API.

    For each trader:
    - Positions API -> open positions with PNL (accurate)
    - Activity API -> trades per timeframe (1h, 24h, 7j)

    Note: Resolved positions are NOT included because the API only returns
    a small subset (~130 recent) out of potentially thousands of markets.
    This would give misleading PNL totals.
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

        # Fetch positions
        positions = await polymarket_client.get_positions_by_address(wallet)

        # Only keep OPEN positions (not redeemable) — they have accurate PNL
        open_pos = [p for p in positions if not p.redeemable]

        # Build position snapshots
        for p in open_pos:
            trader.open_positions.append(PositionSnapshot(
                title=p.title,
                outcome=p.outcome,
                side="BUY",
                entry_price=p.avg_price,
                current_price=p.current_price,
                invested=p.initial_value,
                current_value=p.current_value,
                pnl_usdc=p.cash_pnl,
                pnl_pct=p.pnl_pct,
                shares=p.size,
                redeemable=False,
            ))

        # Sort by absolute PNL
        trader.open_positions.sort(key=lambda p: abs(p.pnl_usdc), reverse=True)

        # Totals for this trader (open positions only)
        trader.total_unrealized = sum(p.cash_pnl for p in open_pos)
        trader.total_invested = sum(p.initial_value for p in open_pos)
        trader.total_current = sum(p.current_value for p in open_pos)

        # Activity per timeframe — paginated for accurate counts
        for tf_stats, secs in [
            (trader.stats_1h, 3600),
            (trader.stats_24h, 86400),
            (trader.stats_7d, 7 * 86400),
        ]:
            start_ts = now_ts - secs
            tf_activity = await polymarket_client.get_activity_paginated(
                wallet, start=start_ts, max_trades=10000
            )
            tf_stats.trades_count = len(tf_activity)
            tf_stats.buys = sum(1 for a in tf_activity if a.side == "BUY")
            tf_stats.sells = sum(1 for a in tf_activity if a.side == "SELL")
            tf_stats.volume_usdc = sum(a.usdc_size for a in tf_activity)

        # Fetch profile data (PnL total, 1D, 1W, volume) via page scraping
        try:
            profile = await polymarket_client.get_trader_profile(wallet)
            if profile:
                trader.has_profile = True
                trader.username = profile.username
                trader.pseudonym = profile.pseudonym
                trader.pnl_total = profile.pnl_total
                trader.pnl_1d = profile.pnl_1d
                trader.pnl_1w = profile.pnl_1w
                trader.pnl_1m = profile.pnl_1m
                trader.volume = profile.volume
                trader.markets_traded = profile.markets_traded
        except Exception as e:
            logger.warning(f"Failed to get profile for {w_short}: {e}")

        report.traders.append(trader)

        # Accumulate global
        report.grand_unrealized += trader.total_unrealized
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
    elements.append(_recap_timeframe_table(
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
