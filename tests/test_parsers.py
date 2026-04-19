"""Heuristic-parser tests against representative Polymarket weather questions."""

from __future__ import annotations

from polybot_weather.polymarket.gamma import GammaMarket
from polybot_weather.polymarket.parsers import _parse_outcome_to_bin, parse_market


def _market(question: str, outcomes: list[str], description: str = "", end_date: str = "2026-04-18T23:59:00Z", resolution_source: str | None = None) -> GammaMarket:
    return GammaMarket(
        id="x", slug="test-slug", question=question, description=description,
        end_date_iso=end_date, outcomes=outcomes, outcome_prices=["0.5"] * len(outcomes),
        clob_token_ids=["t" + str(i) for i in range(len(outcomes))],
        resolution_source=resolution_source,
    )


def test_parses_nyc_temperature_market_with_known_station():
    m = _market(
        question="Highest temperature in NYC on April 18 (KLGA, Wunderground)",
        outcomes=["≤61°F", "62-63°F", "64-65°F", "66-67°F", "68-69°F", "≥70°F"],
        description="Resolves based on KLGA peak temperature for April 18.",
    )
    parsed = parse_market(m)
    assert parsed.metric == "max_temp"
    assert parsed.station_code == "KLGA"
    assert parsed.unit == "F"
    assert parsed.resolution_date is not None
    assert parsed.resolution_date.month == 4 and parsed.resolution_date.day == 18
    assert len(parsed.bins) == 6
    # Range bin parses as inclusive integer endpoints.
    mid = parsed.bins[2]
    assert mid.low == 64.0 and mid.high == 65.0
    # Open-ended bins:
    assert parsed.bins[0].low is None and parsed.bins[0].high == 61.0
    assert parsed.bins[-1].low == 70.0 and parsed.bins[-1].high is None
    assert parsed.confidence > 0.7


def test_falls_back_to_city_when_station_missing():
    m = _market(
        question="Highest temperature in Chicago on July 4",
        outcomes=["≤80°F", "81-85°F", "86-90°F", "≥91°F"],
    )
    parsed = parse_market(m)
    assert parsed.station_code == "KORD"   # city → default station
    assert parsed.metric == "max_temp"
    assert len(parsed.bins) == 4


def test_min_temp_market():
    m = _market(
        question="Lowest temperature in Boston on January 5 (KBOS)",
        outcomes=["≤10°F", "11-15°F", "16-20°F", "≥21°F"],
    )
    parsed = parse_market(m)
    assert parsed.metric == "min_temp"
    assert parsed.station_code == "KBOS"


def test_low_confidence_when_unparseable():
    m = _market(question="Will it snow in Buffalo this week?", outcomes=["Yes", "No"])
    parsed = parse_market(m)
    # Snow market: metric is recognized but no station / no bins.
    assert parsed.metric == "snowfall"
    assert parsed.bins == []
    assert parsed.confidence < 0.6


def test_temp_bin_contains():
    m = _market(
        question="Highest temperature in NYC on April 18 (KLGA)",
        outcomes=["64-65°F"],
    )
    parsed = parse_market(m)
    b = parsed.bins[0]
    assert b.contains(64.0)
    assert b.contains(65.0)
    assert not b.contains(63.0)
    assert not b.contains(66.0)


def test_parses_london_market_in_celsius():
    m = _market(
        question="Highest temperature in London on July 15 (EGLL)",
        outcomes=["≤20°C", "21-23°C", "24-26°C", "27-29°C", "≥30°C"],
        description="Resolves based on EGLL Heathrow daily max in Celsius.",
    )
    parsed = parse_market(m)
    assert parsed.metric == "max_temp"
    assert parsed.station_code == "EGLL"
    assert parsed.unit == "C"
    assert len(parsed.bins) == 5
    assert all(b.unit == "C" for b in parsed.bins)
    assert parsed.bins[1].low == 21.0 and parsed.bins[1].high == 23.0


def test_parses_tokyo_with_native_city_name():
    m = _market(
        question="Maior temperatura em Tóquio em 5 de agosto",
        outcomes=["≤30°C", "31-33°C", "34-36°C", "≥37°C"],
    )
    parsed = parse_market(m)
    assert parsed.station_code == "RJTT"
    assert parsed.unit == "C"


def test_parses_sao_paulo_with_default_celsius():
    m = _market(
        question="Highest temperature in São Paulo on December 25 (SBGR)",
        outcomes=["≤25°C", "26-28°C", "29-31°C", "≥32°C"],
    )
    parsed = parse_market(m)
    assert parsed.station_code == "SBGR"
    assert parsed.unit == "C"
    assert parsed.bins[2].low == 29.0 and parsed.bins[2].high == 31.0


def test_negative_celsius_bins_for_winter_market():
    m = _market(
        question="Lowest temperature in Moscow on January 10 (UUEE)",
        outcomes=["≤-20°C", "-19 to -10°C", "-9 to 0°C", "≥1°C"],
    )
    parsed = parse_market(m)
    assert parsed.station_code == "UUEE"
    assert parsed.unit == "C"
    # Negative ranges should parse correctly.
    assert parsed.bins[1].low == -19.0 and parsed.bins[1].high == -10.0


def test_us_market_still_defaults_to_fahrenheit():
    m = _market(
        question="Highest temperature in Chicago on July 4 (KORD)",
        outcomes=["≤80°F", "81-85°F", "86-90°F", "≥91°F"],
    )
    parsed = parse_market(m)
    assert parsed.unit == "F"
    assert parsed.station_code == "KORD"


def test_outcome_with_suffix_or_below_parses_as_lte_bin():
    """'47°F or below' must parse as [-∞, 47], NOT as a point bin [47, 47]."""
    b = _parse_outcome_to_bin("47°F or below")
    assert b is not None
    assert b.low is None
    assert b.high == 47.0
    assert b.unit == "F"


def test_outcome_with_suffix_or_higher_parses_as_gte_bin():
    b = _parse_outcome_to_bin("62°F or higher")
    assert b is not None
    assert b.low == 62.0
    assert b.high is None


def test_outcome_with_celsius_suffix_or_below():
    b = _parse_outcome_to_bin("5°C or lower")
    assert b is not None
    assert b.low is None
    assert b.high == 5.0
    assert b.unit == "C"


def test_unit_from_outcomes_wins_over_description_boilerplate():
    """Live Polymarket descriptions contain boilerplate like 'toggle between
    Fahrenheit and Celsius' that trips _RE_UNIT_HINT. The unit embedded in the
    parsed outcomes must take priority over that text hint."""
    m = _market(
        question="Will the highest temperature in NYC be 50-51°F on April 19?",
        outcomes=["Yes", "No"],
        description=(
            "Resolves from KLGA daily max. To toggle between Fahrenheit and "
            "Celsius, click the gear icon. Temperatures reported in degrees "
            "Fahrenheit."
        ),
    )
    parsed = parse_market(m)
    assert parsed.unit == "F", f"expected F, got {parsed.unit}"
