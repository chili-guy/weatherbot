"""Bin assignment + Laplace smoothing + climatology mixing."""

from __future__ import annotations

import math

import pytest

from polybot_weather.polymarket.parsers import TempBin
from polybot_weather.probability.bins import (
    assign_to_bins,
    build_distribution,
    mix_with_climatology,
    round_to_resolution,
)


@pytest.fixture
def nyc_bins() -> list[TempBin]:
    return [
        TempBin(label="≤61°F", low=None, high=61.0),
        TempBin(label="62-63°F", low=62.0, high=63.0),
        TempBin(label="64-65°F", low=64.0, high=65.0),
        TempBin(label="66-67°F", low=66.0, high=67.0),
        TempBin(label="68-69°F", low=68.0, high=69.0),
        TempBin(label="≥70°F", low=70.0, high=None),
    ]


def test_round_half_up_matches_polymarket_resolution_rule():
    # 64.4 → 64; 64.6 → 65; 64.5 → 65 (half-up, NOT banker's rounding).
    assert round_to_resolution([64.4, 64.5, 64.6, 65.5]) == [64, 65, 65, 66]


def test_assign_to_bins_with_laplace_prevents_zero(nyc_bins):
    members_int = [64, 64, 65, 65, 66]   # five values, all in mid bins
    dist = assign_to_bins(members_int, nyc_bins, laplace_alpha=0.5)
    probs = [bp.probability for bp in dist]
    # All bins must get nonzero mass thanks to Laplace.
    assert all(p > 0 for p in probs)
    # Probabilities sum to 1.
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)
    # The 64-65°F bin gets the bulk (4 of 5 actual members).
    assert dist[2].probability == max(probs)


def test_build_distribution_does_rounding_then_binning(nyc_bins):
    member_floats = [63.6, 64.4, 64.6, 65.5, 66.4]
    # rounded → [64, 64, 65, 66, 66]
    dist = build_distribution(member_floats, nyc_bins)
    # Mass concentrated in 64-65 and 66-67.
    by_label = {bp.bin.label: bp.probability for bp in dist}
    assert by_label["64-65°F"] > by_label["≤61°F"]
    assert by_label["66-67°F"] > by_label["≤61°F"]


def test_open_ended_bins_capture_extremes(nyc_bins):
    member_floats = [55.0, 58.0, 75.0, 80.0]
    dist = build_distribution(member_floats, nyc_bins)
    by_label = {bp.bin.label: bp.probability for bp in dist}
    assert by_label["≤61°F"] > by_label["64-65°F"]
    assert by_label["≥70°F"] > by_label["64-65°F"]


def test_mix_with_climatology_is_convex(nyc_bins):
    forecast = build_distribution([64.0] * 10, nyc_bins)
    # Climatology spread across 60-70.
    climo = build_distribution([60.0, 62.0, 64.0, 66.0, 68.0, 70.0], nyc_bins)
    mixed = mix_with_climatology(forecast, climo, climatology_weight=0.10)
    assert math.isclose(sum(b.probability for b in mixed), 1.0, abs_tol=1e-9)
    # The forecast's hot bin should be diluted but still highest.
    by_label = {b.bin.label: b.probability for b in mixed}
    forecast_by_label = {b.bin.label: b.probability for b in forecast}
    assert by_label["64-65°F"] < forecast_by_label["64-65°F"]
    assert by_label["64-65°F"] == max(by_label.values())


def test_mix_rejects_misaligned_bins(nyc_bins):
    other_bins = nyc_bins[:5]  # different length
    forecast = build_distribution([64.0], nyc_bins)
    climo = build_distribution([64.0], other_bins)
    with pytest.raises(ValueError):
        mix_with_climatology(forecast, climo)


def test_empty_bins_returns_empty():
    assert build_distribution([60.0, 65.0], []) == []


def test_climatology_distribution_wraps_build_distribution(nyc_bins):
    from polybot_weather.probability.climatology import climatology_distribution

    history = [60.0, 62.0, 64.0, 64.0, 66.0, 68.0, 70.0]
    dist = climatology_distribution(history, nyc_bins)
    assert math.isclose(sum(b.probability for b in dist), 1.0, abs_tol=1e-9)
    # Each bin should have at least the Laplace floor (0.5 / total).
    assert all(bp.probability > 0 for bp in dist)
