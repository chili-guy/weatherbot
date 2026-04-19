"""NHC (National Hurricane Center) — current storms feed.

For "will storm X make landfall in state Y by date Z" markets the spec says:
  > Use the NHC's official probabilistic products. Don't try to recompute
  > tracks yourself.

This client just exposes the active storms list. Probabilistic landfall
products (`wsp_120hr`) are GIS rasters; integrating them is a TODO once a real
hurricane market shows up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

CURRENT_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"


@dataclass
class ActiveStorm:
    id: str
    name: str
    classification: str
    intensity: str | None
    raw: dict[str, Any]


class NhcClient:
    def __init__(self, *, user_agent: str, timeout: float = 30.0) -> None:
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._timeout = timeout

    async def current_storms(self) -> list[ActiveStorm]:
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            try:
                resp = await client.get(CURRENT_STORMS_URL)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("nhc.http_failed", error=str(e))
                return []
        data = resp.json()
        storms_raw = data.get("activeStorms") or data.get("storms") or []
        out: list[ActiveStorm] = []
        for s in storms_raw:
            out.append(
                ActiveStorm(
                    id=str(s.get("id") or s.get("binNumber") or ""),
                    name=str(s.get("name", "")),
                    classification=str(s.get("classification", "")),
                    intensity=s.get("intensity"),
                    raw=s,
                )
            )
        return out
