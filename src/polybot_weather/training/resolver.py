"""Resolve expired markets — pull realized extremes from the ERA5 archive.

ERA5 lags real-time by ~5 days, so `polybot resolve` is intended to be run
periodically (e.g. daily cron). It reads every `Market` whose resolution_date
has passed and that doesn't yet have an `Outcome` row, fetches the observed
daily max/min from Open-Meteo's archive endpoint, and writes the outcome.

We only need the scalar realized value here; deciding which specific outcome
*won* on a per-recommendation basis happens lazily in the backtester by
re-parsing each stored Recommendation.outcome_label.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

import structlog

from ..config import Settings
from ..storage.models import Market
from ..storage.repo import Repo
from ..weather.cache import JsonCache
from ..weather.openmeteo import OpenMeteoClient
from ..weather.stations import get_station

log = structlog.get_logger(__name__)


@dataclass
class ResolutionRecord:
    polymarket_id: str
    slug: str
    metric: str
    realized_value: float | None
    note: str | None = None


async def resolve_pending(
    *,
    settings: Settings,
    repo: Repo,
    lookback_days: int = 21,
) -> list[ResolutionRecord]:
    """Record outcomes for every expired, un-resolved market within lookback."""
    pending: list[Market] = repo.markets_awaiting_resolution(lookback_days=lookback_days)
    if not pending:
        return []

    # ERA5 values for past dates never change — cache for a week so repeated
    # `polybot resolve` runs don't re-hit the archive API.
    archive_cache = JsonCache(settings.cache_dir / "archive", ttl_seconds=7 * 24 * 3600)
    om = OpenMeteoClient(user_agent=settings.user_agent, cache=archive_cache)

    out: list[ResolutionRecord] = []
    for m in pending:
        if m.station_code is None or m.resolution_date is None:
            out.append(
                ResolutionRecord(
                    polymarket_id=m.polymarket_id, slug=m.slug, metric=m.metric,
                    realized_value=None, note="missing station or date",
                )
            )
            continue
        st = get_station(m.station_code)
        if st is None:
            out.append(
                ResolutionRecord(
                    polymarket_id=m.polymarket_id, slug=m.slug, metric=m.metric,
                    realized_value=None, note=f"unknown station {m.station_code}",
                )
            )
            continue

        # CRITICAL: fetch the archive in the SAME unit used to compute
        # `forecast_mean_f` at analysis time — otherwise calibration residuals
        # mix °F and °C and the bias table is garbage. `m.unit` is set by the
        # analyze path from the parsed market; fall back to station default
        # only for rows written before the `unit` column existed.
        unit = (m.unit or st.default_unit).upper()

        # resolution_date is stored as UTC end-of-local-day; the date we actually
        # need from the archive is the LOCAL calendar day at the station.
        local_resolution = m.resolution_date.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(st.timezone))
        target_date = local_resolution.date()

        vmax, vmin = await om.archive_day_extremes(
            st.lat, st.lon, target_date, st.timezone, unit=unit
        )
        realized = vmax if m.metric == "max_temp" else vmin if m.metric == "min_temp" else None
        if realized is None:
            out.append(
                ResolutionRecord(
                    polymarket_id=m.polymarket_id, slug=m.slug, metric=m.metric,
                    realized_value=None,
                    note="archive unavailable (market may be < ~5 days old)",
                )
            )
            continue

        repo.record_outcome(
            market_id=m.id,
            winning_outcome_label=f"{m.metric}={realized:.2f}{unit}",
            realized_value=realized,
        )
        log.info(
            "resolver.recorded",
            slug=m.slug, metric=m.metric, realized=realized, station=m.station_code,
        )
        out.append(
            ResolutionRecord(
                polymarket_id=m.polymarket_id, slug=m.slug, metric=m.metric,
                realized_value=realized,
            )
        )
    return out
