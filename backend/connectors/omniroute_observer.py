"""HTTP transports for the SHADOW semantic observer (NS-ADR-073).

These live outside ``backend/mail_security`` on purpose: that package stays
network-free (only the clamd client is exempt, enforced by a test).

* :class:`HttpRegistryTransport` — GET-only access to OmniRoute's registry for
  NerveShield's local-only route resolver; exactly
  ``/v1/models``, ``/api/combos`` and ``/api/providers``, no redirects,
  bounded response, credentials never in errors.
* :class:`HttpObserverTransport` — POSTs the observer's fixed, tool-free
  request to OmniRoute ``/v1/chat/completions`` (no redirects, bounded
  response) and returns only the assistant text. A tool-call reply is returned
  as its JSON so the observer's validator rejects it.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
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


class HttpObserverTransport:
    """Sends the observer's fixed request through OmniRoute; returns raw text."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0) -> None:
        base = base_url.rstrip("/")
        self._url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        self._key = api_key
        self._timeout = timeout

    async def complete(self, request: Mapping[str, Any], route: Any) -> str:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as c:
                async with c.stream(
                    "POST",
                    self._url,
                    json=dict(request),
                    headers={
                        "Authorization": f"Bearer {self._key}",
                        # Stricter-only opt-outs; the secure combo enforces
                        # this server-side regardless (NS-ADR-034).
                        "x-omniroute-no-memory": "true",
                        "x-omniroute-compression": "off",
                    },
                ) as resp:
                    if resp.status_code != 200:
                        raise ValueError(f"observer status {resp.status_code}")
                    body = await _bounded_json(resp)
        except (httpx.HTTPError, json.JSONDecodeError):
            raise ValueError("observer transport failed") from None
        choices = body.get("choices") if isinstance(body, dict) else None
        message = ((choices or [{}])[0] or {}).get("message") or {}
        if message.get("tool_calls"):
            return json.dumps({"tool_calls": message.get("tool_calls")})
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("observer reply has no text")
        return content


__all__ = ["HttpObserverTransport", "HttpRegistryTransport", "MAX_RESPONSE_BYTES"]
