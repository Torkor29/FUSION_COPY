"""HTML report generation — interactive trader performance report.

Generates a self-contained HTML file with:
- Collapsible sections per trader
- Expandable trade/position details
- Profile stats (PNL total, 1D, 1W, 1M) from Polymarket page scraping
- Activity stats per timeframe with pagination
- Global summary
"""

import io
import logging
import time
from datetime import datetime, timezone
from html import escape

from bot.services.report import (
    TraderReportData,
    TraderSection,
    RecapReportData,
    TimeframeStats,
    PositionSnapshot,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# CSS + JS template
# ═══════════════════════════════════════════════════════════════

_CSS = """
:root {
  --bg: #0f172a; --card: #1e293b; --card2: #334155;
  --text: #e2e8f0; --muted: #94a3b8; --border: #475569;
  --green: #22c55e; --red: #ef4444; --blue: #3b82f6;
  --gold: #f59e0b; --purple: #a855f7;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text);
  padding: 16px; max-width: 900px; margin: 0 auto;
  font-size: 14px; line-height: 1.5;
}
h1 { font-size: 22px; color: var(--gold); margin-bottom: 4px; }
.subtitle { color: var(--muted); font-size: 12px; margin-bottom: 16px; }
.card {
  background: var(--card); border-radius: 10px;
  padding: 16px; margin-bottom: 12px;
  border: 1px solid var(--border);
}
.card-header {
  display: flex; justify-content: space-between; align-items: center;
  cursor: pointer; user-select: none;
}
.card-header:hover { opacity: 0.85; }
.card-header h2 { font-size: 16px; margin: 0; }
.card-header .toggle { font-size: 18px; transition: transform 0.2s; }
.card-header .toggle.open { transform: rotate(90deg); }
.card-body { display: none; margin-top: 12px; }
.card-body.open { display: block; }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 6px;
  font-size: 11px; font-weight: 600;
}
.badge-green { background: rgba(34,197,94,0.15); color: var(--green); }
.badge-red { background: rgba(239,68,68,0.15); color: var(--red); }
.badge-blue { background: rgba(59,130,246,0.15); color: var(--blue); }
.pnl-pos { color: var(--green); font-weight: 600; }
.pnl-neg { color: var(--red); font-weight: 600; }
.stat-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 8px; margin: 10px 0;
}
.stat-box {
  background: var(--card2); border-radius: 8px; padding: 10px;
  text-align: center;
}
.stat-box .label { font-size: 11px; color: var(--muted); }
.stat-box .value { font-size: 18px; font-weight: 700; margin-top: 2px; }
table {
  width: 100%; border-collapse: collapse; margin: 8px 0;
  font-size: 12px;
}
th {
  background: var(--card2); color: var(--muted); text-align: left;
  padding: 6px 8px; font-weight: 600; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
tr:hover td { background: rgba(255,255,255,0.03); }
.section-title {
  font-size: 13px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 1px; margin: 14px 0 6px; font-weight: 600;
}
.detail-toggle {
  cursor: pointer; color: var(--blue); font-size: 11px;
  text-decoration: underline;
}
.detail-content { display: none; }
.detail-content.open { display: table-row-group; }
.summary-row td { font-weight: 700; background: var(--card2); }
.footer {
  text-align: center; color: var(--muted); font-size: 11px;
  margin-top: 20px; padding-top: 12px;
  border-top: 1px solid var(--border);
}
.global-card { border: 2px solid var(--gold); }
@media (max-width: 600px) {
  body { padding: 8px; font-size: 13px; }
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
  table { font-size: 11px; }
  th, td { padding: 4px 6px; }
}
"""

_JS = """
function toggleCard(id) {
  const body = document.getElementById(id);
  const toggle = document.getElementById(id + '-toggle');
  body.classList.toggle('open');
  toggle.classList.toggle('open');
}
function toggleDetail(id) {
  const el = document.getElementById(id);
  el.classList.toggle('open');
  const link = document.getElementById(id + '-link');
  if (el.classList.contains('open')) {
    link.textContent = 'Masquer';
  } else {
    link.textContent = 'Voir details';
  }
}
// Open all trader cards by default
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.auto-open').forEach(function(el) {
    el.classList.add('open');
    const toggle = document.getElementById(el.id + '-toggle');
    if (toggle) toggle.classList.add('open');
  });
});
"""


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _pnl(val: float) -> str:
    """Format PNL with sign."""
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:,.2f}$"


