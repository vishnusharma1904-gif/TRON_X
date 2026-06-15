"""
TRON-X Stock Feed  --  Phase 15

Providers:
  Primary:  yfinance (Yahoo Finance, free, no key needed)
  Optional: Alpha Vantage (fundamentals, requires ALPHA_VANTAGE_KEY)

TTL cache: 1 minute for quotes, 5 minutes for history
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from src.core.logger import log

QUOTE_TTL   = 60     # 1 minute
HISTORY_TTL = 300    # 5 minutes

_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: int) -> Any | None:
    if key in _cache:
        ts, val = _cache[key]
        if time.monotonic() - ts < ttl:
            return val
    return None


def _store(key: str, val: Any) -> None:
    _cache[key] = (time.monotonic(), val)


def _ensure_yfinance():
    try:
        import yfinance
        return yfinance
    except ImportError:
        import subprocess, sys
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "yfinance",
             "--break-system-packages", "--quiet"], check=True
        )
        import yfinance
        return yfinance


# ---------------------------------------------------------------------------
# StockFeed
# ---------------------------------------------------------------------------

class StockFeed:
    """Real-time and historical stock data via yfinance."""

    async def quote(self, symbol: str) -> dict:
        """
        Live quote for a single ticker.

        Returns:
            {
              "symbol":       str,   "name": str,
              "price":        float, "prev_close": float,
              "change":       float, "change_pct": float,
              "open":         float, "high": float, "low": float,
              "volume":       int,   "avg_volume": int,
              "market_cap":   int,
              "pe_ratio":     float | None,
              "52w_high":     float, "52w_low": float,
              "currency":     str,   "exchange": str,
              "market_state": str,   # REGULAR | PRE | POST | CLOSED
              "provider":     str,
            }
        """
        sym = symbol.upper().strip()
        if (cached := _cached(f"quote:{sym}", QUOTE_TTL)):
            return cached

        result = await self._yf_quote(sym)
        if result:
            _store(f"quote:{sym}", result)
        return result or {"error": f"No data for symbol '{sym}'"}

    async def quotes(self, symbols: list[str]) -> list[dict]:
        """Batch quotes for multiple symbols. Runs concurrently."""
        tasks = [self.quote(s) for s in symbols]
        return await asyncio.gather(*tasks)

    async def history(
        self,
        symbol:   str,
        period:   str = "1mo",
        interval: str = "1d",
    ) -> dict:
        """
        OHLCV price history.

        period:   1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
        interval: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo

        Returns:
            {
              "symbol":   str,
              "period":   str,
              "interval": str,
              "bars": [
                {"date": str, "open": float, "high": float,
                 "low": float, "close": float, "volume": int}
              ],
              "provider": str,
            }
        """
        sym = symbol.upper().strip()
        key = f"history:{sym}:{period}:{interval}"
        if (cached := _cached(key, HISTORY_TTL)):
            return cached

        result = await self._yf_history(sym, period, interval)
        if result:
            _store(key, result)
        return result or {"error": f"No history for '{sym}'"}

    async def search(self, query: str) -> list[dict]:
        """Search for ticker symbols by company name or keyword."""
        key = f"search:{query.lower()}"
        if (cached := _cached(key, 3600)):   # cache 1h
            return cached

        result = await self._yf_search(query)
        _store(key, result)
        return result

    # -- yfinance internals --------------------------------------------------

    async def _yf_quote(self, symbol: str) -> dict | None:
        loop = asyncio.get_event_loop()

        def _run():
            yf = _ensure_yfinance()
            ticker = yf.Ticker(symbol)
            info   = ticker.fast_info   # fast_info avoids heavy API call

            price      = getattr(info, "last_price",      None) or 0.0
            prev_close = getattr(info, "previous_close",  None) or 0.0
            change     = round(price - prev_close, 4) if prev_close else 0.0
            change_pct = round((change / prev_close) * 100, 2) if prev_close else 0.0

            # Supplement with .info for name, P/E etc (slower but more complete)
            try:
                full = ticker.info
                name    = full.get("longName") or full.get("shortName") or symbol
                pe      = full.get("trailingPE")
                mktcap  = full.get("marketCap") or getattr(info, "market_cap", 0) or 0
                avg_vol = full.get("averageVolume") or 0
                exch    = full.get("exchange") or ""
                curr    = full.get("currency") or "USD"
                state   = full.get("marketState") or "UNKNOWN"
                w52h    = full.get("fiftyTwoWeekHigh") or getattr(info, "year_high", 0) or 0
                w52l    = full.get("fiftyTwoWeekLow")  or getattr(info, "year_low",  0) or 0
            except Exception:
                name    = symbol
                pe      = None
                mktcap  = getattr(info, "market_cap", 0) or 0
                avg_vol = 0
                exch    = ""
                curr    = "USD"
                state   = "UNKNOWN"
                w52h    = getattr(info, "year_high", 0) or 0
                w52l    = getattr(info, "year_low",  0) or 0

            return {
                "symbol":       symbol,
                "name":         name,
                "price":        round(float(price), 4),
                "prev_close":   round(float(prev_close), 4),
                "change":       change,
                "change_pct":   change_pct,
                "open":         round(float(getattr(info, "open", 0) or 0), 4),
                "high":         round(float(getattr(info, "day_high", 0) or 0), 4),
                "low":          round(float(getattr(info, "day_low",  0) or 0), 4),
                "volume":       int(getattr(info, "last_volume", 0) or 0),
                "avg_volume":   int(avg_vol),
                "market_cap":   int(mktcap),
                "pe_ratio":     round(float(pe), 2) if pe else None,
                "52w_high":     round(float(w52h), 4),
                "52w_low":      round(float(w52l), 4),
                "currency":     curr,
                "exchange":     exch,
                "market_state": state,
                "provider":     "yfinance",
            }

        try:
            return await loop.run_in_executor(None, _run)
        except Exception as e:
            log.warning("[stocks] yfinance quote failed for %s: %s", symbol, e)
            return None

    async def _yf_history(self, symbol: str, period: str, interval: str) -> dict | None:
        loop = asyncio.get_event_loop()

        def _run():
            yf = _ensure_yfinance()
            df = yf.download(
                symbol, period=period, interval=interval,
                progress=False, auto_adjust=True
            )
            if df.empty:
                return None
            bars = []
            for ts, row in df.iterrows():
                bars.append({
                    "date":   str(ts)[:10] if interval in ("1d","5d","1wk","1mo","3mo") else str(ts),
                    "open":   round(float(row["Open"]),   4),
                    "high":   round(float(row["High"]),   4),
                    "low":    round(float(row["Low"]),    4),
                    "close":  round(float(row["Close"]),  4),
                    "volume": int(row["Volume"]),
                })
            return {
                "symbol":   symbol,
                "period":   period,
                "interval": interval,
                "bars":     bars,
                "provider": "yfinance",
            }

        try:
            return await loop.run_in_executor(None, _run)
        except Exception as e:
            log.warning("[stocks] yfinance history failed for %s: %s", symbol, e)
            return None

    async def _yf_search(self, query: str) -> list[dict]:
        loop = asyncio.get_event_loop()

        def _run():
            yf = _ensure_yfinance()
            try:
                results = yf.Search(query, max_results=10)
                quotes  = results.quotes if hasattr(results, "quotes") else []
                return [
                    {
                        "symbol":   q.get("symbol", ""),
                        "name":     q.get("longname") or q.get("shortname") or "",
                        "exchange": q.get("exchange", ""),
                        "type":     q.get("quoteType", ""),
                    }
                    for q in quotes
                    if q.get("symbol")
                ]
            except Exception:
                return []

        try:
            return await loop.run_in_executor(None, _run)
        except Exception as e:
            log.warning("[stocks] yfinance search failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_feed: StockFeed | None = None

def get_stock_feed() -> StockFeed:
    global _feed
    if _feed is None:
        _feed = StockFeed()
    return _feed
