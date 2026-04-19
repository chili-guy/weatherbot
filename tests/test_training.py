"""Calibrator + backtester — end-to-end over a synthetic SQLite DB."""

from __future__ import annotations

from datetime import datetime

import pytest

from polybot_weather.probability.calibration import BiasKey
from polybot_weather.storage.models import Forecast, Market, Outcome, Recommendation
from polybot_weather.storage.repo import Repo
from polybot_weather.training.backtester import _label_won, backtest
from polybot_weather.training.calibrator import recalibrate


@pytest.fixture
def repo(tmp_path):
    return Repo(f"sqlite:///{tmp_path / 'test.db'}")


def _seed_market(repo: Repo, *, polymarket_id: str, slug: str, question: str,
                 station: str, metric: str, date_str: str) -> int:
    return repo.upsert_market(
        polymarket_id=polymarket_id, slug=slug, question=question,
        metric=metric, station_code=station,
        resolution_date=datetime.fromisoformat(date_str),
    )


def _seed_forecast(repo: Repo, *, market_id: int, mean_f: float) -> int:
    return repo.record_forecast(
        market_id=market_id, member_count=30, bias_correction_f=0.0,
        used_climatology=False, spread_f=2.0, forecast_mean_f=mean_f,
    )


def test_label_won_binary_yes_and_not():
    # "64-65°F" wins if realized in [64,65]; "NOT 64-65°F" inverts.
    assert _label_won("64-65°F", 64.5) is True
    assert _label_won("64-65°F", 70.0) is False
    assert _label_won("NOT 64-65°F", 70.0) is True
    assert _label_won("NOT 64-65°F", 64.5) is False


def test_label_won_unparseable_is_none():
    assert _label_won("Yes", 55.0) is None


def test_recalibrate_aggregates_signed_errors(repo: Repo):
    mid = _seed_market(
        repo, polymarket_id="p1", slug="s1", question="q",
        station="KLGA", metric="max_temp", date_str="2026-04-01",
    )
    # Five forecasts, one outcome (realized=60), forecast mean=55 → error +5.
    _seed_forecast(repo, market_id=mid, mean_f=55.0)
    repo.record_outcome(market_id=mid, winning_outcome_label="max_temp=60F", realized_value=60.0)
    # Need five samples for the table to accept; seed four more distinct markets.
    for i in range(4):
        m2 = _seed_market(
            repo, polymarket_id=f"p{i+2}", slug=f"s{i+2}", question="q",
            station="KLGA", metric="max_temp", date_str="2026-04-05",
        )
        _seed_forecast(repo, market_id=m2, mean_f=55.0)
        repo.record_outcome(market_id=m2, winning_outcome_label="x", realized_value=60.0)

    summary = recalibrate(repo=repo)
    assert summary.entries_written == 1
    table = repo.load_bias_table()
    # April, KLGA, openmeteo_ensemble → +5°F correction.
    assert table.correction_f("KLGA", "openmeteo_ensemble", 4) == pytest.approx(5.0)


def test_recalibrate_skips_below_min_samples(repo: Repo):
    mid = _seed_market(
        repo, polymarket_id="p1", slug="s1", question="q",
        station="KLGA", metric="max_temp", date_str="2026-04-01",
    )
    _seed_forecast(repo, market_id=mid, mean_f=55.0)
    repo.record_outcome(market_id=mid, winning_outcome_label="x", realized_value=60.0)
    summary = recalibrate(repo=repo)
    assert summary.entries_written == 0
    assert summary.entries_skipped_insufficient_samples == 1


def test_backtest_scores_and_computes_pnl(repo: Repo):
    mid = _seed_market(
        repo, polymarket_id="p1", slug="s1", question="q",
        station="KLGA", metric="max_temp", date_str="2026-04-01",
    )
    fid = _seed_forecast(repo, market_id=mid, mean_f=64.5)
    # Recommendation to buy "64-65°F" at ask=0.30 with $30 stake (100 contracts).
    repo.record_recommendation(
        forecast_id=fid, outcome_label="64-65°F",
        p_model=0.60, ask=0.30, mid=0.35, edge=0.30,
        ev_per_dollar=1.0, liquidity_usd=500.0, kelly_size_usd=30.0,
        recommend=True, rejection_reason=None,
    )
    # Realized 64.5 → label wins → $100 payout on $30 stake (no fees).
    repo.record_outcome(market_id=mid, winning_outcome_label="x", realized_value=64.5)

    report = backtest(repo=repo, fee_rate=0.0)
    assert report.n_scored == 1
    assert report.hit_rate == 1.0
    assert report.brier == pytest.approx((0.60 - 1.0) ** 2)
    assert report.simulated_pnl_usd == pytest.approx(70.0)  # 100 − 30
    assert report.roi == pytest.approx(70.0 / 30.0)


