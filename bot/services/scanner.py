"""Trader Scanner — scrape Polymarket leaderboard and filter traders by criteria.

Fetches leaderboard by category, extracts trader data (PNL, volume) directly
from the leaderboard page, then optionally enriches with profile data for
per-timeframe PNL (1D, 1W, 1M).

Key design decisions:
- Leaderboard data is used DIRECTLY to create ScannedTrader objects (fast, no extra requests)
- Profile scraping is done only for top candidates (slow, can fail due to rate limiting)
- If profile scraping fails, leaderboard data is used as fallback
- PNL filter treats 0.0 as "no data" and does NOT exclude (uses < 0 not <= 0)
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Available leaderboard categories with their URL slugs
LEADERBOARD_CATEGORIES: dict[str, str] = {
    "All": "",
    "Crypto": "crypto",
    "Politics": "politics",
    "Sports": "sports",
    "Finance": "finance",
    "Tech": "tech",
    "Economy": "economy",
    "Geopolitics": "geopolitics",
    "Culture": "culture",
    "Weather": "weather",
    "Elections": "elections",
}


@dataclass
class ScanFilters:
    """User-configured filters for the scanner."""
    categories: list[str] = field(default_factory=lambda: ["Crypto"])
    period: str = "all"  # Leaderboard period (auto-determined from PNL filters)
    # PNL filters (True = must be positive, False = no filter)
    pnl_1d_positive: bool = False
    pnl_1w_positive: bool = False
    pnl_1m_positive: bool = False
    # Trade count filters (None = no filter)
    trades_min: Optional[int] = None
    trades_max: Optional[int] = None
    # Volume filters (None = no filter)
    volume_min: Optional[float] = None
    volume_max: Optional[float] = None
    # Max results to analyze (profile scraping is slow)
    max_profiles: int = 30


@dataclass
class ScannedTrader:
    """Result for a scanned trader."""
    wallet: str
    username: str = ""
    pseudonym: str = ""
    pnl_total: float = 0.0
    pnl_1d: float = 0.0
    pnl_1w: float = 0.0
    pnl_1m: float = 0.0
    volume: float = 0.0
    markets_traded: int = 0
    positions_value: float = 0.0
    largest_win: float = 0.0
    leaderboard_rank: int = 0
    leaderboard_category: str = ""
    has_profile_data: bool = False  # True if profile scraping succeeded
    # Computed
    pnl_volume_ratio: float = 0.0  # PNL / Volume (efficiency)


async def scrape_leaderboard(
    category_slug: str = "",
    period: str = "1m",
    max_results: int = 50,
) -> list[dict]:
    """Scrape Polymarket leaderboard page for trader wallets, PNL, and volume.

    Parses __NEXT_DATA__ from the leaderboard page. Extracts data from BOTH
    the profit-sorted and volume-sorted queries to get complete data per wallet.

    Returns list of dicts: {rank, username, pseudonym, wallet, pnl, volume}
    """
    from bot.services.polymarket import polymarket_client

    http = await polymarket_client._get_http()

    # Build URL
    if category_slug:
        url = f"https://polymarket.com/leaderboard/{category_slug}?period={period}"
    else:
        url = f"https://polymarket.com/leaderboard?period={period}"

    logger.info(f"Scraping leaderboard: {url}")

    try:
        resp = await http.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=25,
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.error(f"Failed to fetch leaderboard {category_slug}/{period}: {e}")
        return []

    # Extract __NEXT_DATA__ JSON
    match = re.search(
        r'<script\s+id="__NEXT_DATA__"\s+type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        logger.warning(
            f"No __NEXT_DATA__ found in leaderboard page "
            f"(HTML length: {len(html)}, starts with: {html[:200]})"
        )
        return []

    try:
        next_data = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse __NEXT_DATA__: {e}")
        return []

    # Navigate the React Query dehydrated state
    queries = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )

    if not queries:
        logger.warning("No queries found in dehydratedState")
        return []

    logger.info(f"Found {len(queries)} queries in dehydratedState")

    # Collect traders from ALL leaderboard queries (profit + volume sorted)
    # Merge by wallet to get both PNL and volume data
    wallet_data: dict[str, dict] = {}  # wallet → merged data

    for query in queries:
        qkey = query.get("queryKey", [])
        qkey_str = str(qkey)

        # Only process leaderboard queries
        if "/leaderboard" not in qkey_str:
            continue

        # Skip "biggestWins" queries (different structure)
        if "biggestWins" in qkey_str:
            continue

        state = query.get("state", {})
        data = state.get("data", None)

        if not isinstance(data, list):
            logger.debug(
                f"Leaderboard query {qkey} data is not a list: {type(data)}"
            )
            continue

        # Determine sort type from queryKey
        # queryKey format: ["/leaderboard", "profit"|"volume", "30d", page, "crypto", null]
        sort_type = qkey[1] if len(qkey) > 1 else "unknown"

        logger.info(
            f"Processing leaderboard query: sort={sort_type}, entries={len(data)}"
        )

        for i, entry in enumerate(data[:max_results]):
            if not isinstance(entry, dict):
                continue

            wallet = entry.get("proxyWallet", entry.get("address", ""))
            if not wallet or not wallet.startswith("0x"):
                continue

            wallet = wallet.lower()

            if wallet not in wallet_data:
                wallet_data[wallet] = {
                    "rank": entry.get("rank", i + 1),
                    "username": entry.get("name", ""),
                    "pseudonym": entry.get("pseudonym", ""),
                    "wallet": wallet,
                    "pnl": 0.0,
                    "volume": 0.0,
                }

            # The "amount" field contains the sort metric:
            # - In profit-sorted query: amount = profit
            # - In volume-sorted query: amount = volume
            # The "pnl" and "volume" fields are always present too
            entry_pnl = _safe_float(entry.get("pnl", 0))
            entry_volume = _safe_float(entry.get("volume", 0))
            entry_amount = _safe_float(entry.get("amount", 0))

            # Always use pnl and volume fields when available
            if entry_pnl != 0:
                wallet_data[wallet]["pnl"] = entry_pnl
            if entry_volume != 0:
                wallet_data[wallet]["volume"] = entry_volume

            # If PNL is still 0 but amount is non-zero in profit query, use amount
            if wallet_data[wallet]["pnl"] == 0 and sort_type == "profit":
                wallet_data[wallet]["pnl"] = entry_amount

            # If volume is still 0 but amount is non-zero in volume query, use amount
            if wallet_data[wallet]["volume"] == 0 and sort_type == "volume":
                wallet_data[wallet]["volume"] = entry_amount

            # Update username/pseudonym if not set
            if not wallet_data[wallet]["username"]:
                wallet_data[wallet]["username"] = entry.get("name", "")
            if not wallet_data[wallet]["pseudonym"]:
                wallet_data[wallet]["pseudonym"] = entry.get("pseudonym", "")

    traders = list(wallet_data.values())

    # Fallback: regex for wallet addresses if __NEXT_DATA__ parsing failed
    if not traders:
        logger.warning("No traders from __NEXT_DATA__, trying regex fallback")
        wallet_pattern = re.compile(r'0x[a-fA-F0-9]{40}')
        wallets_found = list(set(wallet_pattern.findall(html)))
        for i, w in enumerate(wallets_found[:max_results]):
            traders.append({
                "rank": i + 1,
                "username": "",
                "pseudonym": "",
                "wallet": w.lower(),
                "pnl": 0,
                "volume": 0,
            })

    logger.info(
        f"Leaderboard {category_slug or 'all'}/{period}: "
        f"found {len(traders)} unique traders"
    )

    # Log sample data for debugging
    if traders:
        sample = traders[0]
        logger.info(
            f"Sample trader: {sample['username'] or sample['wallet'][:10]}, "
            f"PNL={sample['pnl']:.2f}, Volume={sample['volume']:.2f}"
        )

    return traders


async def scan_trader_profile(wallet: str) -> Optional[ScannedTrader]:
    """Fetch detailed profile stats for a trader.

    Uses get_trader_profile() for per-timeframe PNL data (1D, 1W, 1M).
    Returns None if profile scraping fails entirely.
    """
    from bot.services.polymarket import polymarket_client

    try:
        profile = await polymarket_client.get_trader_profile(wallet)
    except Exception as e:
        logger.warning(f"Profile scraping exception for {wallet[:10]}...: {e}")
        return None

    if not profile:
        return None

    trader = ScannedTrader(
        wallet=wallet,
        username=profile.username,
        pseudonym=profile.pseudonym,
        pnl_total=profile.pnl_total,
        pnl_1d=profile.pnl_1d,
        pnl_1w=profile.pnl_1w,
        pnl_1m=profile.pnl_1m,
        volume=profile.volume,
        markets_traded=profile.markets_traded,
        positions_value=profile.positions_value,
        largest_win=profile.biggest_win,
        has_profile_data=True,
    )

    # Calculate efficiency ratio
    if trader.volume > 0:
        trader.pnl_volume_ratio = trader.pnl_total / trader.volume * 100

    return trader


def apply_filters(
    traders: list[ScannedTrader], filters: ScanFilters
) -> list[ScannedTrader]:
    """Apply user filters to a list of scanned traders.

    PNL filter logic:
    - If PNL < 0 → filtered out (confirmed loss)
    - If PNL == 0 → kept (no data available or break-even, benefit of the doubt)
    - If PNL > 0 → kept (confirmed gain)
    """
    results = []

    for t in traders:
        # PNL positive checks (AND logic) — use < 0, NOT <= 0
        # 0 means "no data" not "loss", so we keep the trader
        if filters.pnl_1d_positive and t.pnl_1d < 0:
            continue
        if filters.pnl_1w_positive and t.pnl_1w < 0:
            continue
        if filters.pnl_1m_positive and t.pnl_1m < 0:
            continue

        # Trade count filters
        if filters.trades_min is not None and t.markets_traded < filters.trades_min:
            # If no profile data and markets_traded is 0, skip this filter
            if t.has_profile_data or t.markets_traded > 0:
                continue
        if filters.trades_max is not None and t.markets_traded > filters.trades_max:
            continue

        # Volume filters
        if filters.volume_min is not None and t.volume < filters.volume_min:
            continue
        if filters.volume_max is not None and t.volume > filters.volume_max:
            continue

        results.append(t)

    # Sort by PNL total descending (most profitable first)
    results.sort(key=lambda t: t.pnl_total, reverse=True)

    return results


async def run_scan(
    filters: ScanFilters,
    progress_callback=None,
) -> list[ScannedTrader]:
    """Run a full scan: scrape leaderboard(s), fetch profiles, apply filters.

    Flow:
    1. Scrape leaderboard for each category → get wallets + PNL + volume
    2. Create ScannedTrader from leaderboard data (FAST, no extra requests)
    3. Pre-filter with leaderboard data (volume, basic PNL)
    4. For PNL timeframe filters (1D/1W/1M): enrich top candidates with profile data
    5. Apply final filters

    Args:
        filters: User-configured scan filters
        progress_callback: async callable(current, total, message) for progress updates

    Returns:
        Filtered and sorted list of ScannedTrader
    """
    all_wallets: dict[str, dict] = {}  # wallet → leaderboard info

    # Step 1: Scrape leaderboard for each selected category
    for cat_name in filters.categories:
        slug = LEADERBOARD_CATEGORIES.get(cat_name, "")
        if progress_callback:
            await progress_callback(
                0, 0, f"📡 Scraping leaderboard {cat_name}…"
            )

        entries = await scrape_leaderboard(
            category_slug=slug,
            period=filters.period,
            max_results=filters.max_profiles,
        )

        for entry in entries:
            wallet = entry["wallet"].lower()
            if wallet not in all_wallets:
                all_wallets[wallet] = entry
                all_wallets[wallet]["category"] = cat_name

    total_wallets = len(all_wallets)
    logger.info(f"Leaderboard scraping done: {total_wallets} unique wallets")

    if total_wallets == 0:
        if progress_callback:
            await progress_callback(0, 0, "❌ Aucun trader trouvé sur le leaderboard")
        return []

    if progress_callback:
        await progress_callback(
            0, total_wallets,
            f"📋 {total_wallets} traders trouvés"
        )

    # Step 2: Create ScannedTrader from leaderboard data (instant, no HTTP)
    scanned: list[ScannedTrader] = []
    for wallet, info in all_wallets.items():
        lb_pnl = float(info.get("pnl", 0) or 0)
        lb_vol = float(info.get("volume", 0) or 0)

        trader = ScannedTrader(
            wallet=wallet,
            username=info.get("username", ""),
            pseudonym=info.get("pseudonym", ""),
            pnl_total=lb_pnl,
            # Use leaderboard PNL as proxy for 1M (leaderboard = ~30 days)
            pnl_1m=lb_pnl,
            volume=lb_vol,
            leaderboard_rank=info.get("rank", 0),
            leaderboard_category=info.get("category", ""),
            has_profile_data=False,
        )

        # Calculate efficiency ratio
        if trader.volume > 0:
            trader.pnl_volume_ratio = trader.pnl_total / trader.volume * 100

        scanned.append(trader)

    # Step 3: Pre-filter with leaderboard data (volume only for now)
    pre_filtered = []
    for t in scanned:
        if filters.volume_min is not None and t.volume < filters.volume_min:
            continue
        if filters.volume_max is not None and t.volume > filters.volume_max:
            continue
        pre_filtered.append(t)

    logger.info(
        f"Pre-filter: {len(scanned)} → {len(pre_filtered)} "
        f"(volume min={filters.volume_min}, max={filters.volume_max})"
    )

    # Step 4: Enrich with profile data for PNL timeframe filters
    # Only scrape profiles if 1D or 1W PNL filters are enabled
    # (1M is already approximated from leaderboard data)
    needs_profile = filters.pnl_1d_positive or filters.pnl_1w_positive

    if needs_profile and pre_filtered:
        # Limit profile scraping to top candidates (sorted by leaderboard PNL)
        candidates = sorted(pre_filtered, key=lambda t: t.pnl_total, reverse=True)
        to_scrape = candidates[:min(len(candidates), filters.max_profiles)]

        if progress_callback:
            await progress_callback(
                0, len(to_scrape),
                f"👤 Enrichissement des profils ({len(to_scrape)} traders)…"
            )

        semaphore = asyncio.Semaphore(3)  # Conservative: 3 concurrent
        done = 0

        async def _enrich_one(trader: ScannedTrader):
            nonlocal done
            async with semaphore:
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.5)

                profile = await scan_trader_profile(trader.wallet)
                done += 1

                if profile:
                    # Merge profile data into existing trader
                    trader.pnl_1d = profile.pnl_1d
                    trader.pnl_1w = profile.pnl_1w
                    trader.pnl_1m = profile.pnl_1m
                    trader.pnl_total = profile.pnl_total or trader.pnl_total
                    trader.markets_traded = profile.markets_traded
                    trader.positions_value = profile.positions_value
                    trader.largest_win = profile.largest_win
                    trader.has_profile_data = True
                    if profile.volume > 0:
                        trader.volume = profile.volume
                    if profile.username:
                        trader.username = profile.username
                    if profile.pseudonym:
                        trader.pseudonym = profile.pseudonym
                    # Recalculate efficiency
                    if trader.volume > 0:
                        trader.pnl_volume_ratio = (
                            trader.pnl_total / trader.volume * 100
                        )

                    logger.debug(
                        f"Profile enriched: {trader.username or trader.wallet[:10]}, "
                        f"1D={trader.pnl_1d:+.0f}, 1W={trader.pnl_1w:+.0f}, "
                        f"1M={trader.pnl_1m:+.0f}"
                    )
                else:
                    logger.debug(
                        f"Profile scraping failed for {trader.wallet[:10]}… "
                        f"— using leaderboard data as fallback"
                    )

                if progress_callback and done % 3 == 0:
                    await progress_callback(
                        done, len(to_scrape),
                        f"👤 {done}/{len(to_scrape)} profils analysés…"
                    )

        tasks = [_enrich_one(t) for t in to_scrape]
        await asyncio.gather(*tasks, return_exceptions=True)

        enriched_count = sum(1 for t in to_scrape if t.has_profile_data)
        logger.info(
            f"Profile enrichment: {enriched_count}/{len(to_scrape)} succeeded"
        )

    if progress_callback:
        await progress_callback(
            len(pre_filtered), len(pre_filtered),
            f"🔍 Filtrage de {len(pre_filtered)} traders…"
        )

    # Step 5: Apply ALL filters
    results = apply_filters(pre_filtered, filters)

    logger.info(
        f"Scan complete: {total_wallets} wallets → "
        f"{len(pre_filtered)} pre-filtered → {len(results)} final results"
    )

    return results


def _safe_float(val) -> float:
    """Safely convert a value to float."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
