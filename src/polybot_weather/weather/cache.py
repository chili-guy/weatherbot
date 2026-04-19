"""Tiny on-disk JSON cache shared by weather clients.

Per spec: 30-min TTL for forecasts, 24-h for climatology. Implementation is
intentionally minimal — one file per cache key, hashed name. We don't add
`requests-cache` because httpx is the chosen HTTP client and the volumes are
small.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def _key_to_filename(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24] + ".json"


class JsonCache:
    def __init__(self, root: Path, ttl_seconds: int) -> None:
        self.root = root
        self.ttl = ttl_seconds
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / _key_to_filename(key)

    def get(self, key: str) -> Any | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            payload = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - payload.get("_ts", 0) > self.ttl:
            return None
        return payload.get("data")

    def set(self, key: str, data: Any) -> None:
        p = self._path(key)
        try:
            p.write_text(json.dumps({"_ts": time.time(), "data": data}))
        except OSError as e:
            log.warning("cache.write_failed", key=key, error=str(e))
