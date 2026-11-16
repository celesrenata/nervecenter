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
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from backend.inference.omniroute_registry import (  # noqa: E402 - moved, re-exported
    MAX_RESPONSE_BYTES,
    HttpRegistryTransport,
    _bounded_json,
)


class HttpObserverTransport:
    """Sends the observer's fixed request through OmniRoute; returns raw text."""

    def __init__(
        self, base_url: str, api_key: str | Callable[[], str | None], *, timeout: float = 60.0
    ) -> None:
        base = base_url.rstrip("/")
        self._url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        self._key = api_key  # a live callable follows key changes (NS-ADR-117)
        self._timeout = timeout

    async def complete(self, request: Mapping[str, Any], route: Any) -> str:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as c:
                async with c.stream(
                    "POST",
                    self._url,
                    json=dict(request),
                    headers={
                        "Authorization": "Bearer "
                        + ((self._key() if callable(self._key) else self._key) or ""),
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
