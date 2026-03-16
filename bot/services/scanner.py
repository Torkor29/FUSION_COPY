"""Trader Scanner — scrape Polymarket leaderboard and filter traders by criteria.

Fetches leaderboard by category, then scrapes each trader's profile to get
detailed PNL (1D, 1W, 1M), volume, markets traded, and positions.
"""

import asyncio
import logging
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

# Available leaderboard periods
LEADERBOARD_PERIODS: dict[str, str] = {
    "1D": "1d",
    "1W": "1w",
    "1M": "1m",
    "All": "all",
}


@dataclass
class ScanFilters:
    """User-configured filters for the scanner."""
    categories: list[str] = field(default_factory=lambda: ["Crypto"])
    period: str = "all"  # Leaderboard period to scrape (auto-determined from PNL filters)
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
    # Computed
    pnl_volume_ratio: float = 0.0  # PNL / Volume (efficiency)


async def scrape_leaderboard(
    category_slug: str = "",
    period: str = "1w",
    max_results: int = 50,
) -> list[dict]:
    """Scrape Polymarket leaderboard page for trader wallets and basic PNL.

    Returns list of dicts: {rank, username, wallet, pnl, volume}
    """
    import re

    from bot.services.polymarket import polymarket_client

    http = await polymarket_client._get_http()

    # Build URL
    if category_slug:
        url = f"https://polymarket.com/leaderboard/{category_slug}?period={period}"
    else:
        url = f"https://polymarket.com/leaderboard?period={period}"

    try:
        resp = await http.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
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
        logger.warning("No __NEXT_DATA__ found in leaderboard page")
        return []

    import json

    try:
        next_data = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse __NEXT_DATA__: {e}")
        return []

    # Navigate the React Query dehydrated state to find leaderboard data
    traders = []
    try:
        queries = next_data.get("props", {}).get("pageProps", {}).get(
            "dehydratedState", {}
        ).get("queries", [])

        for query in queries:
            state = query.get("state", {})
            data = state.get("data", None)
            if not isinstance(data, list):
                continue

            for i, entry in enumerate(data[:max_results]):
                if not isinstance(entry, dict):
                    continue

                wallet = entry.get("address", entry.get("proxyWallet", ""))
                if not wallet or not wallet.startswith("0x"):
                    continue

                traders.append({
                    "rank": i + 1,
                    "username": entry.get("username", entry.get("name", "")),
                    "wallet": wallet,
                    "pnl": float(entry.get("pnl", entry.get("profit", 0)) or 0),
                    "volume": float(entry.get("volume", 0) or 0),
                })

            if traders:
                break  # Found the leaderboard data

    except Exception as e:
        logger.error(f"Failed to parse leaderboard data: {e}")

    # Fallback: try regex parsing if __NEXT_DATA__ didn't yield results
    if not traders:
        # Look for wallet addresses in the HTML
        wallet_pattern = re.compile(r'0x[a-fA-F0-9]{40}')
        wallets_found = list(set(wallet_pattern.findall(html)))
        for i, w in enumerate(wallets_found[:max_results]):
            traders.append({
                "rank": i + 1,
                "username": "",
                "wallet": w.lower(),
                "pnl": 0,
                "volume": 0,
            })

    logger.info(
        f"Leaderboard {category_slug or 'all'}/{period}: "
        f"found {len(traders)} traders"
    )
    return traders


async def scan_trader_profile(wallet: str) -> Optional[ScannedTrader]:
    """Fetch detailed profile stats for a trader.

    Uses the same scraping logic as get_trader_profile but returns
    a ScannedTrader with all fields populated.
    """
    from bot.services.polymarket import polymarket_client

    profile = await polymarket_client.get_trader_profile(wallet)
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
    )

    # Calculate efficiency ratio
    if trader.volume > 0:
        trader.pnl_volume_ratio = trader.pnl_total / trader.volume * 100

    return trader


def apply_filters(traders: list[ScannedTrader], filters: ScanFilters) -> list[ScannedTrader]:
    """Apply user filters to a list of scanned traders."""
    results = []

    for t in traders:
        # PNL positive checks (AND logic)
        if filters.pnl_1d_positive and t.pnl_1d <= 0:
            continue
        if filters.pnl_1w_positive and t.pnl_1w <= 0:
            continue
        if filters.pnl_1m_positive and t.pnl_1m <= 0:
            continue

        # Trade count filters
        if filters.trades_min is not None and t.markets_traded < filters.trades_min:
            continue
        if filters.trades_max is not None and t.markets_traded > filters.trades_max:
            continue

        # Volume filters
        if filters.volume_min is not None and t.volume < filters.volume_min:
            continue
        if filters.volume_max is not None and t.volume > filters.volume_max:
            continue

        results.append(t)

    # Sort by PNL volume ratio (efficiency) descending
    results.sort(key=lambda t: t.pnl_volume_ratio, reverse=True)

    return results


async def run_scan(
    filters: ScanFilters,
    progress_callback=None,
) -> list[ScannedTrader]:
    """Run a full scan: scrape leaderboard(s), fetch profiles, apply filters.

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

    if progress_callback:
        await progress_callback(
            0, len(all_wallets),
            f"📋 {len(all_wallets)} traders trouvés, analyse des profils…"
        )

    # Step 2: Fetch profiles (with concurrency limit)
    semaphore = asyncio.Semaphore(5)  # Max 5 concurrent profile fetches
    scanned: list[ScannedTrader] = []
    done = 0
    total = min(len(all_wallets), filters.max_profiles)

    async def _fetch_one(wallet: str, info: dict):
        nonlocal done
        async with semaphore:
            trader = await scan_trader_profile(wallet)
            done += 1
            if trader:
                trader.leaderboard_rank = info.get("rank", 0)
                trader.leaderboard_category = info.get("category", "")
                scanned.append(trader)
            if progress_callback and done % 5 == 0:
                await progress_callback(
                    done, total,
                    f"👤 {done}/{total} profils analysés…"
                )

    tasks = []
    for wallet, info in list(all_wallets.items())[:total]:
        tasks.append(_fetch_one(wallet, info))

    await asyncio.gather(*tasks, return_exceptions=True)

    if progress_callback:
        await progress_callback(
            total, total,
            f"🔍 Filtrage de {len(scanned)} profils…"
        )

    # Step 3: Apply filters
    results = apply_filters(scanned, filters)

    logger.info(
        f"Scan complete: {len(all_wallets)} wallets → "
        f"{len(scanned)} profiles → {len(results)} after filters"
    )

    return results
