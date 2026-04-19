"""Bias correction by (station × model × month).

The bias table starts empty and is filled by `polybot calibrate`, which scans
historical forecasts against observed outcomes stored in SQLite. At inference
time we shift each ensemble member by the running mean error.

Lookup contract: callers supply (station, model, month) and we return the
correction in °F (signed: positive → model under-predicts, so we ADD this to
the raw forecast).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BiasKey:
    station: str
    model: str
    month: int  # 1..12


@dataclass(frozen=True)
class BiasEntry:
    key: BiasKey
    mean_error_f: float
    sample_count: int


class BiasTable:
    """In-memory bias table. Persistence happens via storage/repo.py."""

    def __init__(self, entries: list[BiasEntry] | None = None) -> None:
        self._by_key: dict[BiasKey, BiasEntry] = {}
        if entries:
            for e in entries:
                self._by_key[e.key] = e

    def correction_f(self, station: str, model: str, month: int) -> float:
        key = BiasKey(station=station.upper(), model=model.lower(), month=month)
        entry = self._by_key.get(key)
        if entry is None or entry.sample_count < 5:
            return 0.0
        return entry.mean_error_f

    def upsert(self, entry: BiasEntry) -> None:
        self._by_key[entry.key] = entry

    def __len__(self) -> int:
        return len(self._by_key)


def apply_bias(values_f: list[float], correction_f: float) -> list[float]:
    """Shift every member by the bias correction. No-op if correction == 0."""
    if correction_f == 0.0:
        return list(values_f)
    return [v + correction_f for v in values_f]
