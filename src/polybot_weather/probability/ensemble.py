"""Combine multiple weather sources into a single probability distribution.

Inputs:
  * ensemble member peaks (Open-Meteo ensemble, °F)         — primary signal
  * NWS deterministic max/min (°F, optional)                — sanity nudge
  * climatology distribution (°F, optional)                 — weak prior
  * bias correction (°F, optional)                          — applied to members

Output: a list of `BinProbability` aligned to the market's bins.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..polymarket.parsers import TempBin
from .bins import BinProbability, build_distribution, mix_with_climatology
from .calibration import apply_bias


@dataclass
class EnsembleResult:
    distribution: list[BinProbability]
    member_count: int
    bias_correction_f: float
    used_climatology: bool
    spread_f: float  # max - min across (bias-corrected) members


def _spread(values: list[float]) -> float:
    if not values:
        return 0.0
    return float(max(values) - min(values))


def combine(
    *,
    member_values_f: list[float],
    bins: list[TempBin],
    bias_correction_f: float = 0.0,
    climatology_dist: list[BinProbability] | None = None,
    climatology_weight: float = 0.10,
    laplace_alpha: float = 0.5,
) -> EnsembleResult:
    """Build the model's probability distribution over the market's bins.

    Bins must be aligned with `climatology_dist` if it's provided.
    """
    if not member_values_f:
        empty = [BinProbability(bin=b, probability=1.0 / len(bins) if bins else 0.0, members_in_bin=0.0) for b in bins]
        return EnsembleResult(
            distribution=empty,
            member_count=0,
            bias_correction_f=bias_correction_f,
            used_climatology=False,
            spread_f=0.0,
        )

    corrected = apply_bias(member_values_f, bias_correction_f)
    forecast_dist = build_distribution(corrected, bins, laplace_alpha=laplace_alpha)

    used_clim = False
    if climatology_dist is not None and len(climatology_dist) == len(bins):
        forecast_dist = mix_with_climatology(forecast_dist, climatology_dist, climatology_weight)
        used_clim = True

    return EnsembleResult(
        distribution=forecast_dist,
        member_count=len(member_values_f),
        bias_correction_f=bias_correction_f,
        used_climatology=used_clim,
        spread_f=_spread(corrected),
    )
