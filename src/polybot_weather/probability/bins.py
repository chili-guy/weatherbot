"""Map a continuous distribution of forecast peaks → discrete Polymarket bins.

Polymarket weather markets resolve against an integer-Fahrenheit reading from
the official station. So a model member predicting 64.4 °F resolves as 64 °F
and a 64.6 °F prediction resolves as 65 °F. Binning happens AFTER rounding,
not before, otherwise we leak probability mass to neighboring bins.

We use Laplace smoothing (0.5 virtual member per bin) to keep tail bins from
hitting probability zero, which would blow up Kelly sizing later.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..polymarket.parsers import TempBin


@dataclass(frozen=True)
class BinProbability:
    bin: TempBin
    probability: float
    members_in_bin: float  # fractional after smoothing


def _round_half_up(x: float) -> int:
    """Banker's rounding gives 0.5 → even, but NWS uses round-half-up. Match it."""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def round_to_resolution(values: list[float]) -> list[int]:
    """Round float forecasts to the integer °F that Polymarket would resolve to."""
    return [_round_half_up(v) for v in values]


def assign_to_bins(
    rounded_values: list[int], bins: list[TempBin], laplace_alpha: float = 0.5
) -> list[BinProbability]:
    """Assign each rounded value to the first bin that contains it; smooth.

    `laplace_alpha` is the virtual-count added to every bin (per-bin pseudo-count,
    not total). With 51 ensemble members and `alpha=0.5`, a bin that captured
    zero members gets probability `0.5 / (51 + 0.5*N_bins)` instead of 0.
    """
    if not bins:
        return []

    counts: list[float] = [laplace_alpha] * len(bins)
    n_assigned = 0
    for v in rounded_values:
        for i, b in enumerate(bins):
            if b.contains(float(v)):
                counts[i] += 1.0
                n_assigned += 1
                break

    total = sum(counts)
    if total <= 0:
        return [BinProbability(bin=b, probability=0.0, members_in_bin=0.0) for b in bins]

    return [
        BinProbability(bin=b, probability=c / total, members_in_bin=c)
        for b, c in zip(bins, counts, strict=True)
    ]


def build_distribution(
    member_values: list[float],
    bins: list[TempBin],
    *,
    laplace_alpha: float = 0.5,
) -> list[BinProbability]:
    """Full pipeline: float members → integer rounding → binned probabilities."""
    rounded = round_to_resolution(member_values)
    return assign_to_bins(rounded, bins, laplace_alpha=laplace_alpha)


def mix_with_climatology(
    forecast_dist: list[BinProbability],
    climatology_dist: list[BinProbability],
    climatology_weight: float = 0.10,
) -> list[BinProbability]:
    """Convex mix forecast + climatology distributions over the same bins.

    `climatology_weight` defaults to 10% per the spec. Bins MUST be aligned
    (same `bin.label`, in the same order).
    """
    if not 0.0 <= climatology_weight <= 1.0:
        raise ValueError("climatology_weight must be in [0,1]")
    if len(forecast_dist) != len(climatology_dist):
        raise ValueError("forecast_dist and climatology_dist must align")

    w = climatology_weight
    out: list[BinProbability] = []
    for f, c in zip(forecast_dist, climatology_dist, strict=True):
        if f.bin.label != c.bin.label:
            raise ValueError(f"bin mismatch: {f.bin.label} vs {c.bin.label}")
        p = (1.0 - w) * f.probability + w * c.probability
        out.append(BinProbability(bin=f.bin, probability=p, members_in_bin=f.members_in_bin))

    total = sum(b.probability for b in out)
    if total > 0:
        out = [BinProbability(bin=b.bin, probability=b.probability / total, members_in_bin=b.members_in_bin) for b in out]
    return out
