"""Re-fit the per-(station × model × month) bias table from resolved history.

We assume a single model string ("openmeteo_ensemble") since that's the only
one the analysis pipeline emits today. Extending to per-member models would
require `Forecast` to store member-level means.

Signed error convention matches `probability/calibration.py`:
    mean_error_f = mean(realized − forecast_mean)
So positive means the model under-predicted and the correction should be
ADDED to each raw member.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import structlog

from ..probability.calibration import BiasEntry, BiasKey
from ..storage.repo import Repo

log = structlog.get_logger(__name__)

MODEL_KEY = "openmeteo_ensemble"
MIN_SAMPLES = 5


@dataclass
class CalibrationSummary:
    entries_written: int
    entries_skipped_insufficient_samples: int
    pairs_considered: int


def recalibrate(*, repo: Repo) -> CalibrationSummary:
    pairs = repo.forecast_outcome_pairs()

    # Dedup: one market can have hundreds of forecast rows (each scan appends).
    # Treat it as ONE sample by taking the most-recent forecast per market.
    # Without this, high-traffic markets dominate the bias bucket purely by
    # scan frequency — bias becomes a function of how often the dashboard ran.
    latest_by_market: dict[int, tuple[object, object, object]] = {}
    for forecast, outcome, market in pairs:
        prev = latest_by_market.get(market.id)
        if prev is None or forecast.run_at > prev[0].run_at:
            latest_by_market[market.id] = (forecast, outcome, market)

    buckets: dict[BiasKey, list[float]] = defaultdict(list)
    for forecast, outcome, market in latest_by_market.values():
        if forecast.forecast_mean_f is None or outcome.realized_value is None:
            continue
        if market.station_code is None or market.resolution_date is None:
            continue
        key = BiasKey(
            station=market.station_code.upper(),
            model=MODEL_KEY,
            month=market.resolution_date.month,
        )
        buckets[key].append(outcome.realized_value - forecast.forecast_mean_f)

    written = 0
    skipped = 0
    for key, errors in buckets.items():
        n = len(errors)
        if n < MIN_SAMPLES:
            skipped += 1
            continue
        mean_err = sum(errors) / n
        repo.upsert_bias(
            BiasEntry(key=key, mean_error_f=mean_err, sample_count=n),
        )
        written += 1
        log.info(
            "calibrator.bias_updated",
            station=key.station, model=key.model, month=key.month,
            mean_error_f=round(mean_err, 3), n=n,
        )

    return CalibrationSummary(
        entries_written=written,
        entries_skipped_insufficient_samples=skipped,
        pairs_considered=len(latest_by_market),
    )
