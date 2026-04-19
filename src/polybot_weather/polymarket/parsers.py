"""Heuristic parsers for Polymarket weather-market metadata.

The Gamma API returns free-form `question` / `description` / `resolutionSource`
strings. We extract:

  * `city`            — short city slug if recognized
  * `station_code`    — official ICAO/ASOS code if mentioned (KLGA, KNYC, KORD…)
  * `metric`          — "max_temp" | "min_temp" | "snowfall" | "rainfall" | "hurricane"
  * `unit`            — "F" | "C" | "in" | "mm" | None
  * `bins`            — ordered list of `TempBin` for outcome → numeric range
  * `resolution_date` — date the market resolves on (in city-local time)

Heuristic by design — the spec says: when the bot is unsure, it should *not*
silently invent a parse. We expose `confidence` so callers can skip ambiguous
markets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .gamma import GammaMarket

Metric = Literal["max_temp", "min_temp", "snowfall", "rainfall", "hurricane", "unknown"]


@dataclass(frozen=True)
class TempBin:
    """Inclusive low / inclusive high temperature bin in integer degrees."""

    label: str             # original outcome label, e.g. "64-65°F" or "≥70°F"
    low: float | None      # None means unbounded below
    high: float | None     # None means unbounded above
    unit: str = "F"

    def contains(self, value: float) -> bool:
        if self.low is not None and value < self.low:
            return False
        if self.high is not None and value > self.high:
            return False
        return True


@dataclass
class ParsedMarket:
    market_id: str
    slug: str
    metric: Metric
    unit: str | None
    city: str | None = None
    station_code: str | None = None
    resolution_date: date | None = None
    bins: list[TempBin] = field(default_factory=list)
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def is_temperature(self) -> bool:
        return self.metric in ("max_temp", "min_temp")


# Recognized ICAO codes — single source of truth lives in `weather.stations`.
# We import lazily to avoid circular import when stations imports parsers later.
def _known_stations() -> set[str]:
    from ..weather.stations import STATIONS
    return set(STATIONS.keys())


# Pulled in once at import time. Keep in sync with `weather.stations.STATIONS`.
from ..weather.stations import STATIONS as _STATIONS  # noqa: E402

KNOWN_STATIONS: set[str] = set(_STATIONS.keys())


# Maps lowercase city name (English + native) to default ICAO. Used as a
# fallback when the market text doesn't quote an explicit station code.
CITY_TO_DEFAULT_STATION: dict[str, str] = {
    # === US ===
    "nyc": "KNYC", "new york": "KNYC", "manhattan": "KNYC", "nova york": "KNYC",
    "chicago": "KORD",
    "washington": "KDCA", "dc": "KDCA",
    "boston": "KBOS",
    "atlanta": "KATL",
    "miami": "KMIA",
    "los angeles": "KLAX", "la": "KLAX",
    "san francisco": "KSFO", "sf": "KSFO",
    "seattle": "KSEA",
    "denver": "KDEN",
    "phoenix": "KPHX",
    "las vegas": "KLAS",
    "houston": "KIAH",
    "dallas": "KDFW",

    # === Europe ===
    "london": "EGLL", "londres": "EGLL",
    "paris": "LFPG",
    "berlin": "EDDB", "berlim": "EDDB",
    "frankfurt": "EDDF",
    "madrid": "LEMD", "madri": "LEMD",
    "barcelona": "LEBL",
    "rome": "LIRF", "roma": "LIRF",
    "amsterdam": "EHAM", "amsterdã": "EHAM",
    "zurich": "LSZH", "zurique": "LSZH",
    "vienna": "LOWW", "viena": "LOWW",
    "warsaw": "EPWA", "varsóvia": "EPWA",
    "istanbul": "LTBA", "istambul": "LTBA",
    "moscow": "UUEE", "moscou": "UUEE",
    "copenhagen": "EKCH", "copenhague": "EKCH",
    "stockholm": "ESSA", "estocolmo": "ESSA",

    # === Asia / Pacific ===
    "tokyo": "RJTT", "tóquio": "RJTT",
    "seoul": "RKSI", "seul": "RKSI",
    "beijing": "ZBAA", "pequim": "ZBAA",
    "shanghai": "ZSPD", "xangai": "ZSPD",
    "hong kong": "VHHH",
    "shenzhen": "ZGSZ",
    "guangzhou": "ZGGG", "cantão": "ZGGG",
    "taipei": "RCTP", "taipé": "RCTP",
    "bangkok": "VTBS", "banguecoque": "VTBS",
    "hanoi": "VVNB", "hanói": "VVNB",
    "ho chi minh": "VVTS", "saigon": "VVTS",
    "manila": "RPLL",
    "kuala lumpur": "WMKK",
    "jakarta": "WIII", "jacarta": "WIII",
    "singapore": "WSSS", "singapura": "WSSS",
    "mumbai": "VABB", "bombaim": "VABB",
    "delhi": "VIDP", "nova delhi": "VIDP",
    "dubai": "OMDB",
    "doha": "OTHH",
    "sydney": "YSSY",
    "melbourne": "YMML",

    # === Canada ===
    "toronto": "CYYZ",
    "montreal": "CYUL",
    "vancouver": "CYVR",

    # === Latin America ===
    "são paulo": "SBGR", "sao paulo": "SBGR",
    "rio de janeiro": "SBRJ", "rio": "SBRJ",
    "brasília": "SBBR", "brasilia": "SBBR",
    "mexico city": "MMMX", "cidade do méxico": "MMMX", "ciudad de méxico": "MMMX",
    "buenos aires": "SABE",
    "santiago": "SCEL",
    "bogotá": "SKBO", "bogota": "SKBO",
    "lima": "SPJC",

    # === Africa ===
    "cape town": "FACT", "cidade do cabo": "FACT",
    "johannesburg": "FAJS", "joanesburgo": "FAJS",
    "cairo": "HECA",
    "lagos": "DNMM",
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    # Portuguese months
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}

# Temperature regexes — `unit` group is "F" or "C", optionally with the °
# symbol. Matching works for "64-65°F", "64 to 65 F", "20-22°C", "≥30°C".
_RE_RANGE = re.compile(
    r"(-?\d{1,3})\s*[-–to]+\s*(-?\d{1,3})\s*°?\s*([FC])\b", re.IGNORECASE
)
_RE_GTE = re.compile(
    r"(?:≥|>=|or higher|or above|or more)\s*(-?\d{1,3})\s*°?\s*([FC])\b", re.IGNORECASE
)
_RE_LTE = re.compile(
    r"(?:≤|<=|or lower|or below|or less)\s*(-?\d{1,3})\s*°?\s*([FC])\b", re.IGNORECASE
)
# Suffix-form: "47°F or below" / "47 F or higher" — number BEFORE the qualifier.
# Real Polymarket outcomes use this form much more often than the prefix form.
_RE_GTE_SUFFIX = re.compile(
    r"(-?\d{1,3})\s*°?\s*([FC])\s*(?:or higher|or above|or more|\+)", re.IGNORECASE
)
_RE_LTE_SUFFIX = re.compile(
    r"(-?\d{1,3})\s*°?\s*([FC])\s*(?:or lower|or below|or less)", re.IGNORECASE
)
_RE_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|october|november|december|"
    r"janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)"
    r"\s+(\d{1,2})(?:,\s*(\d{4}))?\b",
    re.IGNORECASE,
)
# Any 4-letter ICAO; we still validate against KNOWN_STATIONS to drop random
# 4-letter capital tokens.
_RE_STATION = re.compile(r"\b([A-Z]{4})\b")
# Falls back to °F unless the market text explicitly mentions °C / Celsius.
# Require the "C" to be preceded by °, a digit, or appear as the word celsius/centigrade —
# otherwise city names like "NYC" trigger a false positive.
_RE_UNIT_HINT = re.compile(r"°\s*C\b|\d\s*C\b|\bcelsius\b|\bcentigrade\b", re.IGNORECASE)
_RE_SINGLE = re.compile(r"\b(-?\d{1,3})\s*°?\s*([FC])\b", re.IGNORECASE)


def _parse_outcome_to_bin(label: str, default_unit: str = "F") -> TempBin | None:
    s = label.strip()
    m = _RE_RANGE.search(s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        unit = m.group(2 + 1).upper()  # group 3 is the unit letter
        return TempBin(label=label, low=float(min(lo, hi)), high=float(max(lo, hi)), unit=unit)
    m = _RE_GTE.search(s)
    if m:
        return TempBin(label=label, low=float(m.group(1)), high=None, unit=m.group(2).upper())
    m = _RE_LTE.search(s)
    if m:
        return TempBin(label=label, low=None, high=float(m.group(1)), unit=m.group(2).upper())
    # Suffix-form MUST be checked before `_RE_SINGLE` — otherwise "47°F or below"
    # gets captured as a point bin [47,47].
    m = _RE_GTE_SUFFIX.search(s)
    if m:
        return TempBin(label=label, low=float(m.group(1)), high=None, unit=m.group(2).upper())
    m = _RE_LTE_SUFFIX.search(s)
    if m:
        return TempBin(label=label, low=None, high=float(m.group(1)), unit=m.group(2).upper())
    m = _RE_SINGLE.search(s)
    if m:
        val = float(m.group(1))
        return TempBin(label=label, low=val, high=val, unit=m.group(2).upper())
    # Fall back: if the label has plain digits but no unit, use the market's
    # default unit (driven by station / city). Only attempt for a plain
    # range-like label to keep this path conservative.
    m_plain = re.search(r"(-?\d{1,3})\s*[-–to]+\s*(-?\d{1,3})", s)
    if m_plain:
        lo, hi = int(m_plain.group(1)), int(m_plain.group(2))
        return TempBin(
            label=label, low=float(min(lo, hi)), high=float(max(lo, hi)), unit=default_unit
        )
    return None


def _detect_metric(text: str) -> Metric:
    t = text.lower()
    if (
        "highest temperature" in t
        or "high temperature" in t
        or "max temperature" in t
        or "temperatura mais alta" in t
        or "temperatura máxima" in t
    ):
        return "max_temp"
    if (
        "lowest temperature" in t
        or "low temperature" in t
        or "min temperature" in t
        or "temperatura mais baixa" in t
        or "temperatura mínima" in t
    ):
        return "min_temp"
    if (
        "hurricane" in t
        or "tropical storm" in t
        or "named storm" in t
        or "furacão" in t
        or "tempestade tropical" in t
    ):
        return "hurricane"
    if "snow" in t or "neve" in t:
        return "snowfall"
    if "rain" in t or "rainfall" in t or "chuva" in t or "precipitação" in t:
        return "rainfall"
    if "temperature" in t or "temperatura" in t:
        return "max_temp"  # default temperature interpretation
    return "unknown"


def _detect_station(text: str) -> str | None:
    m = _RE_STATION.search(text or "")
    if m and m.group(1) in KNOWN_STATIONS:
        return m.group(1)
    return None


_CITY_PATTERNS = sorted(
    ((re.compile(r"\b" + re.escape(c) + r"\b", re.IGNORECASE), c) for c in CITY_TO_DEFAULT_STATION),
    key=lambda x: -len(x[1]),
)


def _detect_city(text: str) -> str | None:
    """Word-boundary match — substring match traps short keys like 'la' in
    'April', 'sf' in 'safe', 'dc' in 'adc'. Longer keys are checked first so
    'new york' wins over 'york' if both were registered."""
    t = text or ""
    for pat, city in _CITY_PATTERNS:
        if pat.search(t):
            return city
    return None


def _detect_date(text: str, fallback_year: int | None = None) -> date | None:
    m = _RE_DATE.search(text or "")
    if not m:
        return None
    month = MONTHS.get(m.group(1).lower())
    if month is None:
        return None
    day = int(m.group(2))
    year = int(m.group(3)) if m.group(3) else (fallback_year or date.today().year)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _resolve_default_unit(text: str, station_code: str | None) -> str:
    """Pick °F or °C as the market's default unit.

    Priority:
      1. Explicit "°C" / "celsius" in the text wins.
      2. Station's natural unit (US → F, rest of world → C).
      3. Fall back to °F.
    """
    if _RE_UNIT_HINT.search(text):
        return "C"
    if station_code:
        st = _STATIONS.get(station_code.upper())
        if st:
            return st.default_unit
    return "F"


def parse_market(market: GammaMarket) -> ParsedMarket:
    text_blob = " ".join(
        filter(
            None,
            [market.question, market.description or "", market.resolution_source or ""],
        )
    )

    metric = _detect_metric(text_blob)
    station = _detect_station(text_blob)
    city = _detect_city(text_blob)
    if station is None and city is not None:
        station = CITY_TO_DEFAULT_STATION.get(city)

    default_unit = _resolve_default_unit(text_blob, station)

    fallback_year: int | None = None
    if market.end_date_iso:
        try:
            fallback_year = int(market.end_date_iso[:4])
        except ValueError:
            fallback_year = None
    res_date = _detect_date(text_blob, fallback_year)

    bins: list[TempBin] = []
    is_binary = len(market.outcomes) == 2 and any(o.lower() == "yes" for o in market.outcomes)

    if is_binary:
        # For Yes/No markets, the bin is often described in the question/slug.
        # e.g. "Will it be 28°C?" -> Bin is [28, 28]
        b = _parse_outcome_to_bin(market.question, default_unit=default_unit)
        if b is None:
            # Try slug as fallback
            cleaned_slug = market.slug.replace("-", " ")
            b = _parse_outcome_to_bin(cleaned_slug, default_unit=default_unit)
        
        if b:
            # For binary markets, we only have one 'positive' bin.
            # We represent this as a single-item list.
            bins.append(b)
    else:
        for outcome in market.outcomes:
            b = _parse_outcome_to_bin(outcome, default_unit=default_unit)
            if b is not None:
                bins.append(b)

    confidence = 0.0
    notes: list[str] = []
    if metric != "unknown":
        confidence += 0.25
    else:
        notes.append("metric not recognized in market text")
    if station:
        confidence += 0.30
    else:
        notes.append("no resolution station identified")
    if res_date:
        confidence += 0.15
    else:
        notes.append("no explicit resolution date parsed")
    if bins and metric in ("max_temp", "min_temp") and (is_binary or len(bins) == len(market.outcomes)):
        confidence += 0.30
    elif metric in ("max_temp", "min_temp"):
        if is_binary:
            notes.append("could not parse target bin from binary market text")
        else:
            notes.append(f"parsed {len(bins)} bins out of {len(market.outcomes)} outcomes")

    # Resolved market unit: prefer the unit embedded in the parsed outcomes
    # (e.g. "50-51°F" carries °F), which is stronger evidence than a text-blob
    # hint. The description often contains boilerplate like "toggle between
    # Fahrenheit and Celsius" that would otherwise flip unit=C on a °F market.
    market_unit: str | None = default_unit if metric in ("max_temp", "min_temp") else None
    if bins and metric in ("max_temp", "min_temp"):
        units_from_bins = [b.unit.upper() for b in bins if b.unit]
        n_f = sum(1 for u in units_from_bins if u == "F")
        n_c = sum(1 for u in units_from_bins if u == "C")
        if n_f and n_f >= n_c:
            market_unit = "F"
        elif n_c and n_c > n_f:
            market_unit = "C"

    return ParsedMarket(
        market_id=market.id,
        slug=market.slug,
        metric=metric,
        unit=market_unit,
        city=city,
        station_code=station,
        resolution_date=res_date,
        bins=bins,
        confidence=min(confidence, 1.0),
        notes=notes,
    )
