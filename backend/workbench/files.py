"""API-side file facade (Req 4.3-4.6, 4.12, 9.12).

Forwards list/read/write/op/changes to the opencode file worker and maps worker
errors to HTTP statuses: 400 path_not_permitted, 409 conflict (with
``current_mtime_ns``), 415 not_editable (with size/type), 429 concurrency_limit.
Only opencode Projects have an editor; a menagerie-only slug gets 409
``editor_not_supported``. The worker takes store-root-relative paths, so every
client path is joined under the slug here after rejecting anything that could
leave the project directory.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.persistence.models.workbench import WorkbenchProject

from .store import StoreClient, StoreRejected, StoreUnavailable

KIND = "opencode"
# Worker status/code pairs passed through; extra detail keys are carried along.
_PASSTHROUGH_STATUSES = {400, 404, 409, 415, 422, 429}
_DETAIL_KEYS = ("current_mtime_ns", "size", "type")


class FileError(Exception):
    """Carries the HTTP status, error code and extra body fields for the route."""

    def __init__(
        self, status: int, code: str, message: str = "", extra: dict[str, Any] | None = None
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}
        super().__init__(f"{status} {code}: {message}")

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


def join_path(slug: str, rel: str) -> str:
    """``slug/rel`` with traversal, absolute, backslash and NUL paths rejected."""
    if "\x00" in rel or "\\" in rel or rel.startswith("/"):
        raise FileError(400, "path_not_permitted", "path not permitted")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise FileError(400, "path_not_permitted", "path not permitted")
    return "/".join([slug, *parts])


def _map_rejected(exc: StoreRejected) -> FileError:
    if exc.status not in _PASSTHROUGH_STATUSES:
        return FileError(502, "store_error", exc.message)
    extra = {k: exc.detail[k] for k in _DETAIL_KEYS if k in exc.detail}
    return FileError(exc.status, exc.code, exc.message, extra)


class FileService:
    def __init__(self, session_factory: async_sessionmaker[Any], store: StoreClient) -> None:
        if store.kind != KIND:
            raise ValueError("FileService requires the opencode store client")
        self._sf = session_factory
        self._store = store

    async def _require_project(self, slug: str) -> WorkbenchProject:
        async with self._sf() as s:
            rows = (
                await s.execute(select(WorkbenchProject).where(WorkbenchProject.slug == slug))
            ).scalars().all()
        for p in rows:
            if p.kind == KIND:
                return p
        if rows:
            raise FileError(409, "editor_not_supported", "editor supports opencode projects")
        raise FileError(404, "project_not_found", slug)

    async def _call(self, coro: Any) -> dict[str, Any]:
        try:
            return await coro
        except StoreRejected as exc:
            raise _map_rejected(exc) from exc
        except StoreUnavailable as exc:
            raise FileError(503, "store_unavailable", str(exc)) from exc

    async def list(self, slug: str, path: str = "") -> dict[str, Any]:
        await self._require_project(slug)
        return await self._call(self._store.list_dir(join_path(slug, path)))

    async def read(self, slug: str, path: str) -> dict[str, Any]:
        await self._require_project(slug)
        out = await self._call(self._store.read(join_path(slug, path)))
        return self._strip_slug(slug, out)

    async def write(
        self, slug: str, path: str, content: str, base_mtime_ns: int | None
    ) -> dict[str, Any]:
        await self._require_project(slug)
        out = await self._call(
            self._store.write(join_path(slug, path), content, base_mtime_ns)
        )
        return self._strip_slug(slug, out)

    async def op(
        self,
        slug: str,
        op: str,
        path: str,
        *,
        is_dir: bool = False,
        new_name: str | None = None,
        dest: str | None = None,
    ) -> dict[str, Any]:
        await self._require_project(slug)
        body: dict[str, Any] = {"op": op, "path": join_path(slug, path)}
        if op == "create":
            body["is_dir"] = is_dir
        if op == "rename":
            body["new_name"] = new_name
        if op == "move":
            if dest is None:
                raise FileError(422, "dest_required")
            body["dest"] = join_path(slug, dest)
        out = await self._call(self._store.op(body))
        return self._strip_slug(slug, out)

    async def changes(self, slug: str, since_ns: int = 0) -> dict[str, Any]:
        await self._require_project(slug)
        return await self._call(self._store.changes(slug, since_ns))

    @staticmethod
    def _strip_slug(slug: str, out: dict[str, Any]) -> dict[str, Any]:
        """Return project-relative paths to the client."""
        p = out.get("path")
        if isinstance(p, str) and p.startswith(slug + "/"):
            out = {**out, "path": p[len(slug) + 1:]}
        return out
