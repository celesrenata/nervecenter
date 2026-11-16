"""Tiny HTTP client for the NerveCenter §46 API (stdlib only).

Design constraints:
- Zero dependencies beyond the standard library (``tabulate`` is optional).
- Spec §29 correlation: every request carries a ``Nerve-Trace-Id`` header
  (UUID); callers may supply their own trace id for end-to-end correlation.
- Graceful failures: unreachable servers raise :class:`UnreachableError`
  (CLI maps it to exit code 127); HTTP >= 400 raises :class:`ApiError`
  with the response body attached (CLI maps it to a contract/usage error).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE_URL = "http://localhost:8000"
ENV_API_URL = "NERVE_API"
#: Personal API token (Settings -> Access) sent as a bearer on every request.
ENV_TOKEN = "NERVE_TOKEN"
TRACE_HEADER = "Nerve-Trace-Id"
DEFAULT_TIMEOUT_S = 15.0


class UnreachableError(Exception):
    """The API could not be reached (connection refused, DNS, timeout)."""

    def __init__(self, base_url: str, detail: str) -> None:
        super().__init__(f"cannot reach NerveCenter API at {base_url} ({detail})")
        self.base_url = base_url
        self.detail = detail


class ApiError(Exception):
    """The API answered with an HTTP status >= 400 (contract error)."""

    def __init__(self, method: str, path: str, status: int, detail: Any) -> None:
        summary = detail
        if isinstance(detail, dict):
            message = detail.get("detail") or detail.get("message")
            if message is not None:
                summary = message
        super().__init__(f"{method} {path} returned HTTP {status}: {summary}")
        self.method = method
        self.path = path
        self.status = status
        self.detail = detail


def resolve_base_url(flag_value: str | None) -> str:
    """Resolve the API base URL: ``--api`` flag, else ``NERVE_API``, else default."""
    import os

    base = flag_value or os.environ.get(ENV_API_URL) or DEFAULT_BASE_URL
    return base.rstrip("/")


@dataclass(frozen=True)
class Response:
    """Parsed HTTP response."""

    status_code: int
    data: Any


def _build_url(base_url: str, path: str, params: dict[str, Any] | None) -> str:
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if query:
            path = f"{path}?{query}"
    return f"{base_url}{path}"


def _do_request(
    method: str,
    base_url: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> Response:
    request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as http_response:
            raw = http_response.read()
            status = http_response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise UnreachableError(base_url, str(reason)) from exc

    try:
        data: Any = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        data = raw.decode("utf-8", "replace")
    return Response(status_code=status, data=data)


def request(
    method: str,
    base_url: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    trace_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Response:
    """Perform one HTTP request against the §46 API and parse the JSON body."""
    url = _build_url(base_url, path, params)
    headers = {
        TRACE_HEADER: trace_id or uuid.uuid4().hex,
        "Accept": "application/json",
    }
    token = os.environ.get(ENV_TOKEN, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body: bytes | None = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    response = _do_request(method, base_url, url, headers, body, timeout)
    if response.status_code >= 400:
        raise ApiError(method.upper(), path, response.status_code, response.data)
    return response
