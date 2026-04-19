"""Polymarket CLOB API client — read-only price/book queries.

Wallet/private-key not required for reads. We hit the public REST endpoints
(`/price`, `/book`, `/midpoint`) directly via httpx and reuse the rate limiter.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog
from pydantic import BaseModel

from .rate_limiter import EndpointCategory, get_rate_limiter

log = structlog.get_logger(__name__)

CLOB_BASE = "https://clob.polymarket.com"


class BookLevel(BaseModel):
    price: float
    size: float


class OrderBook(BaseModel):
    token_id: str
    bids: list[BookLevel] = []
    asks: list[BookLevel] = []

    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    def mid(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None
        return (b.price + a.price) / 2.0


class ClobClient:
    def __init__(self, *, user_agent: str, timeout: float = 30.0) -> None:
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._timeout = timeout
        self._limiter = get_rate_limiter()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._limiter.acquire(EndpointCategory.MARKET_DATA)
        url = f"{CLOB_BASE}{path}"
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                await self._limiter.handle_429(
                    EndpointCategory.MARKET_DATA,
                    float(retry_after) if retry_after else None,
                )
                resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    async def midpoint(self, token_id: str) -> float | None:
        # Catch the full httpx.HTTPError tree *and* asyncio cancellation-adjacent
        # errors (Timeout). A single bad book MUST NOT cancel the gather() batch
        # that calls us — the caller analyzes hundreds of markets in parallel.
        try:
            data = await self._get("/midpoint", {"token_id": token_id})
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            log.warning("clob.midpoint_failed", token=token_id, err=type(e).__name__)
            return None
        val = data.get("mid") if isinstance(data, dict) else None
        return float(val) if val is not None else None

    async def price(self, token_id: str, side: str = "BUY") -> float | None:
        """Returns the best price for a side. side ∈ {"BUY","SELL"}."""
        try:
            data = await self._get("/price", {"token_id": token_id, "side": side.upper()})
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            log.warning("clob.price_failed", token=token_id, side=side, err=type(e).__name__)
            return None
        val = data.get("price") if isinstance(data, dict) else None
        return float(val) if val is not None else None

    async def book(self, token_id: str) -> OrderBook | None:
        try:
            data = await self._get("/book", {"token_id": token_id})
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            log.warning("clob.book_failed", token=token_id, err=type(e).__name__)
            return None
        if not isinstance(data, dict):
            return None
        bids_raw = data.get("bids", []) or []
        asks_raw = data.get("asks", []) or []

        def _levels(rows: list[Any], reverse: bool) -> list[BookLevel]:
            parsed = [
                BookLevel(price=float(r["price"]), size=float(r["size"]))
                for r in rows
                if isinstance(r, dict) and "price" in r and "size" in r
            ]
            parsed.sort(key=lambda x: x.price, reverse=reverse)
            return parsed

        return OrderBook(
            token_id=token_id,
            bids=_levels(bids_raw, reverse=True),   # bids: highest first
            asks=_levels(asks_raw, reverse=False),  # asks: lowest first
        )
