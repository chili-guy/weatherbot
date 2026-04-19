"""Edge evaluation + Kelly sizing."""

from __future__ import annotations

import math

import pytest

from polybot_weather.edge.kelly import full_kelly_fraction, kelly_size_usd
from polybot_weather.edge.value import EdgeInputs, EdgeThresholds, evaluate


def test_recommend_when_all_thresholds_pass():
    res = evaluate(
        EdgeInputs(p_model=0.45, ask=0.36, ask_size=200, hours_to_resolution=24),
        EdgeThresholds(),
    )
    assert res.recommend
    assert res.rejection_reason is None
    # edge ≈ 9pp; EV ≈ 25%.
    assert math.isclose(res.edge, 0.09, abs_tol=1e-9)
    assert res.ev_per_dollar > 0.10


def test_reject_when_edge_below_threshold():
    res = evaluate(
        EdgeInputs(p_model=0.40, ask=0.39, ask_size=1000, hours_to_resolution=10),
    )
    assert not res.recommend
    assert "edge" in (res.rejection_reason or "")


def test_reject_when_ev_below_threshold():
    # edge passes (0.06 > 0.05) but EV is only ~8.6% — fails the EV gate.
    res = evaluate(
        EdgeInputs(p_model=0.76, ask=0.70, ask_size=1000, hours_to_resolution=10),
    )
    assert not res.recommend
    assert "EV" in (res.rejection_reason or "")


def test_reject_when_liquidity_too_thin():
    res = evaluate(
        EdgeInputs(p_model=0.50, ask=0.30, ask_size=10, hours_to_resolution=24),
    )
    # ask_size=10 * 0.30 = $3 < $50 floor.
    assert not res.recommend
    assert "liquidity" in (res.rejection_reason or "")


def test_reject_when_resolution_too_far_out():
    res = evaluate(
        EdgeInputs(p_model=0.60, ask=0.30, ask_size=1000, hours_to_resolution=200),
    )
    assert not res.recommend
    assert "resolves" in (res.rejection_reason or "")


def test_reject_when_no_ask():
    res = evaluate(EdgeInputs(p_model=0.5, ask=None, ask_size=None, hours_to_resolution=10))
    assert not res.recommend
    assert "no ask" in (res.rejection_reason or "")


def test_full_kelly_fraction_basic():
    # p=0.6, ask=0.4 → b = 0.6/0.4 = 1.5; f = (0.6*1.5 - 0.4)/1.5 = 0.5/1.5 ≈ 0.333
    f = full_kelly_fraction(0.6, 0.4)
    assert math.isclose(f, 1 / 3, abs_tol=1e-9)


def test_full_kelly_zero_when_no_edge():
    assert full_kelly_fraction(0.4, 0.5) == 0.0   # negative EV → 0
    assert full_kelly_fraction(0.4, 0.4) == 0.0   # break-even → 0


def test_full_kelly_handles_degenerate_inputs():
    assert full_kelly_fraction(0.5, 0.0) == 0.0
    assert full_kelly_fraction(0.5, 1.0) == 0.0
    assert full_kelly_fraction(0.0, 0.5) == 0.0
    assert full_kelly_fraction(1.0, 0.5) == 0.0


def test_kelly_size_capped_by_max_fraction():
    # Big edge → full Kelly would be huge. Fractional Kelly + cap clamps to 5%.
    size = kelly_size_usd(p=0.95, ask=0.30, bankroll_usd=1000.0,
                          kelly_fraction=0.25, max_bet_fraction=0.05)
    assert size == pytest.approx(50.0)


def test_kelly_size_zero_with_zero_bankroll():
    assert kelly_size_usd(p=0.7, ask=0.5, bankroll_usd=0.0) == 0.0


def test_kelly_size_uses_fractional():
    # Mild edge: p=0.55, ask=0.50 → full Kelly ≈ 0.10; quarter-Kelly ≈ 0.025.
    size = kelly_size_usd(p=0.55, ask=0.50, bankroll_usd=1000.0,
                          kelly_fraction=0.25, max_bet_fraction=0.05)
    full = full_kelly_fraction(0.55, 0.50) * 1000.0
    assert size == pytest.approx(full * 0.25)
    assert size < 0.05 * 1000.0  # below the cap
