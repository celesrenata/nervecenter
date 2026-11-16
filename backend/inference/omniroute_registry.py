"""Read-only OmniRoute registry transport for the route guard (NS-ADR-073, NS-ADR-108).

Moved here from ``backend.connectors.omniroute_observer`` (which re-exports it)
so the shared secure client can use it without importing the connectors
package. GET only, exactly ``/v1/models``, ``/api/combos`` and
``/api/providers``, no redirects, bounded response, credentials never in errors.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

#: Bounded, but large enough for the live registry (``/v1/models`` was 369 KB
#: with 988 models on 2026-11-13).
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_REGISTRY_PATHS = frozenset({"/v1/models", "/api/combos", "/api/providers"})


async def _bounded_json(resp: httpx.Response) -> Any:
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        chunks.append(chunk)
    return json.loads(b"".join(chunks))


class HttpRegistryTransport:
    """Read-only OmniRoute registry access for the route guard."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 12.0) -> None:
        # Live /api/combos occasionally takes >3 s; the resolver's overall
        # budget (15 s) still bounds the three GETs together.
        self._base = base_url.rstrip("/").removesuffix("/v1")
        self._key = api_key
        self._timeout = timeout

    async def get_json(self, path: str) -> Any:
        # Exactly the three read-only paths the route guard uses.
        if path not in _REGISTRY_PATHS:
            raise ValueError("registry path not allowed")
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as c:
                async with c.stream(
                    "GET", self._base + path, headers={"Authorization": f"Bearer {self._key}"}
                ) as resp:
                    if resp.status_code != 200:
                        raise ValueError(f"registry status {resp.status_code}")
                    return await _bounded_json(resp)
        except (httpx.HTTPError, json.JSONDecodeError):
            raise ValueError("registry unavailable") from None
