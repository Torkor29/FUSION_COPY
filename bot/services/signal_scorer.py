"""SignalScorer — scores trade signals 0-100 before execution.

Each signal gets a weighted score based on 6 criteria.
Each criterion returns a score AND the raw data that justifies it,
so the user can see exactly WHY a signal got its score.

Criteria (weighted):
- Spread (15%) — bid-ask tightness
- Liquidity (15%) — market 24h volume
- Conviction (20%) — master's trade size vs portfolio
- Trader form (20%) — rolling 7d win rate
- Timing (15%) — distance to expiry sweet spot
- Consensus (15%) — multiple masters on same market
"""

import logging
from typing import Optional

from bot.db.session import async_session
from bot.models.signal_score import SignalScore
from bot.models.base import utcnow

logger = logging.getLogger(__name__)

# Default score weights (must sum to 1.0)
DEFAULT_WEIGHTS = {
    "spread": 0.15,
    "liquidity": 0.15,
    "conviction": 0.20,
    "trader_form": 0.20,
    "timing": 0.15,
    "consensus": 0.15,
}

# For backward compat
WEIGHTS = DEFAULT_WEIGHTS

# Default criteria config (all ON)
DEFAULT_CRITERIA = {
    "spread": {"on": True, "w": 15},
    "liquidity": {"on": True, "w": 15},
    "conviction": {"on": True, "w": 20},
    "trader_form": {"on": True, "w": 20},
    "timing": {"on": True, "w": 15},
    "consensus": {"on": True, "w": 15},
}


def compute_weights(criteria_config: dict = None) -> dict[str, float]:
    """Compute normalized weights from user criteria config.

    Disabled criteria get weight 0. Remaining weights are
    redistributed proportionally so they sum to 1.0.

    Args:
        criteria_config: {"spread": {"on": True, "w": 15}, ...}
                         If None, uses DEFAULT_CRITERIA (all ON).

    Returns:
        {"spread": 0.15, "liquidity": 0.15, ...} normalized to sum=1.0
    """
    if not criteria_config:
        return dict(DEFAULT_WEIGHTS)

    raw = {}
    for key in DEFAULT_WEIGHTS:
        cfg = criteria_config.get(key, DEFAULT_CRITERIA.get(key, {}))
        if cfg.get("on", True):
            raw[key] = cfg.get("w", DEFAULT_CRITERIA.get(key, {}).get("w", 15))
        else:
            raw[key] = 0

    total = sum(raw.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)  # Fallback: all equal if everything is off

    return {k: round(v / total, 4) for k, v in raw.items()}


