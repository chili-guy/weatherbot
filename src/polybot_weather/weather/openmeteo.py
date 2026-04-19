"""Open-Meteo client — deterministic forecast, ensemble, and ERA5 archive.

Open-Meteo is global, free, no key required. Endpoints used:

  * Forecast:   https://api.open-meteo.com/v1/forecast        (deterministic)
  * Ensemble:   https://ensemble-api.open-meteo.com/v1/ensemble (30-51 members)
  * Archive:    https://archive-api.open-meteo.com/v1/archive   (ERA5 reanalysis)

All temperatures are returned in CELSIUS by Open-Meteo and converted to
Fahrenheit at the boundary, since Polymarket weather markets resolve in °F.
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

FORECAST_BASE = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_BASE = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE_BASE = "https://archive-api.open-meteo.com/v1/archive"

ENSEMBLE_MODELS = "gfs_seamless,ecmwf_ifs025,icon_seamless"


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


@dataclass
class EnsembleMembers:
    """Per-day extreme values from each ensemble member.

    Stored in the unit requested by the caller (°F for US markets, °C for
    international). Field names use `_f`/`_min_f` for backwards compatibility,
    but the unit is whatever was passed to `ensemble_for_date(unit=...)`.
    """

    target_date: date
    timezone: str
    unit: str = "F"
    member_max_f: list[float] = field(default_factory=list)
    member_min_f: list[float] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)

    @property
    def member_count(self) -> int:
        return len(self.member_max_f)


class OpenMeteoClient:
    def __init__(self, *, user_agent: str, cache: JsonCache, timeout: float = 30.0) -> None:
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._timeout = timeout
        self._cache = cache

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        cache_key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("openmeteo.http_failed", url=url, error=str(e))
                return None
        data = resp.json()
        self._cache.set(cache_key, data)
        return data

    @staticmethod
    def _unit_param(unit: str) -> str:
        return "fahrenheit" if unit.upper() == "F" else "celsius"

    async def daily_deterministic_max(
        self, lat: float, lon: float, target_date: date, timezone: str, *, unit: str = "F"
    ) -> float | None:
        """Single-value daily max temperature for one date in the requested unit."""
        data = await self._get(
            FORECAST_BASE,
            {
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "timezone": timezone,
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "temperature_unit": self._unit_param(unit),
            },
        )
        if not data:
            return None
        try:
            vals = data["daily"]["temperature_2m_max"]
            return float(vals[0]) if vals else None
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    async def ensemble_for_date(
        self,
        lat: float,
        lon: float,
        target_date: date,
        timezone: str,
        *,
        unit: str = "F",
        hours_lookahead: int = 192,
    ) -> EnsembleMembers:
        """Pull hourly ensemble members and reduce to per-member daily max/min in °F.

        The ensemble endpoint returns hourly arrays per model member. We slice
        the local-day window for `target_date` and take min/max per member.
        """
        result = EnsembleMembers(target_date=target_date, timezone=timezone, unit=unit.upper())

        tz = ZoneInfo(timezone)
        local_start = datetime.combine(target_date, time.min, tzinfo=tz)
        local_end = datetime.combine(target_date, time.max, tzinfo=tz)

        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "models": ENSEMBLE_MODELS,
            "timezone": timezone,
            "forecast_days": min(max((target_date - date.today()).days + 2, 2), 16),
            "temperature_unit": self._unit_param(unit),
        }
        data = await self._get(ENSEMBLE_BASE, params)
        if not data:
            result.sources_failed.append("openmeteo_ensemble")
            return result

        hourly = data.get("hourly") or {}
        time_strs: list[str] = hourly.get("time") or []
        if not time_strs:
            result.sources_failed.append("openmeteo_ensemble_empty")
            return result

        # Times come back as naive ISO in the requested timezone.
        times = [datetime.fromisoformat(t).replace(tzinfo=tz) for t in time_strs]

        in_window = [i for i, t in enumerate(times) if local_start <= t <= local_end]
        if not in_window:
            log.warning(
                "openmeteo.ensemble_no_window",
                target_date=str(target_date),
                tz=timezone,
                returned_first=time_strs[0] if time_strs else None,
                returned_last=time_strs[-1] if time_strs else None,
            )
            return result

        # Member series live under temperature_2m, temperature_2m_member01 ... member50
        member_keys = [k for k in hourly if k == "temperature_2m" or k.startswith("temperature_2m_member")]
        for key in member_keys:
            series = hourly.get(key) or []
            if not series:
                continue
            window = [series[i] for i in in_window if i < len(series) and series[i] is not None]
            if not window:
                continue
            result.member_max_f.append(float(max(window)))
            result.member_min_f.append(float(min(window)))

        if not result.member_max_f:
            result.sources_failed.append("openmeteo_ensemble_no_members")

        log.info(
            "openmeteo.ensemble_loaded",
            members=result.member_count,
            target_date=str(target_date),
            tz=timezone,
        )
        return result

    async def archive_day_extremes(
        self,
        lat: float,
        lon: float,
        target_date: date,
        timezone: str,
        *,
        unit: str = "F",
    ) -> tuple[float | None, float | None]:
        """Return (observed_max, observed_min) for `target_date` from ERA5 archive.

        Archive data lags by ~5 days; for very recent dates this may return
        None. Used by `polybot resolve` to close the forecast/realized loop.
        """
        data = await self._get(
            ARCHIVE_BASE,
            {
                "latitude": lat,
                "longitude": lon,
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": self._unit_param(unit),
                "timezone": timezone,
            },
        )
        if not data:
            return None, None
        try:
            maxs = data["daily"]["temperature_2m_max"]
            mins = data["daily"]["temperature_2m_min"]
        except KeyError:
            return None, None
        vmax = float(maxs[0]) if maxs and maxs[0] is not None else None
        vmin = float(mins[0]) if mins and mins[0] is not None else None
        return vmax, vmin

    async def climatology_max(
        self,
        lat: float,
        lon: float,
        month: int,
        day: int,
        *,
        years: int = 30,
        unit: str = "F",
        timezone: str = "UTC",
    ) -> list[float]:
        """Daily max temperatures for the same calendar day across `years` years.

        Pass the station's local timezone so the daily-max buckets align with
        the forecast and archive endpoints (both use local-day boundaries).
        A UTC bucket for a station at UTC+9 straddles two local days and
        biases the climatology distribution.
        """
        end_year = date.today().year - 1
        start_year = end_year - years + 1
        start = date(start_year, 1, 1)
        end = date(end_year, 12, 31)

        data = await self._get(
            ARCHIVE_BASE,
            {
                "latitude": lat,
                "longitude": lon,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "daily": "temperature_2m_max",
                "temperature_unit": self._unit_param(unit),
                "timezone": timezone,
            },
        )
        if not data:
            return []
        try:
            dates = data["daily"]["time"]
            vals = data["daily"]["temperature_2m_max"]
        except KeyError:
            return []

        out: list[float] = []
        target_md = (month, day)
        for d_str, v in zip(dates, vals, strict=False):
            try:
                d = date.fromisoformat(d_str)
            except ValueError:
                continue
            if v is None:
                continue
            if (d.month, d.day) == target_md:
                out.append(float(v))
        return out


__all__ = ["OpenMeteoClient", "EnsembleMembers", "c_to_f"]
