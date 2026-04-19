"""Token-bucket rate limiter for Polymarket APIs.

Adapted from polymarket-mcp (utils/rate_limiter.py). Limits per category come
from Polymarket's published guidance.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

import structlog

log = structlog.get_logger(__name__)


class EndpointCategory(Enum):
    GAMMA_API = "gamma_api"
    CLOB_GENERAL = "clob_general"
    MARKET_DATA = "market_data"  # /book, /price


@dataclass(frozen=True)
class RateLimitConfig:
    max_tokens: int
    refill_rate: float


RATE_LIMITS: dict[EndpointCategory, RateLimitConfig] = {
    EndpointCategory.GAMMA_API: RateLimitConfig(max_tokens=750, refill_rate=75.0),
    EndpointCategory.CLOB_GENERAL: RateLimitConfig(max_tokens=5000, refill_rate=500.0),
    EndpointCategory.MARKET_DATA: RateLimitConfig(max_tokens=200, refill_rate=20.0),
}


class _TokenBucket:
    def __init__(self, cfg: RateLimitConfig) -> None:
        self.max_tokens = cfg.max_tokens
        self.refill_rate = cfg.refill_rate
        self.tokens = float(cfg.max_tokens)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    async def acquire(self, n: int = 1) -> float:
        async with self._lock:
            waited = 0.0
            while True:
                self._refill()
                if self.tokens >= n:
                    self.tokens -= n
                    return waited
                deficit = n - self.tokens
                sleep_for = max(deficit / self.refill_rate, 0.01)
                await asyncio.sleep(sleep_for)
                waited += sleep_for


class RateLimiter:
    def __init__(self) -> None:
        self.buckets = {cat: _TokenBucket(cfg) for cat, cfg in RATE_LIMITS.items()}
        self._backoff_until: dict[EndpointCategory, float] = defaultdict(float)
        self._backoff_lock = asyncio.Lock()

    async def acquire(self, category: EndpointCategory, n: int = 1) -> float:
        bucket = self.buckets.get(category)
        if bucket is None:
            return 0.0

        waited = 0.0
        async with self._backoff_lock:
            until = self._backoff_until[category]
            now = time.monotonic()
            if until > now:
                pause = until - now
                log.warning("rate_limit.backoff_active", category=category.value, wait_s=pause)
                await asyncio.sleep(pause)
                waited += pause

        waited += await bucket.acquire(n)
        return waited

    async def handle_429(
        self, category: EndpointCategory, retry_after: float | None = None
    ) -> None:
        async with self._backoff_lock:
            now = time.monotonic()
            current = self._backoff_until[category]
            if retry_after is not None:
                wait = float(retry_after)
            elif current > now:
                wait = min((current - now) * 2, 60.0)
            else:
                wait = 1.0
            self._backoff_until[category] = now + wait
            log.warning("rate_limit.429", category=category.value, backoff_s=wait)


_singleton: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _singleton
    if _singleton is None:
        _singleton = RateLimiter()
    return _singleton
