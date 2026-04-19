"""Score historical recommendations against realized outcomes.

Metrics:
  * Brier score   — mean (p − y)²; lower is better, 0.25 is the no-skill baseline.
  * Log-loss      — mean −[y ln p + (1−y) ln(1−p)]; skill baseline depends on p̄.
  * Calibration   — bucket predictions by probability decile and compare to
                    empirical frequency.
  * Simulated P&L — for every `recommend=True` row, simulate buying
                    `kelly_size_usd / ask` contracts at `ask * (1 + fee_rate)`;
                    payout is $1 per contract if the label won.

Only Recommendations whose label we can re-parse to a temperature bin are
scored — labels like "Yes" without an embedded bin range are skipped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..polymarket.parsers import TempBin, _parse_outcome_to_bin
from ..storage.repo import Repo


@dataclass
class CalibrationBucket:
    p_low: float
    p_high: float
    predicted_mean: float
    empirical_rate: float
    count: int


@dataclass
class BacktestReport:
    n_recommended: int = 0
    n_scored: int = 0
    n_unparseable: int = 0
    brier: float | None = None
    log_loss: float | None = None
    hit_rate: float | None = None
    avg_edge: float | None = None
    simulated_pnl_usd: float = 0.0
    simulated_stake_usd: float = 0.0
    roi: float | None = None
    calibration: list[CalibrationBucket] = field(default_factory=list)


def _label_bin(label: str) -> tuple[TempBin | None, bool]:
    """Parse a Recommendation.outcome_label into (bin, negate).

    `negate=True` means a "NOT X" label — the outcome wins iff the realized
    value is OUTSIDE X.
    """
    negate = False
    clean = label.strip()
    if clean.upper().startswith("NOT "):
        negate = True
        clean = clean[4:].strip()
    return _parse_outcome_to_bin(clean), negate


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def _label_won(label: str, realized: float, realized_unit: str | None = None) -> bool | None:
    bin_, negate = _label_bin(label)
    if bin_ is None:
        return None
    # Defensive: the label's parsed unit and the DB-stored realized unit should
    # always agree today (both come from the parser), but older rows or
    # fallback-parsed labels can drift — convert rather than silently compare
    # F vs C.
    value = realized
    if realized_unit and realized_unit.upper() != bin_.unit.upper():
        if bin_.unit.upper() == "F" and realized_unit.upper() == "C":
            value = _c_to_f(realized)
        elif bin_.unit.upper() == "C" and realized_unit.upper() == "F":
            value = _f_to_c(realized)
    inside = bin_.contains(value)
    return (not inside) if negate else inside


def _bucket_calibration(
    scored: list[tuple[float, int]], n_buckets: int = 10
) -> list[CalibrationBucket]:
    if not scored:
        return []
    width = 1.0 / n_buckets
    buckets: list[CalibrationBucket] = []
    for i in range(n_buckets):
        lo, hi = i * width, (i + 1) * width
        subset = [(p, y) for p, y in scored if lo <= p < hi or (i == n_buckets - 1 and p == hi)]
        if not subset:
            continue
        preds = [p for p, _ in subset]
        ys = [y for _, y in subset]
        buckets.append(
            CalibrationBucket(
                p_low=lo, p_high=hi,
                predicted_mean=sum(preds) / len(preds),
                empirical_rate=sum(ys) / len(ys),
                count=len(subset),
            )
        )
    return buckets


def backtest(*, repo: Repo, from_date=None, fee_rate: float = 0.0) -> BacktestReport:
    rows = repo.recommendations_with_outcomes(from_date=from_date)
    report = BacktestReport()

    # Dedup: the same "bet" gets re-written to `recommendation` every time the
    # dashboard rescans the market. If we scored every row we'd simulate the
    # same bet 100+ times and all metrics (Brier, ROI, hit rate) would just
    # reflect the scan cadence. Collapse to the LATEST (forecast.run_at) row
    # per (market_id, outcome_label) — treats it as one bet.
    latest: dict[tuple[int, str], tuple] = {}
    for row in rows:
        rec, outcome, market, forecast = row
        key = (market.id, rec.outcome_label)
        prev = latest.get(key)
        if prev is None or forecast.run_at > prev[3].run_at:
            latest[key] = row

    scored: list[tuple[float, int]] = []    # (p_model, y) for Brier/log-loss/calibration
    edges: list[float] = []

    for rec, outcome, market, forecast in latest.values():
        if not rec.recommend:
            continue
        report.n_recommended += 1
        if outcome.realized_value is None or rec.ask is None or rec.ask <= 0:
            report.n_unparseable += 1
            continue
        won = _label_won(rec.outcome_label, outcome.realized_value, market.unit)
        if won is None:
            report.n_unparseable += 1
            continue
        y = 1 if won else 0
        scored.append((rec.p_model, y))
        edges.append(rec.edge)

        # Reality: `kelly_size_usd` is the TOTAL capital committed. The 5%
        # taker fee is priced into the effective ask, so you buy fewer
        # contracts — you don't pay stake*(1+fee) on top. Inflating the stake
        # overstated both PnL magnitude and max-drawdown.
        effective_ask = rec.ask * (1.0 + fee_rate)
        contracts = rec.kelly_size_usd / effective_ask if rec.kelly_size_usd > 0 else 0.0
        stake = rec.kelly_size_usd
        payout = contracts * y
        report.simulated_pnl_usd += payout - stake
        report.simulated_stake_usd += stake

    report.n_scored = len(scored)
    if scored:
        report.brier = sum((p - y) ** 2 for p, y in scored) / len(scored)
        # Clamp p ∈ (ε, 1−ε) to avoid log(0) explosions.
        eps = 1e-6
        report.log_loss = -sum(
            y * math.log(max(min(p, 1 - eps), eps))
            + (1 - y) * math.log(max(min(1 - p, 1 - eps), eps))
            for p, y in scored
        ) / len(scored)
        report.hit_rate = sum(y for _, y in scored) / len(scored)
        report.calibration = _bucket_calibration(scored)
    if edges:
        report.avg_edge = sum(edges) / len(edges)
    if report.simulated_stake_usd > 0:
        report.roi = report.simulated_pnl_usd / report.simulated_stake_usd

    return report
