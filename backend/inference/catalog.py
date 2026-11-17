"""Searchable OmniRoute model/combo catalog for the pickers.

OmniRoute's ``/v1/models`` lists every model and combo the regular key can use
(about 1000). The web pickers search it server-side: case-insensitive, every
word must match, combos (``owned_by == "combo"``) can be filtered on their own.
The list is cached for a few minutes; the key is never returned or logged.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import structlog

from backend.inference import key_resolver

log = structlog.get_logger(__name__)

CACHE_S = 300.0
MAX_LIMIT = 100
KINDS = ("all", "model", "combo")


class Catalog:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Any = time.monotonic) -> None:
        self._transport = transport
        self._clock = clock
        self._items: list[dict[str, str]] | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def _url() -> str:
        base = (os.environ.get("NERVE_OMNIROUTE_URL") or "").rstrip("/")
        if not base:
            return ""
        return base + ("/models" if base.endswith("/v1") else "/v1/models")

    async def items(self) -> list[dict[str, str]]:
        async with self._lock:
            fresh = self._items is not None and self._clock() - self._at < CACHE_S
            if fresh:
                return self._items or []
            url, key = self._url(), key_resolver.regular_key()
            if not url or not key:
                return self._items or []
            try:
                async with httpx.AsyncClient(transport=self._transport, timeout=20) as c:
                    resp = await c.get(url, headers={"Authorization": f"Bearer {key}"})
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001 - keep the last good list
                log.warning("inference.catalog_fetch_failed", error=type(exc).__name__)
                return self._items or []
            raw = data.get("data", []) if isinstance(data, dict) else data
            items = []
            for m in raw if isinstance(raw, list) else []:
                if isinstance(m, dict) and isinstance(m.get("id"), str):
                    owner = str(m.get("owned_by") or "")
                    items.append({"id": m["id"], "owned_by": owner,
                                  "kind": "combo" if owner == "combo" else "model"})
            items.sort(key=lambda i: i["id"].lower())
            self._items, self._at = items, self._clock()
            return items

    async def search(self, q: str = "", *, kind: str = "all", limit: int = 50,
                     offset: int = 0) -> dict[str, Any]:
        kind = kind if kind in KINDS else "all"
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
        words = [w for w in (q or "").lower().split() if w]
        matches = [
            i for i in await self.items()
            if (kind == "all" or i["kind"] == kind)
            and all(w in f"{i['id']} {i['owned_by']}".lower() for w in words)
        ]
        return {"total": len(matches), "offset": offset, "limit": limit,
                "items": matches[offset:offset + limit]}


_CATALOG: Catalog | None = None


def get_catalog() -> Catalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = Catalog()
    return _CATALOG


__all__ = ["Catalog", "get_catalog"]
