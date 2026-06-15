"""
TRON-X Real-Time Data Feeds API  --  Phase 15

Routes at /api/feeds:

  Weather
    GET  /weather/current     ?location=London&units=metric
    GET  /weather/forecast    ?location=London&days=5&units=metric

  Stocks
    GET  /stocks/quote        ?symbol=AAPL
    POST /stocks/quotes       body: {"symbols": ["AAPL", "GOOGL", "MSFT"]}
    GET  /stocks/history      ?symbol=AAPL&period=1mo&interval=1d
    GET  /stocks/search       ?query=apple

  News
    GET  /news/headlines      ?topic=technology&country=us&count=10
    GET  /news/search         ?query=artificial+intelligence&count=10

  Crypto
    GET  /crypto/price        ?coin=bitcoin&currency=usd
    GET  /crypto/market       ?coin=bitcoin
    GET  /crypto/top          ?limit=10&currency=usd
    GET  /crypto/trending
    GET  /crypto/search       ?query=solana

  Status
    GET  /status              provider availability + cache stats
"""
from __future__ import annotations

import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.core.config import get_settings
from src.feeds.weather import get_weather_feed
from src.feeds.stocks  import get_stock_feed
from src.feeds.news    import get_news_feed
from src.feeds.crypto  import get_crypto_feed

router   = APIRouter(prefix="/api/feeds", tags=["feeds"])
settings = get_settings()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class QuotesRequest(BaseModel):
    symbols: List[str] = Field(..., min_length=1, max_items=20)


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

@router.get("/weather/current")
async def weather_current(
    location: str = Query(..., description="City name, 'lat,lon', or zip code"),
    units:    str = Query(default="metric", description="metric | imperial | standard"),
):
    """Current weather conditions for a location."""
    result = await get_weather_feed().current(location, units)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.get("/weather/forecast")
async def weather_forecast(
    location: str = Query(..., description="City name, 'lat,lon', or zip code"),
    days:     int = Query(default=5, ge=1, le=5, description="Number of forecast days (1-5)"),
    units:    str = Query(default="metric", description="metric | imperial | standard"),
):
    """5-day daily weather forecast."""
    result = await get_weather_feed().forecast(location, days, units)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


# ---------------------------------------------------------------------------
# Stocks
# ---------------------------------------------------------------------------

@router.get("/stocks/quote")
async def stock_quote(
    symbol: str = Query(..., description="Ticker symbol (e.g. AAPL, MSFT, TSLA)"),
):
    """Live stock quote for a single ticker."""
    result = await get_stock_feed().quote(symbol)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.post("/stocks/quotes")
async def stock_quotes(req: QuotesRequest):
    """Batch live quotes for up to 20 ticker symbols."""
    results = await get_stock_feed().quotes(req.symbols)
    return {"quotes": results, "count": len(results)}


@router.get("/stocks/history")
async def stock_history(
    symbol:   str = Query(..., description="Ticker symbol"),
    period:   str = Query(default="1mo", description="1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, ytd, max"),
    interval: str = Query(default="1d",  description="1m, 5m, 15m, 1h, 1d, 1wk, 1mo"),
):
    """OHLCV price history."""
    result = await get_stock_feed().history(symbol, period, interval)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.get("/stocks/search")
async def stock_search(
    query: str = Query(..., description="Company name or keyword"),
):
    """Search for ticker symbols by company name."""
    results = await get_stock_feed().search(query)
    return {"results": results, "count": len(results)}


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

@router.get("/news/headlines")
async def news_headlines(
    topic:   Optional[str] = Query(default=None,  description="Category (business, technology, sports, ...) or keyword"),
    country: str           = Query(default="us",  description="ISO 3166-1 alpha-2 country code"),
    count:   int           = Query(default=10,    ge=1, le=100),
):
    """Top news headlines, optionally filtered by topic or category."""
    result = await get_news_feed().headlines(topic, country, count)
    return result


@router.get("/news/search")
async def news_search(
    query: str = Query(..., description="Full-text search query"),
    count: int = Query(default=10, ge=1, le=100),
):
    """Full-text news search across all indexed sources."""
    result = await get_news_feed().search(query, count)
    return result


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------

@router.get("/crypto/price")
async def crypto_price(
    coin:     str = Query(...,         description="Coin ID or ticker (bitcoin, eth, sol, ...)"),
    currency: str = Query(default="usd", description="Target currency (usd, eur, gbp, btc, eth)"),
):
    """Live price for a single cryptocurrency."""
    result = await get_crypto_feed().price(coin, currency)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.get("/crypto/market")
async def crypto_market(
    coin: str = Query(..., description="Coin ID or ticker"),
):
    """Detailed market data: ATH, ATL, supply, 1h/24h/7d change."""
    result = await get_crypto_feed().market_data(coin)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.get("/crypto/top")
async def crypto_top(
    limit:    int = Query(default=10,  ge=1,  le=250, description="Number of coins to return"),
    currency: str = Query(default="usd", description="Target currency"),
):
    """Top N coins ranked by market cap."""
    results = await get_crypto_feed().top(limit, currency)
    return {"coins": results, "count": len(results), "currency": currency}


@router.get("/crypto/trending")
async def crypto_trending():
    """Coins trending on CoinGecko in the last 24 hours."""
    results = await get_crypto_feed().trending()
    return {"trending": results, "count": len(results)}


@router.get("/crypto/search")
async def crypto_search(
    query: str = Query(..., description="Coin name or ticker symbol"),
):
    """Search for coins by name or ticker."""
    results = await get_crypto_feed().search(query)
    return {"results": results, "count": len(results)}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@router.get("/status")
async def feeds_status():
    """Provider availability and cache statistics for all data feeds."""
    return {
        "weather": {
            "openweathermap": bool(settings.openweather_api_key),
            "wttr_fallback":  True,
            "cache_ttl_s":    600,
        },
        "stocks": {
            "yfinance":      True,
            "alpha_vantage": bool(settings.alpha_vantage_key),
            "cache_ttl_s":   60,
        },
        "news": {
            "newsapi":           bool(settings.newsapi_key),
            "google_news_rss":   True,
            "cache_ttl_s":       900,
            "valid_categories":  [
                "business", "entertainment", "general",
                "health", "science", "sports", "technology",
            ],
        },
        "crypto": {
            "coingecko":       True,
            "key_required":    False,
            "price_ttl_s":     120,
            "market_ttl_s":    300,
        },
        "endpoints": {
            "weather":  ["GET /api/feeds/weather/current", "GET /api/feeds/weather/forecast"],
            "stocks":   ["GET /api/feeds/stocks/quote", "POST /api/feeds/stocks/quotes",
                         "GET /api/feeds/stocks/history", "GET /api/feeds/stocks/search"],
            "news":     ["GET /api/feeds/news/headlines", "GET /api/feeds/news/search"],
            "crypto":   ["GET /api/feeds/crypto/price", "GET /api/feeds/crypto/market",
                         "GET /api/feeds/crypto/top", "GET /api/feeds/crypto/trending",
                         "GET /api/feeds/crypto/search"],
        },
    }
