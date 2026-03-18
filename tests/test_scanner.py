"""Tests for the Trader Scanner module.

Covers:
- scrape_leaderboard() parsing logic
- apply_filters() with all filter combinations
- run_scan() end-to-end with mocked HTTP
- Edge cases: empty data, rate limiting, fallback
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.services.scanner import (
    ScannedTrader,
    ScanFilters,
    _safe_float,
    apply_filters,
    scrape_leaderboard,
    run_scan,
)

# ── Fixtures ───────────────────────────────────────────────────────

SAMPLE_LEADERBOARD_HTML = """
<html><body>
<script id="__NEXT_DATA__" type="application/json">{NEXT_DATA}</script>
</body></html>
"""


def _make_next_data(
    profit_entries=None, volume_entries=None, extra_queries=None
):
    """Build a realistic __NEXT_DATA__ JSON structure."""
    queries = []

    if volume_entries is not None:
        queries.append({
            "queryKey": ["/leaderboard", "volume", "30d", 1, "crypto", None],
            "state": {
                "data": volume_entries,
                "status": "success",
            },
        })

    if profit_entries is not None:
        queries.append({
            "queryKey": ["/leaderboard", "profit", "30d", 1, "crypto", None],
            "state": {
                "data": profit_entries,
                "status": "success",
            },
        })

    if extra_queries:
        queries.extend(extra_queries)

    return json.dumps({
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": queries,
                },
            },
        },
    })


def _make_entry(wallet, name="Trader", pnl=100.0, volume=1000.0, amount=None):
    """Create a leaderboard entry dict."""
    return {
        "rank": 1,
        "proxyWallet": wallet,
        "name": name,
        "pseudonym": f"pseudo-{name}",
        "amount": amount if amount is not None else pnl,
        "pnl": pnl,
        "volume": volume,
        "realized": 0,
        "unrealized": 0,
        "profileImage": "https://example.com/img.png",
    }


WALLET_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
WALLET_C = "0xcccccccccccccccccccccccccccccccccccccccc"


# ── _safe_float ────────────────────────────────────────────────────

class TestSafeFloat:
    def test_normal_float(self):
        assert _safe_float(3.14) == 3.14

    def test_int(self):
        assert _safe_float(42) == 42.0

    def test_string_number(self):
        assert _safe_float("123.45") == 123.45

    def test_none(self):
        assert _safe_float(None) == 0.0

    def test_invalid_string(self):
        assert _safe_float("not_a_number") == 0.0

    def test_empty_string(self):
        assert _safe_float("") == 0.0


# ── scrape_leaderboard ─────────────────────────────────────────────

class TestScrapeLeaderboard:
    """Test leaderboard HTML parsing."""

    @pytest.fixture
    def mock_http(self):
        """Mock HTTP client."""
        mock_client = AsyncMock()
        return mock_client

    async def _run_scrape(self, html_content, mock_http):
        """Helper to run scrape_leaderboard with mocked HTTP."""
        mock_resp = MagicMock()
        mock_resp.text = html_content
        mock_resp.raise_for_status = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch(
            "bot.services.polymarket.polymarket_client"
        ) as mock_pm:
            mock_pm._get_http = AsyncMock(return_value=mock_http)
            result = await scrape_leaderboard("crypto", "1m", 50)

        return result

    async def test_basic_parsing(self, mock_http):
        """Test parsing with both profit and volume queries."""
        entries = [
            _make_entry(WALLET_A, "Alice", pnl=500, volume=10000),
            _make_entry(WALLET_B, "Bob", pnl=300, volume=8000),
        ]
        nd = _make_next_data(profit_entries=entries, volume_entries=entries)
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)

        result = await self._run_scrape(html, mock_http)

        assert len(result) == 2
        assert result[0]["wallet"] == WALLET_A.lower()
        assert result[0]["username"] == "Alice"
        assert result[0]["pnl"] == 500.0
        assert result[0]["volume"] == 10000.0

    async def test_merge_profit_and_volume_queries(self, mock_http):
        """Wallets appearing in both queries should be merged."""
        vol_entries = [
            _make_entry(WALLET_A, "Alice", pnl=500, volume=10000, amount=10000),
        ]
        profit_entries = [
            _make_entry(WALLET_A, "Alice", pnl=500, volume=10000, amount=500),
        ]
        nd = _make_next_data(
            profit_entries=profit_entries, volume_entries=vol_entries
        )
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)

        result = await self._run_scrape(html, mock_http)

        # Should have 1 entry (merged)
        assert len(result) == 1
        assert result[0]["pnl"] == 500.0
        assert result[0]["volume"] == 10000.0

    async def test_different_wallets_in_queries(self, mock_http):
        """Different wallets in profit vs volume queries are all included."""
        vol_entries = [
            _make_entry(WALLET_A, "Alice", pnl=100, volume=5000),
        ]
        profit_entries = [
            _make_entry(WALLET_B, "Bob", pnl=200, volume=3000),
        ]
        nd = _make_next_data(
            profit_entries=profit_entries, volume_entries=vol_entries
        )
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)

        result = await self._run_scrape(html, mock_http)

        assert len(result) == 2
        wallets = {r["wallet"] for r in result}
        assert WALLET_A.lower() in wallets
        assert WALLET_B.lower() in wallets

    async def test_no_next_data(self, mock_http):
        """Returns empty list when no __NEXT_DATA__ found."""
        html = "<html><body>No data here</body></html>"
        result = await self._run_scrape(html, mock_http)
        assert result == []

    async def test_empty_queries(self, mock_http):
        """Returns empty list when queries array is empty."""
        nd = json.dumps({
            "props": {"pageProps": {"dehydratedState": {"queries": []}}}
        })
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)
        result = await self._run_scrape(html, mock_http)
        assert result == []

    async def test_skips_non_leaderboard_queries(self, mock_http):
        """Non-leaderboard queries (tags, etc.) are skipped."""
        entries = [_make_entry(WALLET_A, "Alice", pnl=100, volume=1000)]
        nd = _make_next_data(
            profit_entries=entries,
            extra_queries=[
                {
                    "queryKey": ["/api/tags", "filteredTags", "12345"],
                    "state": {
                        "data": [
                            {"id": 1, "name": "tag1"},
                            {"id": 2, "name": "tag2"},
                        ],
                    },
                },
            ],
        )
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)
        result = await self._run_scrape(html, mock_http)

        assert len(result) == 1
        assert result[0]["wallet"] == WALLET_A.lower()

    async def test_skips_biggest_wins_query(self, mock_http):
        """biggestWins query is skipped (different structure)."""
        entries = [_make_entry(WALLET_A, "Alice", pnl=100, volume=1000)]
        nd = _make_next_data(
            profit_entries=entries,
            extra_queries=[
                {
                    "queryKey": [
                        "/leaderboard", "biggestWins", "30d", 20, "crypto"
                    ],
                    "state": {
                        "data": [{"someField": "value"}],
                    },
                },
            ],
        )
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)
        result = await self._run_scrape(html, mock_http)
        assert len(result) == 1

    async def test_invalid_wallet_skipped(self, mock_http):
        """Entries without valid 0x wallet are skipped."""
        entries = [
            {"rank": 1, "proxyWallet": "", "name": "NoWallet", "pnl": 100},
            {"rank": 2, "proxyWallet": "invalid", "name": "Bad", "pnl": 50},
            _make_entry(WALLET_A, "Good", pnl=200, volume=500),
        ]
        nd = _make_next_data(profit_entries=entries)
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)
        result = await self._run_scrape(html, mock_http)

        assert len(result) == 1
        assert result[0]["username"] == "Good"

    async def test_http_error_returns_empty(self, mock_http):
        """HTTP errors return empty list."""
        mock_http.get = AsyncMock(side_effect=Exception("Connection refused"))

        with patch(
            "bot.services.polymarket.polymarket_client"
        ) as mock_pm:
            mock_pm._get_http = AsyncMock(return_value=mock_http)
            result = await scrape_leaderboard("crypto")

        assert result == []

    async def test_regex_fallback(self, mock_http):
        """Falls back to regex wallet extraction when no leaderboard queries."""
        # __NEXT_DATA__ exists but with non-leaderboard queries only
        nd = json.dumps({
            "props": {
                "pageProps": {
                    "dehydratedState": {
                        "queries": [
                            {
                                "queryKey": ["/api/tags", "stuff"],
                                "state": {"data": {"not_a_list": True}},
                            },
                        ],
                    },
                },
            },
        })
        html = (
            f'<script id="__NEXT_DATA__" type="application/json">{nd}</script>'
            f"<div>{WALLET_A}</div>"
            f"<div>{WALLET_B}</div>"
        )
        result = await self._run_scrape(html, mock_http)

        assert len(result) == 2

    async def test_amount_field_used_for_profit(self, mock_http):
        """In profit-sorted query, 'amount' is used when 'pnl' is 0."""
        entries = [
            {
                "rank": 1,
                "proxyWallet": WALLET_A,
                "name": "Alice",
                "pseudonym": "A",
                "amount": 5000.0,
                "pnl": 0,
                "volume": 10000.0,
            },
        ]
        nd = _make_next_data(profit_entries=entries)
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)
        result = await self._run_scrape(html, mock_http)

        assert len(result) == 1
        # Should use 'amount' as PNL since 'pnl' is 0 and it's profit query
        assert result[0]["pnl"] == 5000.0


# ── apply_filters ──────────────────────────────────────────────────

class TestApplyFilters:
    """Test filter logic."""

    def _make_trader(self, **kwargs):
        """Create a ScannedTrader with defaults."""
        defaults = {
            "wallet": WALLET_A,
            "pnl_total": 100.0,
            "pnl_1d": 10.0,
            "pnl_1w": 50.0,
            "pnl_1m": 100.0,
            "volume": 5000.0,
            "markets_traded": 20,
            "has_profile_data": True,
        }
        defaults.update(kwargs)
        return ScannedTrader(**defaults)

    def test_no_filters(self):
        """All traders pass with no filters."""
        traders = [self._make_trader(), self._make_trader(wallet=WALLET_B)]
        result = apply_filters(traders, ScanFilters())
        assert len(result) == 2

    def test_pnl_1d_positive_passes(self):
        """Trader with positive 1D PNL passes."""
        traders = [self._make_trader(pnl_1d=50.0)]
        result = apply_filters(
            traders, ScanFilters(pnl_1d_positive=True)
        )
        assert len(result) == 1

    def test_pnl_1d_negative_filtered(self):
        """Trader with negative 1D PNL is filtered out."""
        traders = [self._make_trader(pnl_1d=-50.0)]
        result = apply_filters(
            traders, ScanFilters(pnl_1d_positive=True)
        )
        assert len(result) == 0

    def test_pnl_zero_passes(self):
        """Trader with 0 PNL passes (0 = no data, not loss)."""
        traders = [self._make_trader(pnl_1d=0.0)]
        result = apply_filters(
            traders, ScanFilters(pnl_1d_positive=True)
        )
        assert len(result) == 1

    def test_pnl_all_three_and_logic(self):
        """All three PNL filters use AND logic."""
        # Good on all 3
        good = self._make_trader(pnl_1d=10, pnl_1w=20, pnl_1m=30)
        # Bad on 1D only
        bad_1d = self._make_trader(
            wallet=WALLET_B, pnl_1d=-5, pnl_1w=20, pnl_1m=30
        )
        # Bad on 1M only
        bad_1m = self._make_trader(
            wallet=WALLET_C, pnl_1d=10, pnl_1w=20, pnl_1m=-5
        )

        result = apply_filters(
            [good, bad_1d, bad_1m],
            ScanFilters(
                pnl_1d_positive=True,
                pnl_1w_positive=True,
                pnl_1m_positive=True,
            ),
        )
        assert len(result) == 1
        assert result[0].wallet == WALLET_A

    def test_volume_min_filter(self):
        """Volume minimum filter works."""
        low = self._make_trader(volume=100)
        high = self._make_trader(wallet=WALLET_B, volume=10000)

        result = apply_filters(
            [low, high], ScanFilters(volume_min=5000)
        )
        assert len(result) == 1
        assert result[0].wallet == WALLET_B

    def test_volume_max_filter(self):
        """Volume maximum filter works."""
        low = self._make_trader(volume=100)
        high = self._make_trader(wallet=WALLET_B, volume=10000)

        result = apply_filters(
            [low, high], ScanFilters(volume_max=5000)
        )
        assert len(result) == 1
        assert result[0].wallet == WALLET_A

    def test_volume_range(self):
        """Volume min + max works as range."""
        traders = [
            self._make_trader(volume=100),
            self._make_trader(wallet=WALLET_B, volume=5000),
            self._make_trader(wallet=WALLET_C, volume=100000),
        ]
        result = apply_filters(
            traders, ScanFilters(volume_min=1000, volume_max=50000)
        )
        assert len(result) == 1
        assert result[0].wallet == WALLET_B

    def test_trades_min_filter(self):
        """Minimum trades filter works."""
        few = self._make_trader(markets_traded=5)
        many = self._make_trader(wallet=WALLET_B, markets_traded=50)

        result = apply_filters(
            [few, many], ScanFilters(trades_min=10)
        )
        assert len(result) == 1
        assert result[0].wallet == WALLET_B

    def test_trades_max_filter(self):
        """Maximum trades filter works."""
        few = self._make_trader(markets_traded=5)
        many = self._make_trader(wallet=WALLET_B, markets_traded=50)

        result = apply_filters(
            [few, many], ScanFilters(trades_max=10)
        )
        assert len(result) == 1
        assert result[0].wallet == WALLET_A

    def test_trades_min_no_profile_data_passthrough(self):
        """Trader without profile data bypasses trades_min filter."""
        no_data = self._make_trader(
            markets_traded=0, has_profile_data=False
        )
        result = apply_filters(
            [no_data], ScanFilters(trades_min=10)
        )
        # Should pass because we have no data (can't filter fairly)
        assert len(result) == 1

    def test_combined_filters(self):
        """Multiple filters work together."""
        good = self._make_trader(
            pnl_1d=10, volume=5000, markets_traded=20
        )
        bad_pnl = self._make_trader(
            wallet=WALLET_B, pnl_1d=-5, volume=5000, markets_traded=20
        )
        bad_vol = self._make_trader(
            wallet=WALLET_C, pnl_1d=10, volume=50, markets_traded=20
        )

        result = apply_filters(
            [good, bad_pnl, bad_vol],
            ScanFilters(
                pnl_1d_positive=True,
                volume_min=1000,
            ),
        )
        assert len(result) == 1
        assert result[0].wallet == WALLET_A

    def test_empty_list(self):
        """Empty trader list returns empty."""
        result = apply_filters([], ScanFilters())
        assert result == []

    def test_results_sorted_by_pnl(self):
        """Results are sorted by total PNL descending."""
        t1 = self._make_trader(pnl_total=100)
        t2 = self._make_trader(wallet=WALLET_B, pnl_total=500)
        t3 = self._make_trader(wallet=WALLET_C, pnl_total=200)

        result = apply_filters([t1, t2, t3], ScanFilters())
        assert result[0].pnl_total == 500
        assert result[1].pnl_total == 200
        assert result[2].pnl_total == 100

    def test_none_filter_values_not_applied(self):
        """None filter values mean 'no filter' (don't apply)."""
        trader = self._make_trader(volume=100, markets_traded=5)

        # All None → should pass
        result = apply_filters(
            [trader],
            ScanFilters(
                trades_min=None,
                trades_max=None,
                volume_min=None,
                volume_max=None,
            ),
        )
        assert len(result) == 1


# ── run_scan ───────────────────────────────────────────────────────

class TestRunScan:
    """Test the full scan pipeline."""

    async def test_basic_scan_no_profile_scraping(self):
        """Scan without PNL 1D/1W filters skips profile scraping."""
        entries = [
            _make_entry(WALLET_A, "Alice", pnl=500, volume=10000),
            _make_entry(WALLET_B, "Bob", pnl=-100, volume=5000),
        ]
        nd = _make_next_data(profit_entries=entries)
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)

        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch(
            "bot.services.polymarket.polymarket_client"
        ) as mock_pm:
            mock_pm._get_http = AsyncMock(return_value=mock_http)

            filters = ScanFilters(
                categories=["Crypto"],
                pnl_1m_positive=True,  # Only 1M → use leaderboard data directly
            )
            results = await run_scan(filters)

        # Alice has positive PNL, Bob has negative
        assert len(results) == 1
        assert results[0].username == "Alice"
        assert results[0].pnl_total == 500.0
        assert results[0].pnl_1m == 500.0  # Leaderboard PNL used as 1M proxy

    async def test_scan_with_volume_filter(self):
        """Volume filter applied correctly."""
        entries = [
            _make_entry(WALLET_A, "Alice", pnl=500, volume=10000),
            _make_entry(WALLET_B, "Bob", pnl=300, volume=500),
        ]
        nd = _make_next_data(profit_entries=entries)
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)

        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch(
            "bot.services.polymarket.polymarket_client"
        ) as mock_pm:
            mock_pm._get_http = AsyncMock(return_value=mock_http)

            filters = ScanFilters(
                categories=["Crypto"],
                volume_min=1000,
            )
            results = await run_scan(filters)

        assert len(results) == 1
        assert results[0].username == "Alice"

    async def test_scan_empty_leaderboard(self):
        """Empty leaderboard returns empty results."""
        nd = _make_next_data(profit_entries=[], volume_entries=[])
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)

        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch(
            "bot.services.polymarket.polymarket_client"
        ) as mock_pm:
            mock_pm._get_http = AsyncMock(return_value=mock_http)

            filters = ScanFilters(categories=["Crypto"])
            results = await run_scan(filters)

        assert results == []

    async def test_scan_with_profile_enrichment(self):
        """When 1D PNL filter is on, profiles are scraped and merged."""
        entries = [
            _make_entry(WALLET_A, "Alice", pnl=500, volume=10000),
        ]
        nd = _make_next_data(profit_entries=entries)
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)

        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        # Mock profile scraping
        mock_profile = ScannedTrader(
            wallet=WALLET_A.lower(),
            username="Alice",
            pnl_total=500,
            pnl_1d=25.0,
            pnl_1w=150.0,
            pnl_1m=500.0,
            volume=10000,
            has_profile_data=True,
        )

        with patch(
            "bot.services.polymarket.polymarket_client"
        ) as mock_pm:
            mock_pm._get_http = AsyncMock(return_value=mock_http)

            with patch(
                "bot.services.scanner.scan_trader_profile",
                return_value=mock_profile,
            ):
                filters = ScanFilters(
                    categories=["Crypto"],
                    pnl_1d_positive=True,
                )
                results = await run_scan(filters)

        assert len(results) == 1
        assert results[0].pnl_1d == 25.0
        assert results[0].has_profile_data is True

    async def test_scan_profile_failure_uses_leaderboard_data(self):
        """When profile scraping fails, leaderboard data is used."""
        entries = [
            _make_entry(WALLET_A, "Alice", pnl=500, volume=10000),
        ]
        nd = _make_next_data(profit_entries=entries)
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)

        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with patch(
            "bot.services.polymarket.polymarket_client"
        ) as mock_pm:
            mock_pm._get_http = AsyncMock(return_value=mock_http)

            # Profile scraping fails
            with patch(
                "bot.services.scanner.scan_trader_profile",
                return_value=None,
            ):
                filters = ScanFilters(
                    categories=["Crypto"],
                    pnl_1d_positive=True,  # Triggers profile scraping
                )
                results = await run_scan(filters)

        # Trader should still be in results with leaderboard data
        assert len(results) == 1
        assert results[0].pnl_total == 500.0
        assert results[0].pnl_1d == 0.0  # No profile data
        assert results[0].has_profile_data is False

    async def test_scan_progress_callback(self):
        """Progress callback is called correctly."""
        entries = [_make_entry(WALLET_A, "Alice", pnl=100, volume=1000)]
        nd = _make_next_data(profit_entries=entries)
        html = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd)

        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        callback = AsyncMock()

        with patch(
            "bot.services.polymarket.polymarket_client"
        ) as mock_pm:
            mock_pm._get_http = AsyncMock(return_value=mock_http)

            filters = ScanFilters(categories=["Crypto"])
            await run_scan(filters, progress_callback=callback)

        # Should be called at least for leaderboard scraping + filtering
        assert callback.call_count >= 2

    async def test_scan_multiple_categories(self):
        """Scanning multiple categories merges results."""
        entries_a = [_make_entry(WALLET_A, "Alice", pnl=100, volume=1000)]
        entries_b = [_make_entry(WALLET_B, "Bob", pnl=200, volume=2000)]

        nd_a = _make_next_data(profit_entries=entries_a)
        nd_b = _make_next_data(profit_entries=entries_b)

        html_a = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd_a)
        html_b = SAMPLE_LEADERBOARD_HTML.replace("{NEXT_DATA}", nd_b)

        call_count = 0

        async def _get_response(*args, **kwargs):
            nonlocal call_count
            url = args[0] if args else kwargs.get("url", "")
            call_count += 1

            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()

            # Return different HTML based on URL
            if "crypto" in str(url):
                mock_resp.text = html_a
            else:
                mock_resp.text = html_b
            return mock_resp

        mock_http = AsyncMock()
        mock_http.get = _get_response

        with patch(
            "bot.services.polymarket.polymarket_client"
        ) as mock_pm:
            mock_pm._get_http = AsyncMock(return_value=mock_http)

            filters = ScanFilters(categories=["Crypto", "Politics"])
            results = await run_scan(filters)

        assert len(results) == 2
