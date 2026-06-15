"""
TRON-X Crypto Feed  --  Phase 15

Provider: CoinGecko public API (completely free, no API key required)
  Endpoints used:
    /simple/price           -- live prices
    /coins/{id}             -- market data for one coin
    /coins/markets          -- top N coins by market cap
    /search/trending        -- trending in last 24h
    /search                 -- coin search by name/symbol

TTL cache: 2 minutes for prices, 5 minutes for market data
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from src.core.logger import log

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
PRICE_TTL      = 120   # 2 minutes
MARKET_TTL     = 300   # 5 minutes
TRENDING_TTL   = 300

_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: int) -> Any | None:
    if key in _cache:
        ts, val = _cache[key]
        if time.monotonic() - ts < ttl:
            return val
    return None


def _store(key: str, val: Any) -> None:
    _cache[key] = (time.monotonic(), val)


async def _http_get(url: str, params: dict | None = None) -> Any:
    try:
        import aiohttp
    except ImportError:
        import subprocess, sys
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "aiohttp",
             "--break-system-packages", "--quiet"], check=True
        )
        import aiohttp

    headers = {"Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            r.raise_for_status()
            return await r.json()


# Common symbol -> CoinGecko ID shortcuts
_SYMBOL_MAP = {
    "btc":  "bitcoin",      "eth":  "ethereum",
    "bnb":  "binancecoin",  "sol":  "solana",
    "xrp":  "ripple",       "ada":  "cardano",
    "doge": "dogecoin",     "dot":  "polkadot",
    "matic":"matic-network","link": "chainlink",
    "usdt": "tether",       "usdc": "usd-coin",
    "avax": "avalanche-2",  "trx":  "tron",
    "ltc":  "litecoin",     "uni":  "uniswap",
}

def _resolve_id(coin: str) -> str:
    """Resolve ticker symbol to CoinGecko coin ID if possible."""
    return _SYMBOL_MAP.get(coin.lower(), coin.lower())


# ---------------------------------------------------------------------------
# CryptoFeed
# ---------------------------------------------------------------------------

class CryptoFeed:
    """Real-time crypto data via CoinGecko (no API key needed)."""

    async def price(
        self,
        coin: str,
        vs_currency: str = "usd",
    ) -> dict:
        """
        Simple price for one or more coins.

        coin: CoinGecko ID (e.g. 'bitcoin') or ticker ('BTC')
        vs_currency: target currency (usd, eur, gbp, btc, eth...)

        Returns:
            {
              "coin":         str,
              "vs_currency":  str,
              "price":        float,
              "change_24h":   float,  # percent
              "market_cap":   float,
              "volume_24h":   float,
              "provider":     str,
            }
        """
        coin_id = _resolve_id(coin)
        vs      = vs_currency.lower()
        key     = f"price:{coin_id}:{vs}"
        if (cached := _cached(key, PRICE_TTL)):
            return cached

        try:
            data = await _http_get(
                f"{COINGECKO_BASE}/simple/price",
                params={
                    "ids":                  coin_id,
                    "vs_currencies":        vs,
                    "include_market_cap":   "true",
                    "include_24hr_vol":     "true",
                    "include_24hr_change":  "true",
                }
            )
            coin_data = data.get(coin_id, {})
            if not coin_data:
                return {"error": f"Coin not found: '{coin}'"}

            result = {
                "coin":        coin_id,
                "symbol":      coin.upper(),
                "vs_currency": vs,
                "price":       coin_data.get(vs, 0),
                "change_24h":  round(coin_data.get(f"{vs}_24h_change", 0), 2),
                "market_cap":  coin_data.get(f"{vs}_market_cap", 0),
                "volume_24h":  coin_data.get(f"{vs}_24h_vol", 0),
                "provider":    "coingecko",
            }
            _store(key, result)
            return result
        except Exception as e:
            log.warning("[crypto] price failed for %s: %s", coin, e)
            return {"error": str(e)}

    async def market_data(self, coin: str) -> dict:
        """
        Detailed market data for a single coin.

        Returns:
            {
              "id":            str,   "symbol": str, "name": str,
              "price_usd":     float, "market_cap": float,
              "rank":          int,   "volume_24h": float,
              "change_1h":     float, "change_24h": float, "change_7d": float,
              "ath":           float, "ath_date": str,
              "atl":           float, "atl_date": str,
              "circulating_supply": float,
              "total_supply":  float | None,
              "max_supply":    float | None,
              "description":   str,
              "homepage":      str,
              "provider":      str,
            }
        """
        coin_id = _resolve_id(coin)
        key     = f"market:{coin_id}"
        if (cached := _cached(key, MARKET_TTL)):
            return cached

        try:
            data = await _http_get(
                f"{COINGECKO_BASE}/coins/{coin_id}",
                params={
                    "localization":        "false",
                    "tickers":             "false",
                    "market_data":         "true",
                    "community_data":      "false",
                    "developer_data":      "false",
                }
            )
            md  = data.get("market_data", {})
            result = {
                "id":                 data.get("id", coin_id),
                "symbol":             data.get("symbol", "").upper(),
                "name":               data.get("name", ""),
                "price_usd":          md.get("current_price", {}).get("usd", 0),
                "market_cap":         md.get("market_cap", {}).get("usd", 0),
                "rank":               data.get("market_cap_rank", 0),
                "volume_24h":         md.get("total_volume", {}).get("usd", 0),
                "change_1h":          round(md.get("price_change_percentage_1h_in_currency", {}).get("usd", 0) or 0, 2),
                "change_24h":         round(md.get("price_change_percentage_24h", 0) or 0, 2),
                "change_7d":          round(md.get("price_change_percentage_7d", 0) or 0, 2),
                "ath":                md.get("ath", {}).get("usd", 0),
                "ath_date":           (md.get("ath_date", {}).get("usd") or "")[:10],
                "atl":                md.get("atl", {}).get("usd", 0),
                "atl_date":           (md.get("atl_date", {}).get("usd") or "")[:10],
                "circulating_supply": md.get("circulating_supply", 0),
                "total_supply":       md.get("total_supply"),
                "max_supply":         md.get("max_supply"),
                "description":        (data.get("description", {}).get("en", "") or "")[:500],
                "homepage":           next(iter(data.get("links", {}).get("homepage", [])), ""),
                "provider":           "coingecko",
            }
            _store(key, result)
            return result
        except Exception as e:
            log.warning("[crypto] market_data failed for %s: %s", coin, e)
            return {"error": str(e)}

    async def top(self, limit: int = 10, vs_currency: str = "usd") -> list[dict]:
        """
        Top N coins by market cap.

        Returns list of:
            {
              "rank": int, "id": str, "symbol": str, "name": str,
              "price": float, "market_cap": float,
              "change_24h": float, "volume_24h": float,
            }
        """
        limit  = max(1, min(limit, 250))
        vs     = vs_currency.lower()
        key    = f"top:{limit}:{vs}"
        if (cached := _cached(key, PRICE_TTL)):
            return cached

        try:
            data = await _http_get(
                f"{COINGECKO_BASE}/coins/markets",
                params={
                    "vs_currency":            vs,
                    "order":                  "market_cap_desc",
                    "per_page":               limit,
                    "page":                   1,
                    "sparkline":              "false",
                    "price_change_percentage": "24h",
                }
            )
            result = [
                {
                    "rank":       c.get("market_cap_rank", i + 1),
                    "id":         c.get("id", ""),
                    "symbol":     c.get("symbol", "").upper(),
                    "name":       c.get("name", ""),
                    "price":      c.get("current_price", 0),
                    "market_cap": c.get("market_cap", 0),
                    "change_24h": round(c.get("price_change_percentage_24h", 0) or 0, 2),
                    "volume_24h": c.get("total_volume", 0),
                }
                for i, c in enumerate(data)
            ]
            _store(key, result)
            return result
        except Exception as e:
            log.warning("[crypto] top failed: %s", e)
            return []

    async def trending(self) -> list[dict]:
        """
        Coins trending on CoinGecko in the last 24h.

        Returns list of:
            {"rank": int, "id": str, "symbol": str, "name": str, "score": int}
        """
        key = "trending"
        if (cached := _cached(key, TRENDING_TTL)):
            return cached

        try:
            data = await _http_get(f"{COINGECKO_BASE}/search/trending")
            result = [
                {
                    "rank":   c["item"].get("score", i),
                    "id":     c["item"].get("id", ""),
                    "symbol": c["item"].get("symbol", "").upper(),
                    "name":   c["item"].get("name", ""),
                    "score":  c["item"].get("score", 0),
                }
                for i, c in enumerate(data.get("coins", []))
            ]
            _store(key, result)
            return result
        except Exception as e:
            log.warning("[crypto] trending failed: %s", e)
            return []

    async def search(self, query: str) -> list[dict]:
        """Search coins by name or ticker symbol."""
        key = f"search:{query.lower()}"
        if (cached := _cached(key, 3600)):
            return cached

        try:
            data = await _http_get(
                f"{COINGECKO_BASE}/search", params={"query": query}
            )
            result = [
                {
                    "id":     c.get("id", ""),
                    "symbol": c.get("symbol", "").upper(),
                    "name":   c.get("name", ""),
                    "rank":   c.get("market_cap_rank") or 9999,
                }
                for c in (data.get("coins") or [])[:15]
            ]
            _store(key, result)
            return result
        except Exception as e:
            log.warning("[crypto] search failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_feed: CryptoFeed | None = None

def get_crypto_feed() -> CryptoFeed:
    global _feed
    if _feed is None:
        _feed = CryptoFeed()
    return _feed
