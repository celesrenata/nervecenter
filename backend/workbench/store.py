"""API-side client for the per-kind Workbench file workers (Requirement 1.8).

Each store kind ("opencode", "menagerie") has its own worker. A failure talking
to one worker raises StoreUnavailable naming that kind and never affects the
other kind.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_TIMEOUT = 10.0
CLONE_TIMEOUT = 310.0

_ENV_VARS = {
    "opencode": "WORKBENCH_FS_OPENCODE_URL",
    "menagerie": "WORKBENCH_FS_MENAGERIE_URL",
}
_DEFAULT_URLS = {
    "opencode": "http://wb-fs-opencode.workbench:8090",
    "menagerie": "http://wb-fs-menagerie.workbench:8090",
}


class StoreUnavailable(RuntimeError):
    """The file worker for `kind` is unreachable, timed out, or reported 5xx."""

    def __init__(self, kind: str, reason: str = "") -> None:
        self.kind = kind
        self.reason = reason
        super().__init__(f"store {kind!r} unavailable" + (f": {reason}" if reason else ""))


class StoreRejected(RuntimeError):
    """The worker answered with a 4xx; carries its error code and message."""

    def __init__(
        self,
        kind: str,
        status: int,
        code: str,
        message: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.kind = kind
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(f"store {kind!r} rejected request ({status} {code}): {message}")


def worker_url(kind: str) -> str:
    if kind not in _ENV_VARS:
        raise ValueError(f"unknown kind: {kind!r}")
    return os.environ.get(_ENV_VARS[kind], _DEFAULT_URLS[kind]).rstrip("/")


class StoreClient:
    def __init__(
        self,
        kind: str,
        base_url: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        clone_timeout: float = CLONE_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if kind not in _ENV_VARS:
            raise ValueError(f"unknown kind: {kind!r}")
        self.kind = kind
        self.base_url = (base_url or worker_url(kind)).rstrip("/")
        self._timeout = timeout
        self._clone_timeout = clone_timeout
        self._transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        t = self._timeout if timeout is None else timeout
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=t, transport=self._transport
            ) as client:
                resp = await client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise StoreUnavailable(self.kind, type(exc).__name__) from exc
        if resp.status_code >= 500:
            raise StoreUnavailable(self.kind, f"http {resp.status_code}")
        if resp.status_code >= 400:
            code, message = "error", ""
            extra: dict[str, Any] = {}
            try:
                detail = resp.json().get("detail")
                if isinstance(detail, dict):
                    code = str(detail.get("error", code))
                    message = str(detail.get("message", ""))
                    extra = detail
            except (ValueError, AttributeError):
                pass
            raise StoreRejected(self.kind, resp.status_code, code, message, extra)
        try:
            body = resp.json()
        except ValueError as exc:
            raise StoreUnavailable(self.kind, "invalid response") from exc
        return body if isinstance(body, dict) else {"result": body}

    async def mkproject(self, slug: str) -> dict[str, Any]:
        return await self._request("POST", "/mkproject", json={"slug": slug})

    async def clone(self, slug: str, git_url: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/clone", json={"slug": slug, "git_url": git_url}, timeout=self._clone_timeout
        )

    async def rmproject(self, slug: str, confirm: str) -> dict[str, Any]:
        return await self._request("POST", "/rmproject", json={"slug": slug, "confirm": confirm})

    async def dirs(self) -> list[str]:
        return list((await self._request("GET", "/dirs")).get("dirs", []))

    async def stat(self, path: str = "", size: bool = False) -> dict[str, Any]:
        return await self._request("GET", "/stat", params={"path": path, "size": str(size).lower()})

    async def probe(self) -> dict[str, Any]:
        return await self._request("GET", "/probe")

    # Phase 2 file operations. Paths are store-root-relative (e.g. "slug/src/a.py").

    async def list_dir(self, path: str) -> dict[str, Any]:
        return await self._request("GET", "/list", params={"path": path})

    async def read(self, path: str) -> dict[str, Any]:
        return await self._request("GET", "/read", params={"path": path})

    async def write(self, path: str, content: str, base_mtime_ns: int | None) -> dict[str, Any]:
        return await self._request(
            "PUT", "/write",
            json={"path": path, "content": content, "base_mtime_ns": base_mtime_ns},
        )

    async def op(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/op", json=body)

    async def changes(self, path: str, since_ns: int = 0) -> dict[str, Any]:
        return await self._request("GET", "/changes", params={"path": path, "since_ns": since_ns})


def get_store_clients() -> dict[str, StoreClient]:
    """One independent client per kind."""
    return {kind: StoreClient(kind) for kind in _ENV_VARS}


__all__ = [
    "CLONE_TIMEOUT", "DEFAULT_TIMEOUT", "StoreClient", "StoreRejected",
    "StoreUnavailable", "get_store_clients", "worker_url",
]
