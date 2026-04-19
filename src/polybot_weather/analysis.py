"""End-to-end analysis pipeline: market → forecast → distribution → edge.

The CLI calls into here. Everything below is pure orchestration of the lower-
level modules (`polymarket/`, `weather/`, `probability/`, `edge/`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time as _time, timezone as _timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from .config import Settings
from .edge.kelly import kelly_size_usd
from .edge.value import EdgeInputs, EdgeResult, EdgeThresholds, evaluate
from .polymarket.clob import ClobClient, OrderBook
from .polymarket.gamma import GammaClient, GammaMarket
from .polymarket.parsers import ParsedMarket, parse_market
from .probability.bins import BinProbability, round_to_resolution
from .probability.calibration import BiasTable, apply_bias
from .probability.climatology import climatology_distribution
from .probability.ensemble import EnsembleResult, combine
from .storage.repo import Repo
from .weather.cache import JsonCache
from .weather.openmeteo import OpenMeteoClient
from .weather.stations import Station, get_station

log = structlog.get_logger(__name__)


@dataclass
class OutcomeAnalysis:
    label: str
    p_model: float
    ask: float | None
    mid: float | None
    edge: EdgeResult
    kelly_size_usd: float


@dataclass
class MarketAnalysis:
    market: GammaMarket
    parsed: ParsedMarket
    station: Station | None
    ensemble: EnsembleResult | None
    outcomes: list[OutcomeAnalysis] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)
    error: str | None = None

    def has_recommendation(self) -> bool:
        return any(o.edge.recommend for o in self.outcomes)


def _hours_to_resolution(parsed: ParsedMarket, station: Station | None) -> float | None:
    if parsed.resolution_date is None:
        return None
    tz = ZoneInfo(station.timezone) if station else ZoneInfo("UTC")
    res_local = datetime.combine(parsed.resolution_date, datetime.max.time(), tzinfo=tz)
    now = datetime.now(tz)
    return max((res_local - now).total_seconds() / 3600.0, 0.0)


async def _fetch_book_for_outcome(clob: ClobClient, token_id: str) -> OrderBook | None:
    return await clob.book(token_id)


def _pick_token_id(market: GammaMarket, outcome_label: str) -> str | None:
    """Map an outcome label back to the CLOB token id.

    Gamma returns parallel arrays `outcomes` and `clobTokenIds` in the same
    order, so we just align by index.
    """
    try:
        idx = market.outcomes.index(outcome_label)
    except ValueError:
        return None
    if idx >= len(market.clob_token_ids):
        return None
    return market.clob_token_ids[idx]


async def analyze_market(
    *,
    market: GammaMarket,
    settings: Settings,
    repo: Repo | None = None,
) -> MarketAnalysis:
    parsed = parse_market(market)

    if not parsed.is_temperature:
        return MarketAnalysis(
            market=market,
            parsed=parsed,
            station=None,
            ensemble=None,
            error=f"non-temperature markets not yet supported (metric={parsed.metric})",
        )
    if parsed.station_code is None:
        return MarketAnalysis(
            market=market, parsed=parsed, station=None, ensemble=None,
            error="could not identify resolution station",
        )
    if parsed.resolution_date is None:
        return MarketAnalysis(
            market=market, parsed=parsed, station=None, ensemble=None,
            error="could not parse resolution date",
        )
    if not parsed.bins:
        return MarketAnalysis(
            market=market, parsed=parsed, station=None, ensemble=None,
            error="could not parse bins from outcomes",
        )

    station = get_station(parsed.station_code)
    if station is None:
        return MarketAnalysis(
            market=market, parsed=parsed, station=None, ensemble=None,
            error=f"station {parsed.station_code} not in stations DB",
        )

    forecast_cache = JsonCache(settings.cache_dir / "forecast", settings.forecast_ttl_seconds)
    clim_cache = JsonCache(settings.cache_dir / "climatology", settings.climatology_ttl_seconds)

    om = OpenMeteoClient(user_agent=settings.user_agent, cache=forecast_cache)
    om_clim = OpenMeteoClient(user_agent=settings.user_agent, cache=clim_cache)
    clob = ClobClient(user_agent=settings.user_agent)

    bias_table: BiasTable = repo.load_bias_table() if repo else BiasTable()

    sources_failed: list[str] = []

    # Use the unit declared by the parser (driven by station + market text).
    unit = parsed.unit or station.default_unit

    ens_members = await om.ensemble_for_date(
        station.lat, station.lon, parsed.resolution_date, station.timezone, unit=unit
    )
    sources_failed.extend(ens_members.sources_failed)

    member_values = (
        ens_members.member_max_f if parsed.metric == "max_temp" else ens_members.member_min_f
    )

    # Climatology — same calendar day across last 30 years, same unit.
    clim_history = await om_clim.climatology_max(
        station.lat, station.lon, parsed.resolution_date.month, parsed.resolution_date.day,
        unit=unit, timezone=station.timezone,
    )
    clim_dist: list[BinProbability] | None = (
        climatology_distribution(clim_history, parsed.bins) if clim_history else None
    )

    correction = bias_table.correction_f(
        station.code, "openmeteo_ensemble", parsed.resolution_date.month
    )

    is_binary = (
        len(market.outcomes) == 2
        and any(o.lower() == "yes" for o in market.outcomes)
        and len(parsed.bins) == 1
    )

    # Compute the bias-corrected mean once; reused for DB persistence (so
    # `calibrate` later can compute residual error) and for the ensemble path.
    corrected_members = apply_bias(member_values, correction) if member_values else []
    forecast_mean_f = (
        sum(corrected_members) / len(corrected_members) if corrected_members else None
    )

    if is_binary:
        # Binary Yes/No sub-market (grouped-event bin). Compute p_yes as the
        # raw proportion of ensemble members that land inside the bin range,
        # with Laplace smoothing. Then pair both Yes and No outcomes with
        # their respective order books so we can spot edge on either side.
        rounded = round_to_resolution(corrected_members)
        target = parsed.bins[0]
        n_in = sum(1 for v in rounded if target.contains(float(v)))
        n_total = max(len(rounded), 1)
        alpha = 0.5
        p_yes = (n_in + alpha) / (n_total + 2.0 * alpha)

        ens_result = EnsembleResult(
            distribution=[
                BinProbability(bin=target, probability=p_yes, members_in_bin=float(n_in)),
            ],
            member_count=n_total,
            bias_correction_f=correction,
            used_climatology=False,
            spread_f=(max(corrected_members) - min(corrected_members)) if corrected_members else 0.0,
        )
    else:
        ens_result = combine(
            member_values_f=member_values,
            bins=parsed.bins,
            bias_correction_f=correction,
            climatology_dist=clim_dist,
        )

    # Pull asks for each outcome in parallel.
    token_ids = [
        _pick_token_id(market, o) for o in market.outcomes
    ]
    book_tasks = [
        _fetch_book_for_outcome(clob, tid) if tid else asyncio.sleep(0, result=None)
        for tid in token_ids
    ]
    books = await asyncio.gather(*book_tasks)

    thresholds = EdgeThresholds(
        min_edge=settings.min_edge,
        min_ev=settings.min_ev,
        min_liquidity_usd=settings.min_liquidity_usd,
        max_hours_to_resolution=float(settings.max_hours_to_resolution),
        fee_rate=settings.fee_rate,
    )
    hours_left = _hours_to_resolution(parsed, station)

    outcomes: list[OutcomeAnalysis] = []
    if is_binary:
        p_yes = ens_result.distribution[0].probability
        # Canonical short label derived from the parsed bin, NOT the raw
        # question. Persisted to `recommendation.outcome_label` (String(64)),
        # so verbose strings like "Will the highest temperature in Shenzhen…?"
        # would truncate and break `_label_won` on re-parse.
        tb = parsed.bins[0]
        if tb.low is not None and tb.high is not None:
            canonical = (
                f"{int(tb.low)}°{tb.unit}"
                if tb.low == tb.high
                else f"{int(tb.low)}-{int(tb.high)}°{tb.unit}"
            )
        elif tb.low is not None:
            canonical = f"≥{int(tb.low)}°{tb.unit}"
        elif tb.high is not None:
            canonical = f"≤{int(tb.high)}°{tb.unit}"
        else:
            canonical = tb.label  # fallback; should be unreachable
        for label, book in zip(market.outcomes, books, strict=False):
            is_yes = label.lower() == "yes"
            p = p_yes if is_yes else (1.0 - p_yes)
            display_label = canonical if is_yes else f"NOT {canonical}"
            ask = book.best_ask().price if book and book.best_ask() else None
            ask_size = book.best_ask().size if book and book.best_ask() else None
            mid = book.mid() if book else None
            edge_inputs = EdgeInputs(
                p_model=p, ask=ask, ask_size=ask_size, hours_to_resolution=hours_left,
            )
            edge_res = evaluate(edge_inputs, thresholds)
            size = (
                kelly_size_usd(
                    p=p, ask=ask,
                    bankroll_usd=settings.bankroll_usd,
                    kelly_fraction=settings.kelly_fraction,
                    max_bet_fraction=settings.max_bet_fraction,
                )
                if edge_res.recommend and ask is not None
                else 0.0
            )
            outcomes.append(
                OutcomeAnalysis(
                    label=display_label, p_model=p, ask=ask, mid=mid,
                    edge=edge_res, kelly_size_usd=size,
                )
            )
    else:
        # ens_result.distribution aligns with parsed.bins, which in turn aligns with market.outcomes.
        for prob, label, book in zip(ens_result.distribution, market.outcomes, books, strict=False):
            ask = book.best_ask().price if book and book.best_ask() else None
            ask_size = book.best_ask().size if book and book.best_ask() else None
            mid = book.mid() if book else None
            edge_inputs = EdgeInputs(
                p_model=prob.probability, ask=ask, ask_size=ask_size,
                hours_to_resolution=hours_left,
            )
            edge_res = evaluate(edge_inputs, thresholds)
            size = (
                kelly_size_usd(
                    p=prob.probability, ask=ask,
                    bankroll_usd=settings.bankroll_usd,
                    kelly_fraction=settings.kelly_fraction,
                    max_bet_fraction=settings.max_bet_fraction,
                )
                if edge_res.recommend and ask is not None
                else 0.0
            )
            outcomes.append(
                OutcomeAnalysis(
                    label=label, p_model=prob.probability, ask=ask, mid=mid,
                    edge=edge_res, kelly_size_usd=size,
                )
            )

    analysis = MarketAnalysis(
        market=market,
        parsed=parsed,
        station=station,
        ensemble=ens_result,
        outcomes=outcomes,
        sources_failed=sources_failed,
    )

    if repo is not None:
        # Store resolution_date as UTC-naive at the local end-of-day. Polymarket
        # weather markets close at the end of the station's local day, so a
        # Tokyo "April 19" market actually closes at ~15:00 UTC on April 18.
        # Comparing against `datetime.utcnow()` in the resolver requires the
        # stored value to be on the same clock.
        tz = ZoneInfo(station.timezone)
        local_eod = datetime.combine(parsed.resolution_date, _time.max, tzinfo=tz)
        res_utc_naive = local_eod.astimezone(_timezone.utc).replace(tzinfo=None)

        market_id = repo.upsert_market(
            polymarket_id=market.id,
            slug=market.slug,
            question=market.question,
            metric=parsed.metric,
            station_code=station.code,
            resolution_date=res_utc_naive,
            unit=unit,
        )
        forecast_id = repo.record_forecast(
            market_id=market_id,
            member_count=ens_result.member_count,
            bias_correction_f=ens_result.bias_correction_f,
            used_climatology=ens_result.used_climatology,
            spread_f=ens_result.spread_f,
            sources_failed=sources_failed or None,
            forecast_mean_f=forecast_mean_f,
        )
        for o in outcomes:
            repo.record_recommendation(
                forecast_id=forecast_id,
                outcome_label=o.label,
                p_model=o.p_model,
                ask=o.ask,
                mid=o.mid,
                edge=o.edge.edge,
                ev_per_dollar=o.edge.ev_per_dollar,
                liquidity_usd=o.edge.liquidity_usd,
                kelly_size_usd=o.kelly_size_usd,
                recommend=o.edge.recommend,
                rejection_reason=o.edge.rejection_reason,
            )

    return analysis


async def scan_weather_markets(settings: Settings) -> list[GammaMarket]:
    gamma = GammaClient(user_agent=settings.user_agent)
    return await gamma.find_weather_markets()


def _parse_end_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Gamma returns ISO-8601 with trailing Z.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def filter_tradeable_soon(
    markets: list[GammaMarket],
    *,
    max_hours_to_resolution: int,
    min_volume_24hr: float = 0.0,
) -> list[GammaMarket]:
    """Keep markets that can plausibly trade now: short time-to-resolve + some
    volume (or zero-volume fresh markets on a short clock).

    Scanning 1000+ markets costs API quota; this filter is the single biggest
    lever for dashboard responsiveness.
    """
    now = datetime.now(ZoneInfo("UTC"))
    keep: list[GammaMarket] = []
    for m in markets:
        end = _parse_end_date(m.end_date_iso)
        if end is not None:
            hours_left = (end - now).total_seconds() / 3600.0
            # Generous multiplier because some markets set end_date hours AFTER
            # the resolution day's midnight; the per-market analysis applies
            # the strict threshold.
            if hours_left < 0 or hours_left > max_hours_to_resolution * 2:
                continue
        if (m.volume_24hr or 0.0) < min_volume_24hr:
            continue
        keep.append(m)
    return keep


async def analyze_many(
    markets: list[GammaMarket],
    *,
    settings: Settings,
    repo: Repo | None = None,
    concurrency: int = 6,
    on_progress: Any = None,
) -> list[MarketAnalysis]:
    """Analyze a batch under a concurrency cap; optionally report progress.

    `on_progress` is an optional callable `(done, total, last_analysis) -> None`
    invoked after each market completes — used by the dashboard to redraw.
    """
    sem = asyncio.Semaphore(concurrency)
    results: list[MarketAnalysis] = []
    total = len(markets)

    async def _one(m: GammaMarket) -> MarketAnalysis:
        async with sem:
            # Wrap in try/except so a single bad market (network blip,
            # unexpected API shape) can't cancel the whole gather batch.
            try:
                a = await analyze_market(market=m, settings=settings, repo=repo)
            except Exception as e:  # noqa: BLE001
                log.warning("analysis.failed", slug=m.slug, err=repr(e))
                a = MarketAnalysis(
                    market=m,
                    parsed=parse_market(m),
                    station=None,
                    ensemble=None,
                    error=f"{type(e).__name__}: {e}",
                )
            if on_progress is not None:
                try:
                    on_progress(len(results) + 1, total, a)
                except Exception:
                    pass
            results.append(a)
            return a

    await asyncio.gather(*(_one(m) for m in markets))
    return results


async def recommend(
    *, settings: Settings, repo: Repo | None = None
) -> list[MarketAnalysis]:
    markets = await scan_weather_markets(settings)
    markets = filter_tradeable_soon(
        markets, max_hours_to_resolution=settings.max_hours_to_resolution
    )
    analyses = await analyze_many(markets, settings=settings, repo=repo)
    return [a for a in analyses if a.has_recommendation()]


def market_summary(market: GammaMarket) -> dict[str, Any]:
    return {
        "id": market.id,
        "slug": market.slug,
        "question": market.question,
        "outcomes": market.outcomes,
        "end_date": market.end_date_iso,
    }