def test_recalibrate_dedups_multiple_forecasts_per_market(repo: Repo):
    """A single market with 10 forecast rows must count as ONE residual sample,
    not ten — otherwise re-scans would dominate the bias bucket."""
    # Five distinct markets → five residuals (meets MIN_SAMPLES=5).
    for i in range(5):
        mid = _seed_market(
            repo, polymarket_id=f"p{i}", slug=f"s{i}", question="q",
            station="KLGA", metric="max_temp", date_str="2026-04-01",
        )
        # Pollute with ten rescans per market, with WILDLY different means.
        # Only the latest (run_at max) should drive the residual.
        for _ in range(9):
            _seed_forecast(repo, market_id=mid, mean_f=999.0)  # trash
        _seed_forecast(repo, market_id=mid, mean_f=55.0)  # latest — true value
        repo.record_outcome(
            market_id=mid, winning_outcome_label="x", realized_value=60.0,
        )

    summary = recalibrate(repo=repo)
    assert summary.entries_written == 1
    table = repo.load_bias_table()
    # Without dedup the mean would be dragged toward (60 - 999) ≈ -939. With
    # dedup keeping only the latest (55.0) → residual = +5.
    assert table.correction_f("KLGA", "openmeteo_ensemble", 4) == pytest.approx(5.0)


def test_backtest_dedups_multiple_recommendations_per_market(repo: Repo):
    """Same market scanned 100 times should NOT be counted 100 bets."""
    mid = _seed_market(
        repo, polymarket_id="p1", slug="s1", question="q",
        station="KLGA", metric="max_temp", date_str="2026-04-01",
    )
    # Ten rescans → ten (forecast, recommendation) pairs for the same bet.
    for _ in range(10):
        fid = _seed_forecast(repo, market_id=mid, mean_f=64.5)
        repo.record_recommendation(
            forecast_id=fid, outcome_label="64-65°F",
            p_model=0.60, ask=0.30, mid=0.35, edge=0.30, ev_per_dollar=1.0,
            liquidity_usd=500.0, kelly_size_usd=30.0, recommend=True,
            rejection_reason=None,
        )
    repo.record_outcome(market_id=mid, winning_outcome_label="x", realized_value=64.5)

    report = backtest(repo=repo, fee_rate=0.0)
    # One market × one bet — not ten.
    assert report.n_scored == 1
    assert report.simulated_pnl_usd == pytest.approx(70.0)


def test_label_won_handles_unit_mismatch_via_conversion():
    # Bin parsed in °F, realized stored in °C — must convert before comparing.
    # 20°C ≈ 68°F → outside a "64-65°F" bin.
    assert _label_won("64-65°F", 20.0, "C") is False
    # 18.2°C ≈ 64.76°F → inside.
    assert _label_won("64-65°F", 18.2, "C") is True


def test_backtest_fee_eats_into_pnl(repo: Repo):
    mid = _seed_market(
        repo, polymarket_id="p1", slug="s1", question="q",
        station="KLGA", metric="max_temp", date_str="2026-04-01",
    )
    fid = _seed_forecast(repo, market_id=mid, mean_f=64.5)
    repo.record_recommendation(
        forecast_id=fid, outcome_label="64-65°F",
        p_model=0.60, ask=0.30, mid=0.35, edge=0.30, ev_per_dollar=1.0,
        liquidity_usd=500.0, kelly_size_usd=30.0, recommend=True, rejection_reason=None,
    )
    # Losing trade: stake=$30 all in, 0 payout → pnl=-30 (fee eats contract
    # count but not extra cash on top of stake).
    repo.record_outcome(market_id=mid, winning_outcome_label="x", realized_value=70.0)
    report = backtest(repo=repo, fee_rate=0.05)
    assert report.simulated_pnl_usd == pytest.approx(-30.0)
    assert report.hit_rate == 0.0


def test_backtest_fee_reduces_winning_payout(repo: Repo):
    """Fee affects the contract count you can buy — a 5% fee should shave
    the payout of a winning bet by ~5%."""
    mid = _seed_market(
        repo, polymarket_id="p1", slug="s1", question="q",
        station="KLGA", metric="max_temp", date_str="2026-04-01",
    )
    fid = _seed_forecast(repo, market_id=mid, mean_f=64.5)
    repo.record_recommendation(
        forecast_id=fid, outcome_label="64-65°F",
        p_model=0.60, ask=0.30, mid=0.35, edge=0.30, ev_per_dollar=1.0,
        liquidity_usd=500.0, kelly_size_usd=30.0, recommend=True, rejection_reason=None,
    )
    repo.record_outcome(market_id=mid, winning_outcome_label="x", realized_value=64.5)
    report = backtest(repo=repo, fee_rate=0.05)
    # contracts = 30 / (0.30 * 1.05) = 95.238…  payout = $95.24  pnl = $65.24
    assert report.simulated_pnl_usd == pytest.approx(30.0 / (0.30 * 1.05) - 30.0)
