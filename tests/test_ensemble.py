"""Ensemble combine() — bias correction + climatology mixing + spread."""

from __future__ import annotations

import math

import pytest

from polybot_weather.polymarket.parsers import TempBin
from polybot_weather.probability.bins import build_distribution
from polybot_weather.probability.calibration import BiasEntry, BiasKey, BiasTable, apply_bias
from polybot_weather.probability.ensemble import combine


@pytest.fixture
def bins_3() -> list[TempBin]:
    return [
        TempBin(label="≤62°F", low=None, high=62.0),
        TempBin(label="63-65°F", low=63.0, high=65.0),
        TempBin(label="≥66°F", low=66.0, high=None),
    ]


def test_combine_with_zero_members_uses_uniform(bins_3):
    res = combine(member_values_f=[], bins=bins_3)
    assert res.member_count == 0
    assert math.isclose(sum(b.probability for b in res.distribution), 1.0, abs_tol=1e-9)
    # Uniform across bins.
    assert all(math.isclose(b.probability, 1 / 3, abs_tol=1e-9) for b in res.distribution)


def test_bias_correction_shifts_distribution(bins_3):
    members = [63.0, 64.0, 65.0]
    no_bias = combine(member_values_f=members, bins=bins_3)
    with_bias = combine(member_values_f=members, bins=bins_3, bias_correction_f=+3.0)
    # +3°F bias pushes mass from middle toward the high open-ended bin.
    assert with_bias.distribution[2].probability > no_bias.distribution[2].probability
    assert with_bias.bias_correction_f == 3.0


def test_climatology_mixing_dilutes_forecast(bins_3):
    members = [64.0] * 30
    climo = build_distribution([55.0, 58.0, 60.0, 70.0, 72.0, 75.0], bins_3)
    mixed = combine(
        member_values_f=members, bins=bins_3, climatology_dist=climo, climatology_weight=0.20
    )
    forecast_only = combine(member_values_f=members, bins=bins_3)
    # The middle bin loses probability when we mix in a fat-tailed climatology.
    assert mixed.distribution[1].probability < forecast_only.distribution[1].probability
    assert mixed.used_climatology is True


def test_spread_is_max_minus_min(bins_3):
    members = [60.0, 64.0, 70.0]
    res = combine(member_values_f=members, bins=bins_3)
    assert math.isclose(res.spread_f, 10.0, abs_tol=1e-9)


def test_apply_bias_no_op_when_zero():
    members = [63.0, 64.0]
    assert apply_bias(members, 0.0) == members
    assert apply_bias(members, 1.5) == [64.5, 65.5]


def test_bias_table_returns_zero_when_undertrained():
    tbl = BiasTable(
        entries=[
            BiasEntry(key=BiasKey("KLGA", "openmeteo_ensemble", 4), mean_error_f=2.0, sample_count=3)
        ]
    )
    # sample_count < 5 → ignored.
    assert tbl.correction_f("KLGA", "openmeteo_ensemble", 4) == 0.0


def test_bias_table_returns_correction_when_trained():
    tbl = BiasTable(
        entries=[
            BiasEntry(key=BiasKey("KLGA", "openmeteo_ensemble", 4), mean_error_f=-1.5, sample_count=42)
        ]
    )
    assert tbl.correction_f("klga", "openmeteo_ensemble", 4) == -1.5
    # Wrong month → 0.
    assert tbl.correction_f("KLGA", "openmeteo_ensemble", 5) == 0.0
