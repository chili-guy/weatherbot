"""Polymarket Gamma API client — market discovery for weather markets."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import structlog
from pydantic import BaseModel

from .rate_limiter import EndpointCategory, get_rate_limiter

log = structlog.get_logger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Word-boundary patterns that flag a market as weather-related. Using regex with
# \b prevents false positives like "Ukraine" matching "rain" or "Carolina
# Hurricanes" (NHL team) matching "hurricane".
WEATHER_KEYWORD_PATTERNS: tuple[str, ...] = (
    r"\btemperature\b|\btemperatura\b",
    r"\b(highest|lowest|max|min|coldest|hottest)\s+temperature\b",
    r"\b(mais alta|mais baixa|máxima|mínima)\s+temperatura\b",
    r"\b(hurricane|tropical storm|named storm|furacão|tempestade tropical)\b",
    r"\b(snow|snowfall|snowstorm|blizzard|neve)\b",
    r"\b(rain|rainfall|chuva|precipitação)\b",
    r"\bweather\b|\bclima\b|\btempo\b",
    r"\b\d+°\s*[FC]\b",
)

# Negative patterns: if any of these match, drop the market even if a positive
# keyword fires. Sports franchises and country/place names with weather-like
# words live here.
WEATHER_NEGATIVE_PATTERNS: tuple[str, ...] = (
    r"\b(carolina hurricanes|miami hurricanes|nhl|nfl|nba|mlb|soccer|football)\b",
    r"\b(ukraine|ukrainian)\b",   # "rain" substring trap
    r"\b(election|ceasefire|president|war|crypto|stock|token|launch)\b",
)


class GammaMarket(BaseModel):
    """Subset of Gamma's market shape used by the bot.

    Gamma returns many fields; we keep the ones relevant for weather analysis
    and stash the raw payload for parsers that need richer text.
    """

    id: str
    slug: str
    question: str
    description: str | None = None
    end_date_iso: str | None = None
    closed: bool = False
    active: bool = True
    resolution_source: str | None = None
    outcomes: list[str] = []
    outcome_prices: list[str] = []
    clob_token_ids: list[str] = []
    # Event context (populated when the market is a sub-market of a grouped event)
    event_id: str | None = None
    event_slug: str | None = None
    event_title: str | None = None
    group_item_title: str | None = None  # e.g. "58-59°F" — cleaner than outcome label "Yes"
    volume_24hr: float | None = None
    liquidity: float | None = None
    raw: dict[str, Any] = {}


def _coerce_list(val: Any) -> list[str]:
    """Gamma returns some array fields as JSON strings — normalize to list."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val]
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _to_market(raw: dict[str, Any], event: dict[str, Any] | None = None) -> GammaMarket:
    def _f(x: Any) -> float | None:
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None

    return GammaMarket(
        id=str(raw.get("id", "")),
        slug=raw.get("slug", ""),
        question=raw.get("question", ""),
        description=raw.get("description"),
        end_date_iso=raw.get("endDate"),
        closed=bool(raw.get("closed", False)),
        active=bool(raw.get("active", True)),
        resolution_source=raw.get("resolutionSource"),
        outcomes=_coerce_list(raw.get("outcomes")),
        outcome_prices=_coerce_list(raw.get("outcomePrices")),
        clob_token_ids=_coerce_list(raw.get("clobTokenIds")),
        event_id=str(event["id"]) if event and event.get("id") is not None else None,
        event_slug=event.get("slug") if event else None,
        event_title=event.get("title") if event else None,
        group_item_title=raw.get("groupItemTitle"),
        volume_24hr=_f(raw.get("volume24hr")),
        liquidity=_f(raw.get("liquidityNum") or raw.get("liquidity")),
        raw=raw,
    )


