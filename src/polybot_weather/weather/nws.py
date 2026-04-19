"""NOAA / NWS gridded forecast client.

NWS requires an identifying User-Agent. Calls are two-step:
  1. GET /points/{lat},{lon}  →  returns gridId + gridX + gridY + forecastHourly URL
  2. GET that hourly URL      →  hourly temperature forecast in °F
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog

from .cache import JsonCache

log = structlog.get_logger(__name__)

NWS_BASE = "https://api.weather.gov"


@dataclass
class NwsForecast:
    target_date: date
    timezone: str
    max_f: float | None = None
    min_f: float | None = None
    sources_failed: list[str] = field(default_factory=list)


class NwsClient:
    def __init__(self, *, user_agent: str, cache: JsonCache, timeout: float = 30.0) -> None:
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "application/geo+json",
        }
        self._timeout = timeout
        self._cache = cache

    async def _get(self, url: str) -> dict[str, Any] | None:
        cached = self._cache.get(url)
        if cached is not None:
            return cached
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("nws.http_failed", url=url, error=str(e))
                return None
        data = resp.json()
        self._cache.set(url, data)
        return data

    async def hourly_extremes(
        self, lat: float, lon: float, target_date: date, timezone: str
    ) -> NwsForecast:
        out = NwsForecast(target_date=target_date, timezone=timezone)

        points = await self._get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
        if not points:
            out.sources_failed.append("nws_points")
            return out
        forecast_url = (
            points.get("properties", {}).get("forecastHourly")
            if isinstance(points, dict)
            else None
        )
        if not forecast_url:
            out.sources_failed.append("nws_no_forecast_url")
            return out

        forecast = await self._get(forecast_url)
        if not forecast:
            out.sources_failed.append("nws_forecast")
            return out

        periods = forecast.get("properties", {}).get("periods", [])
        if not periods:
            return out

        tz = ZoneInfo(timezone)
        local_start = datetime.combine(target_date, time.min, tzinfo=tz)
        local_end = datetime.combine(target_date, time.max, tzinfo=tz)

        temps_f: list[float] = []
        for p in periods:
            try:
                start = datetime.fromisoformat(p["startTime"]).astimezone(tz)
            except (KeyError, ValueError):
                continue
            if not (local_start <= start <= local_end):
                continue
            unit = (p.get("temperatureUnit") or "F").upper()
            t = p.get("temperature")
            if t is None:
                continue
            t_f = float(t) if unit == "F" else (float(t) * 9 / 5 + 32)
            temps_f.append(t_f)

        if temps_f:
            out.max_f = max(temps_f)
            out.min_f = min(temps_f)
        return out
