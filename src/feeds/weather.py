"""
TRON-X Weather Feed  --  Phase 15

Providers (in order):
  1. OpenWeatherMap API  -- requires OPENWEATHER_API_KEY in .env
  2. wttr.in JSON API    -- no key needed, always available as fallback

TTL cache: 10 minutes (weather doesn't change that fast)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from src.core.config import get_settings
from src.core.logger import log

settings = get_settings()

OWM_BASE   = "https://api.openweathermap.org/data/2.5"
WTTR_BASE  = "https://wttr.in"
CACHE_TTL  = 600   # 10 minutes

_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str) -> Any | None:
    if key in _cache:
        ts, val = _cache[key]
        if time.monotonic() - ts < CACHE_TTL:
            return val
    return None


def _store(key: str, val: Any) -> None:
    _cache[key] = (time.monotonic(), val)


async def _http_get(url: str, params: dict | None = None) -> dict | list:
    """Async HTTP GET using aiohttp; auto-installs if missing."""
    try:
        import aiohttp
    except ImportError:
        import subprocess, sys
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "aiohttp",
             "--break-system-packages", "--quiet"], check=True
        )
        import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.json(content_type=None)


# ---------------------------------------------------------------------------
# WeatherFeed
# ---------------------------------------------------------------------------

class WeatherFeed:
    """Real-time weather data with OpenWeatherMap + wttr.in fallback."""

    # -- current conditions --------------------------------------------------

    async def current(self, location: str, units: str = "metric") -> dict:
        """
        Current weather for a location (city name, 'lat,lon', or zip code).

        Returns:
            {
              "location":    str,   "country": str,
              "temp":        float, "feels_like": float,
              "temp_min":    float, "temp_max": float,
              "humidity":    int,   "pressure": int,
              "conditions":  str,   "description": str,
              "wind_speed":  float, "wind_deg": int,
              "visibility":  int,   "clouds": int,
              "sunrise":     int,   "sunset": int,   # unix timestamps
              "units":       str,
              "provider":    str,
            }
        """
        key = f"current:{location}:{units}"
        if (cached := _cached(key)):
            return cached

        result = None
        if settings.openweather_api_key:
            result = await self._owm_current(location, units)
        if result is None:
            result = await self._wttr_current(location, units)

        if result:
            _store(key, result)
        return result or {"error": f"Weather unavailable for '{location}'"}

    async def forecast(self, location: str, days: int = 5, units: str = "metric") -> dict:
        """
        5-day / 3-hourly forecast collapsed to daily summaries.

        Returns:
            {
              "location": str, "country": str,
              "days": [
                {
                  "date": str,           # YYYY-MM-DD
                  "temp_min":  float,
                  "temp_max":  float,
                  "conditions": str,
                  "description": str,
                  "humidity":  int,
                  "wind_speed": float,
                  "pop": float,          # probability of precipitation 0-1
                }
              ],
              "units": str, "provider": str,
            }
        """
        days = max(1, min(days, 5))
        key  = f"forecast:{location}:{days}:{units}"
        if (cached := _cached(key)):
            return cached

        result = None
        if settings.openweather_api_key:
            result = await self._owm_forecast(location, units, days)
        if result is None:
            result = await self._wttr_forecast(location, units, days)

        if result:
            _store(key, result)
        return result or {"error": f"Forecast unavailable for '{location}'"}

    # -- OWM internals -------------------------------------------------------

    async def _owm_current(self, location: str, units: str) -> dict | None:
        try:
            params = {
                "q":     location,
                "appid": settings.openweather_api_key,
                "units": units,
            }
            data = await _http_get(f"{OWM_BASE}/weather", params)
            return {
                "location":    data["name"],
                "country":     data["sys"]["country"],
                "temp":        data["main"]["temp"],
                "feels_like":  data["main"]["feels_like"],
                "temp_min":    data["main"]["temp_min"],
                "temp_max":    data["main"]["temp_max"],
                "humidity":    data["main"]["humidity"],
                "pressure":    data["main"]["pressure"],
                "conditions":  data["weather"][0]["main"],
                "description": data["weather"][0]["description"].capitalize(),
                "wind_speed":  data["wind"].get("speed", 0),
                "wind_deg":    data["wind"].get("deg", 0),
                "visibility":  data.get("visibility", 0),
                "clouds":      data["clouds"]["all"],
                "sunrise":     data["sys"]["sunrise"],
                "sunset":      data["sys"]["sunset"],
                "units":       units,
                "provider":    "openweathermap",
            }
        except Exception as e:
            log.warning("[weather] OWM current failed: %s", e)
            return None

    async def _owm_forecast(self, location: str, units: str, days: int) -> dict | None:
        try:
            params = {
                "q":     location,
                "appid": settings.openweather_api_key,
                "units": units,
                "cnt":   days * 8,   # 3-hourly * 8 = 1 day
            }
            data = await _http_get(f"{OWM_BASE}/forecast", params)

            # Collapse 3-hourly entries into daily
            from collections import defaultdict
            daily: dict[str, list] = defaultdict(list)
            for item in data["list"]:
                date = item["dt_txt"][:10]
                daily[date].append(item)

            result_days = []
            for date, entries in sorted(daily.items())[:days]:
                temps    = [e["main"]["temp"]     for e in entries]
                humids   = [e["main"]["humidity"] for e in entries]
                winds    = [e["wind"]["speed"]    for e in entries]
                pops     = [e.get("pop", 0)       for e in entries]
                midday   = entries[len(entries) // 2]
                result_days.append({
                    "date":        date,
                    "temp_min":    round(min(temps), 1),
                    "temp_max":    round(max(temps), 1),
                    "conditions":  midday["weather"][0]["main"],
                    "description": midday["weather"][0]["description"].capitalize(),
                    "humidity":    int(sum(humids) / len(humids)),
                    "wind_speed":  round(sum(winds) / len(winds), 1),
                    "pop":         round(max(pops), 2),
                })

            city = data["city"]
            return {
                "location": city["name"],
                "country":  city["country"],
                "days":     result_days,
                "units":    units,
                "provider": "openweathermap",
            }
        except Exception as e:
            log.warning("[weather] OWM forecast failed: %s", e)
            return None

    # -- wttr.in internals ---------------------------------------------------

    async def _wttr_current(self, location: str, units: str) -> dict | None:
        try:
            data = await _http_get(f"{WTTR_BASE}/{location}", {"format": "j1"})
            cc  = data["current_condition"][0]
            na  = data["nearest_area"][0]
            city = na["areaName"][0]["value"]
            country = na["country"][0]["value"]
            temp_c = float(cc["temp_C"])
            feels_c = float(cc["FeelsLikeC"])
            if units == "imperial":
                temp_val = round(temp_c * 9/5 + 32, 1)
                feels_val = round(feels_c * 9/5 + 32, 1)
            else:
                temp_val = temp_c
                feels_val = feels_c
            return {
                "location":    city,
                "country":     country,
                "temp":        temp_val,
                "feels_like":  feels_val,
                "temp_min":    temp_val,
                "temp_max":    temp_val,
                "humidity":    int(cc["humidity"]),
                "pressure":    int(cc["pressure"]),
                "conditions":  cc["weatherDesc"][0]["value"],
                "description": cc["weatherDesc"][0]["value"],
                "wind_speed":  round(float(cc["windspeedKmph"]) / 3.6, 1),
                "wind_deg":    int(cc["winddirDegree"]),
                "visibility":  int(cc["visibility"]) * 1000,
                "clouds":      int(cc["cloudcover"]),
                "sunrise":     0,
                "sunset":      0,
                "units":       units,
                "provider":    "wttr.in",
            }
        except Exception as e:
            log.warning("[weather] wttr.in current failed: %s", e)
            return None

    async def _wttr_forecast(self, location: str, units: str, days: int) -> dict | None:
        try:
            data = await _http_get(f"{WTTR_BASE}/{location}", {"format": "j1"})
            na   = data["nearest_area"][0]
            city    = na["areaName"][0]["value"]
            country = na["country"][0]["value"]

            result_days = []
            for day in data["weather"][:days]:
                max_c = float(day["maxtempC"])
                min_c = float(day["mintempC"])
                if units == "imperial":
                    max_val = round(max_c * 9/5 + 32, 1)
                    min_val = round(min_c * 9/5 + 32, 1)
                else:
                    max_val, min_val = max_c, min_c
                hourly = day["hourly"]
                avg_humid = int(sum(int(h["humidity"]) for h in hourly) / len(hourly))
                avg_wind  = round(sum(float(h["windspeedKmph"]) for h in hourly) / len(hourly) / 3.6, 1)
                desc = day["hourly"][4]["weatherDesc"][0]["value"] if len(hourly) > 4 else "N/A"
                result_days.append({
                    "date":        day["date"],
                    "temp_min":    min_val,
                    "temp_max":    max_val,
                    "conditions":  desc,
                    "description": desc,
                    "humidity":    avg_humid,
                    "wind_speed":  avg_wind,
                    "pop":         0.0,
                })
            return {
                "location": city,
                "country":  country,
                "days":     result_days,
                "units":    units,
                "provider": "wttr.in",
            }
        except Exception as e:
            log.warning("[weather] wttr.in forecast failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_feed: WeatherFeed | None = None

def get_weather_feed() -> WeatherFeed:
    global _feed
    if _feed is None:
        _feed = WeatherFeed()
    return _feed