def _pnl_class(val: float) -> str:
    return "pnl-pos" if val >= 0 else "pnl-neg"


def _pnl_html(val: float) -> str:
    return f'<span class="{_pnl_class(val)}">{_pnl(val)}</span>'


def _pct_html(val: float) -> str:
    sign = "+" if val >= 0 else ""
    return f'<span class="{_pnl_class(val)}">{sign}{val:.1f}%</span>'


def _badge(text: str, cls: str = "blue") -> str:
    return f'<span class="badge badge-{cls}">{escape(text)}</span>'


# ═══════════════════════════════════════════════════════════════
# 1) TRADER REPORT (Dashboard) — HTML generation
# ═══════════════════════════════════════════════════════════════

def generate_trader_report_html(data: TraderReportData) -> io.BytesIO:
    """Generate a self-contained HTML report of followed traders."""

    parts = []

    # ── HTML head ──
    parts.append(f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WenPolymarket - Dashboard Traders</title>
<style>{_CSS}</style>
</head>
<body>
<h1>WENPOLYMARKET</h1>
<div class="subtitle">
  Rapport Dashboard &mdash; Performance des Traders Suivis<br>
  Utilisateur : {escape(data.username)} &bull;
  {data.generated_at.strftime('%d/%m/%Y %H:%M UTC')}
</div>
""")

    # ── Global Summary Card ──
    pct = (data.grand_unrealized / data.grand_invested * 100) if data.grand_invested > 0 else 0
    # Sum profile PNLs for global display
    total_pnl_profile = sum(t.pnl_total for t in data.traders if t.has_profile)
    total_pnl_1w = sum(t.pnl_1w for t in data.traders if t.has_profile)
    total_pnl_1d = sum(t.pnl_1d for t in data.traders if t.has_profile)
    has_profiles = any(t.has_profile for t in data.traders)

    parts.append(f"""
<div class="card global-card">
  <div class="card-header" onclick="toggleCard('global')">
    <h2>RESUME GLOBAL</h2>
    <span class="toggle open" id="global-toggle">&#9654;</span>
  </div>
  <div class="card-body auto-open" id="global">
    <div class="stat-grid">
      <div class="stat-box">
        <div class="label">Traders suivis</div>
        <div class="value">{len(data.traders)}</div>
      </div>
      <div class="stat-box">
        <div class="label">Positions ouvertes</div>
        <div class="value">{data.total_open_positions}</div>
      </div>
      <div class="stat-box">
        <div class="label">Capital investi</div>
        <div class="value">{data.grand_invested:,.0f}$</div>
      </div>
      <div class="stat-box">
        <div class="label">PNL ouvert</div>
        <div class="value {_pnl_class(data.grand_unrealized)}">{_pnl(data.grand_unrealized)}</div>
      </div>
""")

    if has_profiles:
        parts.append(f"""
      <div class="stat-box">
        <div class="label">PNL Total (profils)</div>
        <div class="value {_pnl_class(total_pnl_profile)}">{_pnl(total_pnl_profile)}</div>
      </div>
      <div class="stat-box">
        <div class="label">PNL 7 jours</div>
        <div class="value {_pnl_class(total_pnl_1w)}">{_pnl(total_pnl_1w)}</div>
      </div>
      <div class="stat-box">
        <div class="label">PNL 24h</div>
        <div class="value {_pnl_class(total_pnl_1d)}">{_pnl(total_pnl_1d)}</div>
      </div>
""")

    parts.append("""
    </div>
  </div>
</div>
""")

    # ── Per-Trader Cards ──
    for i, trader in enumerate(data.traders):
        card_id = f"trader-{i}"
        name = escape(trader.pseudonym) if trader.pseudonym else trader.wallet_short
        profile_link = f"https://polymarket.com/@{trader.username}" if trader.username else ""

        # Summary badge
        if trader.has_profile:
            pnl_badge = _badge(f"PNL: {_pnl(trader.pnl_total)}", "green" if trader.pnl_total >= 0 else "red")
        else:
            pnl_badge = _badge(f"PNL ouvert: {_pnl(trader.total_unrealized)}", "green" if trader.total_unrealized >= 0 else "red")

        parts.append(f"""
<div class="card">
  <div class="card-header" onclick="toggleCard('{card_id}')">
    <h2>{name} {pnl_badge}</h2>
    <span class="toggle" id="{card_id}-toggle">&#9654;</span>
  </div>
  <div class="card-body" id="{card_id}">
""")

        # Profile link
        if profile_link:
            parts.append(f'<div style="margin-bottom:8px;"><a href="{profile_link}" target="_blank" style="color:var(--blue);font-size:12px;">Voir sur Polymarket &rarr;</a></div>')

        # ── Profile Stats ──
        if trader.has_profile:
            parts.append(f"""
    <div class="stat-grid">
      <div class="stat-box">
        <div class="label">PNL Total</div>
        <div class="value {_pnl_class(trader.pnl_total)}">{_pnl(trader.pnl_total)}</div>
      </div>
      <div class="stat-box">
        <div class="label">PNL 24h</div>
        <div class="value {_pnl_class(trader.pnl_1d)}">{_pnl(trader.pnl_1d)}</div>
      </div>
      <div class="stat-box">
        <div class="label">PNL 7 jours</div>
        <div class="value {_pnl_class(trader.pnl_1w)}">{_pnl(trader.pnl_1w)}</div>
      </div>
      <div class="stat-box">
        <div class="label">PNL 30 jours</div>
        <div class="value {_pnl_class(trader.pnl_1m)}">{_pnl(trader.pnl_1m)}</div>
      </div>
      <div class="stat-box">
        <div class="label">Volume total</div>
        <div class="value">{trader.volume:,.0f}$</div>
      </div>
      <div class="stat-box">
        <div class="label">Marches trades</div>
        <div class="value">{trader.markets_traded:,}</div>
      </div>
    </div>
""")

        # ── Activity table ──
        parts.append('<div class="section-title">Activite de trading</div>')
        parts.append("""<table>
<thead><tr><th>Periode</th><th>Trades</th><th>Buys</th><th>Sells</th><th>Volume</th></tr></thead>
<tbody>""")
        for st in [trader.stats_1h, trader.stats_24h, trader.stats_7d]:
            parts.append(f"""<tr>
  <td><b>{st.label}</b></td>
  <td>{st.trades_count:,}</td>
  <td>{st.buys:,}</td>
  <td>{st.sells:,}</td>
  <td>{st.volume_usdc:,.0f}$</td>
</tr>""")
        parts.append("</tbody></table>")

        # ── Open Positions ──
        if trader.open_positions:
            detail_id = f"positions-{i}"
            nb = len(trader.open_positions)
            total_inv = sum(p.invested for p in trader.open_positions)
            total_pnl = sum(p.pnl_usdc for p in trader.open_positions)

            parts.append(f"""
    <div class="section-title">
      Positions ouvertes ({nb}) &mdash;
      Investi: {total_inv:,.0f}$ &bull;
      PNL: {_pnl_html(total_pnl)}
      &nbsp; <span class="detail-toggle" id="{detail_id}-link" onclick="toggleDetail('{detail_id}')">Voir details</span>
    </div>
""")
            # Summary table (top 5 always visible)
            parts.append("""<table>
<thead><tr><th>Marche</th><th>Outcome</th><th>Entry</th><th>Now</th><th>Investi</th><th>PNL</th><th>%</th></tr></thead>
<tbody>""")
            for j, p in enumerate(trader.open_positions[:5]):
                title = escape(p.title[:30])
                parts.append(f"""<tr>
  <td>{title}</td>
  <td>{escape(p.outcome)}</td>
  <td>{p.entry_price:.2f}</td>
  <td>{p.current_price:.2f}</td>
  <td>{p.invested:.1f}$</td>
  <td>{_pnl_html(p.pnl_usdc)}</td>
  <td>{_pct_html(p.pnl_pct)}</td>
</tr>""")

            # Hidden detail rows
            if nb > 5:
                parts.append(f'</tbody><tbody class="detail-content" id="{detail_id}">')
                for p in trader.open_positions[5:]:
                    title = escape(p.title[:30])
                    parts.append(f"""<tr>
  <td>{title}</td>
  <td>{escape(p.outcome)}</td>
  <td>{p.entry_price:.2f}</td>
  <td>{p.current_price:.2f}</td>
  <td>{p.invested:.1f}$</td>
  <td>{_pnl_html(p.pnl_usdc)}</td>
  <td>{_pct_html(p.pnl_pct)}</td>
</tr>""")

            # Total row
            total_val = sum(p.current_value for p in trader.open_positions)
            total_pct = (total_pnl / total_inv * 100) if total_inv > 0 else 0
            parts.append(f"""</tbody>
<tfoot><tr class="summary-row">
  <td colspan="4"><b>TOTAL</b></td>
  <td><b>{total_inv:,.0f}$</b></td>
  <td><b>{_pnl_html(total_pnl)}</b></td>
  <td><b>{_pct_html(total_pct)}</b></td>
</tr></tfoot></table>""")
        else:
            parts.append('<div style="color:var(--muted);font-style:italic;margin:8px 0;">Aucune position ouverte.</div>')

        parts.append("</div></div>")  # Close card-body and card

    # ── Footer ──
    parts.append(f"""
<div class="footer">
  WENPOLYMARKET &mdash; Rapport genere le {data.generated_at.strftime('%d/%m/%Y %H:%M UTC')}<br>
  Les performances passees ne garantissent pas les resultats futurs.
</div>
<script>{_JS}</script>
</body></html>""")

    html_content = "\n".join(parts)
    buffer = io.BytesIO(html_content.encode("utf-8"))
    buffer.seek(0)
    return buffer


# ═══════════════════════════════════════════════════════════════
# 2) RECAP REPORT (Our Trades) — HTML generation
# ═══════════════════════════════════════════════════════════════

def generate_recap_report_html(data: RecapReportData) -> io.BytesIO:
    """Generate a self-contained HTML report of our copied trades."""

    parts = []
    mode = "PAPER TRADING" if data.is_paper else "LIVE TRADING"

    parts.append(f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WenPolymarket - Recap {mode}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>WENPOLYMARKET</h1>
<div class="subtitle">
  Rapport Recap &mdash; {mode}<br>
  {escape(data.username)} &bull; Wallet: {escape(data.wallet_short)} &bull;
  {data.generated_at.strftime('%d/%m/%Y %H:%M UTC')}
</div>
""")

    # ── Portfolio Card ──
    parts.append(f"""
<div class="card global-card">
  <div class="card-header" onclick="toggleCard('portfolio')">
    <h2>PORTEFEUILLE</h2>
    <span class="toggle open" id="portfolio-toggle">&#9654;</span>
  </div>
  <div class="card-body auto-open" id="portfolio">
    <div class="stat-grid">
      <div class="stat-box">
        <div class="label">Capital initial</div>
        <div class="value">{data.paper_initial:,.0f}$</div>
      </div>
      <div class="stat-box">
        <div class="label">Cash disponible</div>
        <div class="value">{data.paper_balance:,.0f}$</div>
      </div>
      <div class="stat-box">
        <div class="label">Portefeuille total</div>
        <div class="value">{data.portfolio_value:,.0f}$</div>
      </div>
      <div class="stat-box">
        <div class="label">PNL total</div>
        <div class="value {_pnl_class(data.total_pnl)}">{_pnl(data.total_pnl)} ({data.total_pnl_pct:+.1f}%)</div>
      </div>
      <div class="stat-box">
        <div class="label">Trades resolus</div>
        <div class="value">{data.settled_trades_count}</div>
      </div>
      <div class="stat-box">
        <div class="label">Win rate</div>
        <div class="value">{f'{data.overall_win_rate:.0f}%' if data.overall_win_rate >= 0 else 'N/A'}</div>
      </div>
    </div>
  </div>
</div>
""")

    # ── Performance by Timeframe ──
    parts.append("""
<div class="card">
  <div class="card-header" onclick="toggleCard('perf')">
    <h2>PERFORMANCE PAR PERIODE</h2>
    <span class="toggle open" id="perf-toggle">&#9654;</span>
  </div>
  <div class="card-body auto-open" id="perf">
    <table>
    <thead><tr><th>Periode</th><th>Trades</th><th>B/S</th><th>Volume</th><th>PNL non-realise</th><th>PNL realise</th><th>W/L</th></tr></thead>
    <tbody>
""")
    for st in [data.stats_1h, data.stats_24h, data.stats_7d, data.stats_all]:
        wr = f"{st.wins}W/{st.losses}L" if (st.wins + st.losses) > 0 else "-"
        parts.append(f"""<tr>
  <td><b>{st.label}</b></td>
  <td>{st.trades_count}</td>
  <td>{st.buys}B/{st.sells}S</td>
  <td>{st.volume_usdc:,.0f}$</td>
  <td>{_pnl_html(st.unrealized_pnl)}</td>
  <td>{_pnl_html(st.realized_pnl)}</td>
  <td>{wr}</td>
</tr>""")
    parts.append("</tbody></table></div></div>")

    # ── Open Positions ──
    nb = len(data.open_positions)
    parts.append(f"""
<div class="card">
  <div class="card-header" onclick="toggleCard('positions')">
    <h2>POSITIONS ACTIVES ({nb})</h2>
    <span class="toggle" id="positions-toggle">&#9654;</span>
  </div>
  <div class="card-body" id="positions">
""")
    if data.open_positions:
        parts.append("""<table>
<thead><tr><th>Marche</th><th>Outcome</th><th>Entry</th><th>Now</th><th>Investi</th><th>Valeur</th><th>PNL</th><th>%</th></tr></thead>
<tbody>""")
        total_inv = 0.0
        total_val = 0.0
        total_pnl = 0.0
        for p in data.open_positions:
            total_inv += p.invested
            total_val += p.current_value
            total_pnl += p.pnl_usdc
            title = escape(p.title[:28])
            parts.append(f"""<tr>
  <td>{title}</td>
  <td>{escape(p.outcome)}</td>
  <td>{p.entry_price:.2f}</td>
  <td>{p.current_price:.2f}</td>
  <td>{p.invested:.1f}$</td>
  <td>{p.current_value:.1f}$</td>
  <td>{_pnl_html(p.pnl_usdc)}</td>
  <td>{_pct_html(p.pnl_pct)}</td>
</tr>""")
        t_pct = (total_pnl / total_inv * 100) if total_inv > 0 else 0
        parts.append(f"""</tbody>
<tfoot><tr class="summary-row">
  <td colspan="4"><b>TOTAL</b></td>
  <td><b>{total_inv:,.0f}$</b></td>
  <td><b>{total_val:,.0f}$</b></td>
  <td><b>{_pnl_html(total_pnl)}</b></td>
  <td><b>{_pct_html(t_pct)}</b></td>
</tr></tfoot></table>""")
    else:
        parts.append('<div style="color:var(--muted);font-style:italic;">Aucune position ouverte.</div>')

    parts.append("</div></div>")

    # ── Footer ──
    parts.append(f"""
<div class="footer">
  WENPOLYMARKET &mdash; Rapport genere le {data.generated_at.strftime('%d/%m/%Y %H:%M UTC')}<br>
  Les performances passees ne garantissent pas les resultats futurs.
</div>
<script>{_JS}</script>
</body></html>""")

    html_content = "\n".join(parts)
    buffer = io.BytesIO(html_content.encode("utf-8"))
    buffer.seek(0)
    return buffer
