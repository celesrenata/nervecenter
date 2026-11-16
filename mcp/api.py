"""Async HTTP proxy from MCP tools to the §46 NerveCenter HTTP API.

Every request carries a fresh correlation id (``Nerve-Trace-Id``) per spec
§29 so the API can tie the call back to the originating goal/task/agent
run. An optional ``httpx`` transport can be injected for tests.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
TRACE_HEADER = "Nerve-Trace-Id"
#: Internal service credential the api accepts for this pod's tool paths only
#: (NS-ADR-122). Never logged.
API_TOKEN_ENV = "NERVE_MCP_API_TOKEN"


class ApiError(Exception):
    """Base error for NerveCenter API calls."""


class ContractError(ApiError):
    """The API answered but not with a usable §46 contract payload."""

    def __init__(self, message: str, *, status: int | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class UnreachableError(ApiError):
    """Network-level failure (refused, DNS, TLS, timeout)."""


def _safe_json(text: str) -> Any:
    """Parse a response body; return raw text when it is not JSON."""
    try:
        return json.loads(text)
    except ValueError:
        return text


class NerveAPI:
    """Small async proxy for the §46 NerveCenter HTTP API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        trace_header: str = TRACE_HEADER,
        api_token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.trace_header = trace_header
        self._api_token = api_token if api_token is not None else os.environ.get(API_TOKEN_ENV, "")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> NerveAPI:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _headers(self) -> dict[str, str]:
        headers = {self.trace_header: uuid.uuid4().hex}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Perform the HTTP request and decode the JSON body (or ``None``)."""
        try:
            response = await self._client.request(
                method, path, params=params, json=json_body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise UnreachableError(
                f"cannot reach NerveCenter API at {self.base_url} ({exc.__class__.__name__})"
            ) from exc
        if response.status_code >= 400:
            detail = _safe_json(response.text)
            raise ContractError(
                f"{method} {path} returned HTTP {response.status_code}",
                status=response.status_code,
                detail=detail,
            )
        if response.status_code == 204 or not response.text:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ContractError(f"{method} {path} returned a non-JSON body") from exc

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Perform a GET request."""
        return await self.request("GET", path, params=params)

    async def post(self, path: str, *, json_body: dict[str, Any] | None = None) -> Any:
        """Perform a POST request."""
        return await self.request("POST", path, json_body=json_body)

    async def patch(self, path: str, *, json_body: dict[str, Any] | None = None) -> Any:
        """Perform a PATCH request."""
        return await self.request("PATCH", path, json_body=json_body)
