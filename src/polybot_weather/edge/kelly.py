"""Fractional Kelly sizing.

For a YES contract priced at `ask` (so payout-on-win = 1/ask, net-odds = (1-ask)/ask):
    f_full = (p * b - q) / b      where b = (1-ask)/ask, q = 1-p
    f_used = min(kelly_fraction * f_full, max_fraction)

We always cap at max_fraction (default 5%) regardless of model confidence —
single-bet variance dominates with thin order books.
"""

from __future__ import annotations


def full_kelly_fraction(p: float, ask: float) -> float:
    """Returns the unconstrained Kelly fraction; clamps below at 0."""
    if ask <= 0 or ask >= 1:
        return 0.0
    if not 0.0 < p < 1.0:
        return 0.0
    b = (1.0 - ask) / ask
    if b <= 0:
        return 0.0
    f = (p * b - (1.0 - p)) / b
    return max(f, 0.0)


def kelly_size_usd(
    *,
    p: float,
    ask: float,
    bankroll_usd: float,
    kelly_fraction: float = 0.25,
    max_bet_fraction: float = 0.05,
) -> float:
    """Dollar size of the bet, after fractional + per-bet cap clamps."""
    if bankroll_usd <= 0:
        return 0.0
    f_full = full_kelly_fraction(p, ask)
    f_used = min(kelly_fraction * f_full, max_bet_fraction)
    return max(f_used, 0.0) * bankroll_usd
