"""Build a climatology distribution over the same bins as a Polymarket market.

Pulls daily-max history (last 30 years, same calendar day) via Open-Meteo
Archive, rounds to integer °F, then bins. Used as a weak prior in
`bins.mix_with_climatology`.
"""

from __future__ import annotations

from ..polymarket.parsers import TempBin
from .bins import BinProbability, build_distribution


def climatology_distribution(historical_max_f: list[float], bins: list[TempBin]) -> list[BinProbability]:
    """Treat the historical sample as a 'pseudo-ensemble' and bin it."""
    return build_distribution(historical_max_f, bins, laplace_alpha=0.5)