class GammaClient:
    def __init__(self, *, user_agent: str, timeout: float = 30.0) -> None:
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._timeout = timeout
        self._limiter = get_rate_limiter()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._limiter.acquire(EndpointCategory.GAMMA_API)
        url = f"{GAMMA_BASE}{path}"
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                await self._limiter.handle_429(
                    EndpointCategory.GAMMA_API,
                    float(retry_after) if retry_after else None,
                )
                resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    async def list_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
        extra_params: dict[str, Any] | None = None,
    ) -> list[GammaMarket]:
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
        }
        if extra_params:
            params.update(extra_params)
        data = await self._get("/markets", params)
        items = data if isinstance(data, list) else data.get("data", [])
        return [_to_market(m) for m in items]

    async def get_market(self, market_id_or_slug: str) -> GammaMarket | None:
        try:
            if market_id_or_slug.isdigit():
                data = await self._get(f"/markets/{market_id_or_slug}")
            else:
                items = await self._get("/markets", {"slug": market_id_or_slug})
                data = (items[0] if isinstance(items, list) and items else None) or (
                    items.get("data", [None])[0] if isinstance(items, dict) else None
                )
        except httpx.HTTPStatusError as e:
            log.warning("gamma.get_market_failed", target=market_id_or_slug, status=e.response.status_code)
            return None
        if not data:
            return None
        return _to_market(data)

    async def list_events(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Raw event list — each event carries a `markets: [...]` array.

        Temperature markets on Polymarket live as grouped events (e.g.
        "Highest temperature in NYC on April 18?") with ~11 binary Yes/No
        sub-markets covering each bin. The flat `/markets` endpoint does
        NOT return those sub-markets, so we have to walk `/events`.
        """
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
        }
        data = await self._get("/events", params)
        return data if isinstance(data, list) else data.get("data", [])

    async def find_weather_events(
        self,
        *,
        positive_patterns: tuple[str, ...] = WEATHER_KEYWORD_PATTERNS,
        negative_patterns: tuple[str, ...] = WEATHER_NEGATIVE_PATTERNS,
        limit_per_page: int = 500,
        max_pages: int = 40,
    ) -> list[GammaMarket]:
        """Discover weather sub-markets via the `/events` endpoint.

        Returns flattened `GammaMarket` objects — one per active sub-market
        inside a matching event — tagged with their parent event metadata.
        """
        pos = [re.compile(p, re.IGNORECASE) for p in positive_patterns]
        neg = [re.compile(p, re.IGNORECASE) for p in negative_patterns]

        seen: set[str] = set()
        results: list[GammaMarket] = []
        for page in range(max_pages):
            batch = await self.list_events(
                active=True, closed=False,
                limit=limit_per_page, offset=page * limit_per_page,
            )
            if not batch:
                break
            for ev in batch:
                title = ev.get("title") or ""
                slug = ev.get("slug") or ""
                hay = f"{slug} {title}"
                if not any(p.search(hay) for p in pos):
                    continue
                if any(n.search(hay) for n in neg):
                    continue
                for sub in ev.get("markets", []) or []:
                    if sub.get("closed") or not sub.get("active", True):
                        continue
                    sid = str(sub.get("id", ""))
                    if not sid or sid in seen:
                        continue
                    seen.add(sid)
                    results.append(_to_market(sub, event=ev))
            if len(batch) < limit_per_page:
                break
        log.info("gamma.weather_events_found", markets=len(results))
        return results

    async def find_weather_markets(
        self,
        *,
        positive_patterns: tuple[str, ...] = WEATHER_KEYWORD_PATTERNS,
        negative_patterns: tuple[str, ...] = WEATHER_NEGATIVE_PATTERNS,
        limit_per_page: int = 500,
        max_pages: int = 100,
    ) -> list[GammaMarket]:
        """Discover weather markets via BOTH `/markets` and `/events`.

        `/markets` surfaces stand-alone markets (e.g. hurricane-season binaries).
        `/events` surfaces the grouped daily-temperature sub-markets that
        `/markets` hides. We merge both and dedup by id.
        """
        pos = [re.compile(p, re.IGNORECASE) for p in positive_patterns]
        neg = [re.compile(p, re.IGNORECASE) for p in negative_patterns]

        seen: set[str] = set()
        results: list[GammaMarket] = []

        # Pass 1: flat /markets (stand-alone markets like hurricane binaries)
        for page in range(max_pages):
            batch = await self.list_markets(
                active=True, closed=False,
                limit=limit_per_page, offset=page * limit_per_page,
            )
            if not batch:
                break
            for m in batch:
                if m.id in seen:
                    continue
                hay = f"{m.slug} {m.question}"
                if not any(p.search(hay) for p in pos):
                    continue
                if any(n.search(hay) for n in neg):
                    continue
                seen.add(m.id)
                results.append(m)
            if len(batch) < limit_per_page:
                break

        # Pass 2: /events — grouped events (daily temperature markets, etc.)
        event_markets = await self.find_weather_events(
            positive_patterns=positive_patterns,
            negative_patterns=negative_patterns,
            limit_per_page=limit_per_page,
        )
        for m in event_markets:
            if m.id in seen:
                continue
            seen.add(m.id)
            results.append(m)

        log.info("gamma.weather_markets_found", count=len(results))
        return results
