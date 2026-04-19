"""Expected-value calculations and recommendation gating.

For a YES contract priced at `ask`, paying $1 if it resolves YES:
    edge   = p_model - ask                  (in probability points)
    EV/$  = (p_model / ask) - 1             (expected return per dollar invested)

We recommend a buy iff:
    edge > min_edge
    AND EV/$ > min_ev
    AND ask-side liquidity * ask >= min_liquidity_usd
    AND hours-to-resolution <= max_hours
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeInputs:
    p_model: float
    ask: float | None
    ask_size: float | None  # in contracts; ask_size * ask = USD on offer
    hours_to_resolution: float | None


@dataclass(frozen=True)
class EdgeThresholds:
    min_edge: float = 0.05
    min_ev: float = 0.10
    min_liquidity_usd: float = 50.0
    max_hours_to_resolution: float = 72.0
    fee_rate: float = 0.0  # multiplicative taker fee on the ask (0.05 = 5%)


@dataclass(frozen=True)
class EdgeResult:
    edge: float                  # p_model - ask  (NaN-substitute: -1.0 if ask missing)
    ev_per_dollar: float         # p_model/ask - 1
    liquidity_usd: float
    recommend: bool
    rejection_reason: str | None


_DEFAULT_THRESHOLDS = EdgeThresholds()


def evaluate(inputs: EdgeInputs, thresholds: EdgeThresholds = _DEFAULT_THRESHOLDS) -> EdgeResult:
    if inputs.ask is None or inputs.ask <= 0:
        return EdgeResult(edge=-1.0, ev_per_dollar=-1.0, liquidity_usd=0.0,
                          recommend=False, rejection_reason="no ask price")

    # Effective entry cost = ask + fee. Polymarket's 5% taker fee eats directly
    # into edge, so treat it as if we were buying at ask*(1+fee_rate).
    effective_ask = inputs.ask * (1.0 + thresholds.fee_rate)
    edge = inputs.p_model - effective_ask
    ev = (inputs.p_model / effective_ask) - 1.0
    liquidity = (inputs.ask_size or 0.0) * inputs.ask

    if edge <= thresholds.min_edge:
        return EdgeResult(edge=edge, ev_per_dollar=ev, liquidity_usd=liquidity,
                          recommend=False, rejection_reason=f"edge {edge:.3f} <= min {thresholds.min_edge}")
    if ev <= thresholds.min_ev:
        return EdgeResult(edge=edge, ev_per_dollar=ev, liquidity_usd=liquidity,
                          recommend=False, rejection_reason=f"EV {ev:.3f} <= min {thresholds.min_ev}")
    if liquidity < thresholds.min_liquidity_usd:
        return EdgeResult(edge=edge, ev_per_dollar=ev, liquidity_usd=liquidity,
                          recommend=False, rejection_reason=f"liquidity ${liquidity:.0f} < min ${thresholds.min_liquidity_usd:.0f}")
    if inputs.hours_to_resolution is None or inputs.hours_to_resolution > thresholds.max_hours_to_resolution:
        return EdgeResult(edge=edge, ev_per_dollar=ev, liquidity_usd=liquidity,
                          recommend=False, rejection_reason=f"resolves in {inputs.hours_to_resolution}h > max {thresholds.max_hours_to_resolution}h")

    return EdgeResult(edge=edge, ev_per_dollar=ev, liquidity_usd=liquidity,
                      recommend=True, rejection_reason=None)