class SignalScorer:
    """Scores trade signals on a 0-100 scale with full transparency."""

    def __init__(
        self,
        polymarket_client=None,
        trader_tracker=None,
        market_intel_service=None,
        monitor=None,
    ):
        self._pm = polymarket_client
        self._tracker = trader_tracker
        self._intel = market_intel_service
        self._monitor = monitor

    async def score_signal(self, signal, criteria_config: dict = None) -> SignalScore:
        """Score a trade signal 0-100 with component breakdown + raw data.

        Args:
            signal: TradeSignal from monitor
            criteria_config: Per-user criteria config from UserSettings.scoring_criteria
                             If None, all criteria ON with default weights.
        """
        # Compute user-specific weights (disabled criteria get weight 0)
        weights = compute_weights(criteria_config)

        components = {}
        details = {}

        # Each scorer returns (score: float, detail: dict)
        # Disabled criteria (weight=0) are still scored but won't affect total
        components["spread"], details["spread"] = await self._score_spread(signal)
        components["liquidity"], details["liquidity"] = await self._score_liquidity(signal)
        components["conviction"], details["conviction"] = await self._score_conviction(signal)
        components["trader_form"], details["trader_form"] = await self._score_trader_form(signal)
        components["timing"], details["timing"] = await self._score_timing(signal)
        components["consensus"], details["consensus"] = await self._score_consensus(signal)

        # Weighted total (only enabled criteria contribute)
        total = sum(components[k] * weights[k] for k in weights)
        total = round(min(100, max(0, total)), 1)

        # Store scores, weights, and raw details in components JSON
        full_components = {}
        for k in DEFAULT_WEIGHTS:
            enabled = weights.get(k, 0) > 0
            full_components[k] = {
                "score": components[k],
                "enabled": enabled,
                "weight": weights[k],
                "weight_pct": round(weights[k] * 100),
                "weighted": round(components[k] * weights[k], 1),
                **details.get(k, {}),
            }

        score = SignalScore(
            signal_hash=SignalScore.make_hash(
                signal.master_wallet, signal.market_id, signal.token_id, signal.side
            ),
            master_wallet=signal.master_wallet,
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=signal.side,
            total_score=total,
            components=full_components,
            passed=False,
            created_at=utcnow(),
        )

        try:
            async with async_session() as session:
                session.add(score)
                await session.commit()
                await session.refresh(score)
        except Exception as e:
            logger.warning("Failed to persist signal score: %s", e)

        logger.info(
            "Signal scored: %.0f/100 %s %s on %s",
            total, signal.side, signal.master_wallet[:10],
            (signal.market_question or signal.market_id)[:40],
        )

        return score

    # ── Component scorers — each returns (score, details_dict) ────

    async def _score_spread(self, signal) -> tuple[float, dict]:
        """Spread bid-ask. Plus c'est serré, mieux c'est."""
        if not self._pm:
            return 50.0, {"spread_pct": None, "reason": "Données indisponibles"}

        try:
            book = await self._pm.get_order_book(signal.token_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])

            if not bids or not asks:
                return 30.0, {"spread_pct": None, "reason": "Pas de carnet d'ordres"}

            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 1))

            if best_ask <= 0:
                return 30.0, {"spread_pct": None, "reason": "Prix ask invalide"}

            spread_pct = round(((best_ask - best_bid) / best_ask) * 100, 2)

            if spread_pct < 1:
                score = 100.0
            elif spread_pct < 2:
                score = 80.0
            elif spread_pct < 3:
                score = 60.0
            elif spread_pct < 5:
                score = 30.0
            else:
                score = 0.0

            return score, {
                "spread_pct": spread_pct,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "reason": (
                    f"Spread de {spread_pct:.1f}% "
                    f"(bid ${best_bid:.4f} / ask ${best_ask:.4f})"
                ),
            }

        except Exception as e:
            logger.debug("Spread scoring failed: %s", e)
            return 50.0, {"spread_pct": None, "reason": f"Erreur: {str(e)[:50]}"}

    async def _score_liquidity(self, signal) -> tuple[float, dict]:
        """Volume 24h du marché. Plus c'est élevé, plus c'est liquide."""
        if not self._intel:
            return 50.0, {"volume_24h": None, "reason": "Données indisponibles"}

        try:
            intel = await self._intel.get_intel(signal.market_id)
            if not intel:
                return 40.0, {"volume_24h": None, "reason": "Marché non trouvé"}

            vol = intel.volume_24h

            if vol >= 500_000:
                score = 100.0
            elif vol >= 100_000:
                score = 80.0
            elif vol >= 50_000:
                score = 60.0
            elif vol >= 10_000:
                score = 40.0
            else:
                score = 10.0

            return score, {
                "volume_24h": vol,
                "reason": f"Volume 24h: ${vol:,.0f}",
            }

        except Exception as e:
            logger.debug("Liquidity scoring failed: %s", e)
            return 50.0, {"volume_24h": None, "reason": f"Erreur: {str(e)[:50]}"}

    async def _score_conviction(self, signal) -> tuple[float, dict]:
        """Taille du trade vs portfolio du trader. Gros trade = haute conviction."""
        if not self._pm:
            return 50.0, {"conviction_pct": None, "reason": "Données indisponibles"}

        try:
            trade_value = signal.size * signal.price

            positions = await self._pm.get_positions_by_address(signal.master_wallet)
            if not positions:
                return 50.0, {"conviction_pct": None, "reason": "Portfolio trader vide"}

            portfolio_value = sum(
                abs(float(p.get("currentValue", 0) or 0)) for p in positions
            )
            if portfolio_value <= 0:
                return 50.0, {"conviction_pct": None, "reason": "Valeur portfolio = 0"}

            conviction_pct = round((trade_value / portfolio_value) * 100, 1)

            if conviction_pct >= 10:
                score = 100.0
            elif conviction_pct >= 5:
                score = 80.0
            elif conviction_pct >= 2:
                score = 60.0
            else:
                score = 20.0

            return score, {
                "conviction_pct": conviction_pct,
                "trade_value": round(trade_value, 2),
                "portfolio_value": round(portfolio_value, 2),
                "reason": (
                    f"Trade de ${trade_value:.0f} = {conviction_pct:.1f}% "
                    f"de son portfolio (${portfolio_value:,.0f})"
                ),
            }

        except Exception as e:
            logger.debug("Conviction scoring failed: %s", e)
            return 50.0, {"conviction_pct": None, "reason": f"Erreur: {str(e)[:50]}"}

    async def _score_trader_form(self, signal) -> tuple[float, dict]:
        """Win rate 7 jours du trader. Plus c'est haut, mieux c'est."""
        if not self._tracker:
            return 50.0, {"win_rate": None, "trades": 0, "reason": "Tracker désactivé"}

        try:
            stats = await self._tracker.get_stats(signal.master_wallet, "7d")
            if not stats or stats.trade_count < 3:
                return 50.0, {
                    "win_rate": None,
                    "trades": stats.trade_count if stats else 0,
                    "reason": f"Pas assez de données ({stats.trade_count if stats else 0} trades, min 3)",
                }

            wr = stats.win_rate
            trades = stats.trade_count
            streak = stats.current_streak

            if wr >= 70:
                score = 100.0
            elif wr >= 60:
                score = 80.0
            elif wr >= 50:
                score = 60.0
            elif wr >= 40:
                score = 30.0
            else:
                score = 0.0

            streak_text = (
                f", série de {streak} victoires" if streak > 0
                else f", série de {abs(streak)} défaites" if streak < 0
                else ""
            )

            return score, {
                "win_rate": round(wr, 1),
                "trades": trades,
                "streak": streak,
                "reason": f"Win rate 7j: {wr:.0f}% sur {trades} trades{streak_text}",
            }

        except Exception as e:
            logger.debug("Trader form scoring failed: %s", e)
            return 50.0, {"win_rate": None, "trades": 0, "reason": f"Erreur: {str(e)[:50]}"}

    async def _score_timing(self, signal) -> tuple[float, dict]:
        """Distance à l'expiry. Sweet spot = 2h-48h."""
        if not self._intel:
            return 50.0, {"hours_to_expiry": None, "reason": "Données indisponibles"}

        try:
            intel = await self._intel.get_intel(signal.market_id)
            if not intel or not intel.expiry:
                return 50.0, {"hours_to_expiry": None, "reason": "Pas de date d'expiry"}

            now = utcnow()
            hours = round((intel.expiry - now).total_seconds() / 3600, 1)

            if hours < 0:
                score, label = 0.0, "Déjà expiré"
            elif hours < 0.5:
                score, label = 20.0, f"{hours*60:.0f} min — trop proche, risqué"
            elif hours < 2:
                score, label = 50.0, f"{hours:.1f}h — correct"
            elif hours < 48:
                score, label = 100.0, f"{hours:.0f}h — zone idéale (2-48h)"
            elif hours < 168:
                score, label = 80.0, f"{hours/24:.0f}j — moyen terme, bon"
            elif hours < 720:
                score, label = 60.0, f"{hours/24:.0f}j — long terme"
            elif hours < 2160:
                score, label = 40.0, f"{hours/24:.0f}j — capital bloqué longtemps"
            else:
                score, label = 20.0, f"{hours/24:.0f}j — trop lointain"

            return score, {
                "hours_to_expiry": hours,
                "reason": f"Expiry dans {label}",
            }

        except Exception as e:
            logger.debug("Timing scoring failed: %s", e)
            return 50.0, {"hours_to_expiry": None, "reason": f"Erreur: {str(e)[:50]}"}

    async def _score_consensus(self, signal) -> tuple[float, dict]:
        """Nombre d'autres traders suivis sur le même marché."""
        if not self._monitor:
            return 50.0, {"other_traders": 0, "reason": "Monitor indisponible"}

        try:
            count = 0
            wallet_states = getattr(self._monitor, "_wallet_states", {})

            for wallet, positions in wallet_states.items():
                if wallet == signal.master_wallet:
                    continue
                for pos in (positions or []):
                    token = pos.get("asset", {}).get("id", "") or pos.get("token_id", "")
                    if token == signal.token_id:
                        count += 1
                        break

            if count >= 3:
                score = 100.0
            elif count >= 2:
                score = 70.0
            elif count >= 1:
                score = 40.0
            else:
                score = 20.0

            if count == 0:
                reason = "Aucun autre trader suivi sur ce marché"
            else:
                reason = f"{count} autre(s) trader(s) suivi(s) ont la même position"

            return score, {
                "other_traders": count,
                "reason": reason,
            }

        except Exception as e:
            logger.debug("Consensus scoring failed: %s", e)
            return 50.0, {"other_traders": 0, "reason": f"Erreur: {str(e)[:50]}"}

    # ── Formatting ────────────────────────────────────────────────

    @staticmethod
    def format_score(score: SignalScore, signal) -> str:
        """Format détaillé pour le topic Signals — breakdown transparent."""
        from bot.utils.formatting import bar, badge_score, short_wallet as sw, SEP

        c = score.components or {}
        grade = badge_score(score.total_score)
        market_name = getattr(signal, "market_question", None) or signal.market_id[:20]
        wallet = sw(signal.master_wallet)

        CRITERIA_ORDER = [
            ("spread", "📏 Spread"),
            ("liquidity", "💧 Liquidité"),
            ("conviction", "💪 Conviction"),
            ("trader_form", "📈 Forme trader"),
            ("timing", "⏱ Timing"),
            ("consensus", "👥 Consensus"),
        ]

        lines = [
            f"📊 *{score.total_score:.0f}/100* {grade}\n",
            f"*{signal.side}* sur _{market_name}_",
            f"Trader: `{wallet}` | Prix: ${signal.price:.4f}\n",
            f"*── Détail du calcul ──*\n",
        ]

        # Sort by weighted contribution (biggest impact first)
        sorted_criteria = sorted(
            CRITERIA_ORDER,
            key=lambda x: c.get(x[0], {}).get("weighted", 0)
            if isinstance(c.get(x[0]), dict) else 0,
            reverse=True,
        )

        for key, label in sorted_criteria:
            comp = c.get(key, {})
            if isinstance(comp, dict):
                s = comp.get("score", 50)
                enabled = comp.get("enabled", True)
                wpct = comp.get("weight_pct", 15)
                weighted = comp.get("weighted", 0)
                reason = comp.get("reason", "—")
            else:
                s = float(comp)
                enabled = True
                wpct = 15
                weighted = round(s * DEFAULT_WEIGHTS.get(key, 0.15), 1)
                reason = "—"

            if not enabled:
                lines.append(f"⬜ {label} — _désactivé_")
            else:
                vbar = bar(s, 100, 10)
                lines.append(f"{vbar} {label} ({wpct}%) → *{weighted:.0f}* pts")
                lines.append(f"  _{reason}_")

        lines.append(f"\n{SEP}")
        lines.append(f"*Total: {score.total_score:.0f}/100* {grade}")

        return "\n".join(lines)

    @staticmethod
    def format_score_compact(score: SignalScore, signal) -> str:
        """Format compact pour la notification de trade copié."""
        from bot.utils.formatting import bar, badge_score

        c = score.components or {}
        grade = badge_score(score.total_score)

        LABELS = {
            "spread": "spread", "liquidity": "liquidité",
            "conviction": "conviction", "trader_form": "forme",
            "timing": "timing", "consensus": "consensus",
        }

        # Find strongest and weakest enabled criteria
        best_key, worst_key = None, None
        best_s, worst_s = -1, 101
        for key in DEFAULT_WEIGHTS:
            comp = c.get(key, {})
            if isinstance(comp, dict):
                if not comp.get("enabled", True):
                    continue
                s = comp.get("score", 50)
            else:
                s = float(comp)
            if s > best_s:
                best_s, best_key = s, key
            if s < worst_s:
                worst_s, worst_key = s, key

        score_bar = bar(score.total_score, 100, 10)
        best = LABELS.get(best_key, "?")
        worst = LABELS.get(worst_key, "?")

        return (
            f"🧠 {score_bar} *{score.total_score:.0f}/100* {grade}\n"
            f"  ✅ Fort: {best} ({best_s:.0f}) | ⚠️ Faible: {worst} ({worst_s:.0f})"
        )
