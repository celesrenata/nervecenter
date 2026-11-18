"""Workbench store health (Requirements 9.3, 9.4).

Probes each store through its StoreClient. A probe that times out, fails, or
returns an unusable body reports the store as unmounted, and the other store
is still reported.
"""

from __future__ import annotations

import asyncio
from typing import Any

from backend.workbench.store import StoreClient, get_store_clients

PROBE_TIMEOUT_S = 5.0


def _unavailable() -> dict[str, Any]:
    return {"mounted": False, "writable": False, "free_bytes": None}


async def probe_store(client: StoreClient) -> dict[str, Any]:
    try:
        raw = await asyncio.wait_for(client.probe(), PROBE_TIMEOUT_S)
    except Exception:  # timeout, StoreUnavailable, StoreRejected, anything else
        return _unavailable()
    if not isinstance(raw, dict) or raw.get("timeout"):
        return _unavailable()
    free = raw.get("free_bytes")
    if isinstance(free, bool) or not isinstance(free, int):
        free = None
    return {
        "mounted": raw.get("mounted") is True,
        "writable": raw.get("writable") is True,
        "free_bytes": free,
    }


async def store_health(
    clients: dict[str, StoreClient] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return {kind: {mounted, writable, free_bytes}} for every store, probed concurrently."""
    clients = clients if clients is not None else get_store_clients()
    kinds = list(clients)
    results = await asyncio.gather(*(probe_store(clients[k]) for k in kinds))
    return dict(zip(kinds, results))


__all__ = ["PROBE_TIMEOUT_S", "probe_store", "store_health"]
