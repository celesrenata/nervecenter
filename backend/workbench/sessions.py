"""Workbench opencode Session proxy (Req 3.6-3.8, 3.10, 3.12, 4.8, 8.1, 8.3, 8.5, 8.6, 10.2).

``SessionService`` wraps the extended ``OpenCodeClient``. It creates Sessions
rooted at ``/projects/<slug>``, relays the (global) opencode event feed filtered
to one Session, mirrors Sessions into ``workbench_sessions`` and records token
usage. It never touches ``coding_handoffs`` (Req 10.2).

Undocumented opencode details, kept configurable:
  * The event name for file edits is not documented. ``file_changed_events``
    treats an event as an edit when its ``type`` contains one of the substrings
    in ``WORKBENCH_FILE_EDIT_EVENTS`` (comma separated; default
    ``file.edited,file.changed,file.written,file.updated``), or when it is a
    tool event (type contains ``tool``) whose tool name is edit/write/patch/
    multiedit. The path is read from the first of path/filePath/file/filepath
    found in ``data`` (or ``data.input``/``data.args``). Confirm against the
    pinned image and adjust the env var.
  * Where token usage sits in an event is undocumented. ``extract_usage`` looks
    for a ``usage``/``tokens`` dict in ``data`` (or ``data.info``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.handoffs.central import (
    OpenCodeClient,
    OpenCodeError,
    event_session_id,
)
from backend.persistence.models.workbench import WorkbenchProject, WorkbenchSession

from .phase import default_model, default_small_model
from .usage import UsageRecorder
from .validation import needs_nonlocal_confirm, validate_message

logger = logging.getLogger("nervecenter.workbench.sessions")

REQUEST_TIMEOUT_S = 5.0
ABORT_TIMEOUT_S = 10.0
PROJECTS_ROOT = "/projects"
OMNIROUTE_PROVIDER = "omniroute"
OMNIROUTE_BASE_URL = "http://omniroute.nervecenter:20128/v1"

FILE_EDIT_EVENTS_ENV = "WORKBENCH_FILE_EDIT_EVENTS"
DEFAULT_FILE_EDIT_EVENTS = ("file.edited", "file.changed", "file.written", "file.updated")
EDIT_TOOLS = {"edit", "write", "patch", "multiedit", "apply_patch"}
_PATH_KEYS = ("path", "filePath", "file", "filepath")


class SessionError(Exception):
    """Carries the HTTP status and error code the route should return."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code}: {message}")


def _unavailable() -> SessionError:
    return SessionError(503, "opencode_unavailable", "opencode did not respond within 5 s")


def _strip_provider(model: str) -> str:
    prefix = OMNIROUTE_PROVIDER + "/"
    return model[len(prefix):] if model.startswith(prefix) else model


def model_needs_confirm(model: str | None, confirmed: bool) -> bool:
    """Non-local check that tolerates the ``omniroute/`` provider prefix."""
    return needs_nonlocal_confirm(_strip_provider(model) if model else model, confirmed)


def build_opencode_config(
    model: str | None = None,
    small_model: str | None = None,
    *,
    base_url: str = OMNIROUTE_BASE_URL,
) -> dict[str, Any]:
    """Generated opencode config: OmniRoute is the only provider, no API keys (Req 8.1, 8.4)."""
    model = model or default_model()
    small_model = small_model or default_small_model()
    ids = {_strip_provider(m) for m in (model, small_model)}

    def ref(m: str) -> str:
        return m if m.startswith(OMNIROUTE_PROVIDER + "/") else f"{OMNIROUTE_PROVIDER}/{m}"

    return {
        "$schema": "https://opencode.ai/config.json",
        "enabled_providers": [OMNIROUTE_PROVIDER],
        "provider": {
            OMNIROUTE_PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "OmniRoute",
                "options": {"baseURL": base_url},
                "models": {i: {"name": i} for i in sorted(ids)},
            }
        },
        "model": ref(model),
        "small_model": ref(small_model),
    }


def file_edit_event_names(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    raw = (os.environ if env is None else env).get(FILE_EDIT_EVENTS_ENV, "")
    names = tuple(n.strip().lower() for n in raw.split(",") if n.strip())
    return names or DEFAULT_FILE_EDIT_EVENTS


def _find_path(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in _PATH_KEYS:
        if isinstance(data.get(key), str) and data[key]:
            return data[key]
    for nested in ("input", "args", "state", "properties"):
        found = _find_path(data.get(nested))
        if found:
            return found
    return None


def file_changed_events(
    event: dict[str, Any], names: tuple[str, ...] | None = None
) -> list[dict[str, str]]:
    """Heuristic: ``[{"type": "file_changed", "path": ...}]`` for edit events, else []."""
    etype = str(event.get("type", "")).lower()
    data = event.get("data")
    names = names if names is not None else file_edit_event_names()
    path = _find_path(data)
    if not path:
        return []
    if any(n in etype for n in names):
        return [{"type": "file_changed", "path": path}]
    if "tool" in etype and isinstance(data, dict):
        tool = str(data.get("tool") or data.get("name") or "").lower()
        if tool in EDIT_TOOLS:
            return [{"type": "file_changed", "path": path}]
    return []


def extract_usage(event: dict[str, Any]) -> tuple[int, int] | None:
    """(input_tokens, output_tokens) when the event carries usage info."""
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    for holder in (data, data.get("info") if isinstance(data.get("info"), dict) else {}):
        for key in ("usage", "tokens"):
            u = holder.get(key)
            if not isinstance(u, dict):
                continue
            inp = next((u[k] for k in ("input", "input_tokens", "inputTokens") if k in u), None)
            out = next((u[k] for k in ("output", "output_tokens", "outputTokens") if k in u), None)
            if isinstance(inp, int) and isinstance(out, int) and inp >= 0 and out >= 0:
                return inp, out
    return None


class SessionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[Any],
        client: OpenCodeClient,
        usage: UsageRecorder | None = None,
    ) -> None:
        self._sf = session_factory
        self._client = client
        self._usage = usage or UsageRecorder(session_factory)

    async def _project(self, slug: str) -> WorkbenchProject:
        async with self._sf() as s:
            rows = (
                await s.execute(select(WorkbenchProject).where(WorkbenchProject.slug == slug))
            ).scalars().all()
        opencode = [p for p in rows if p.kind == "opencode"]
        if opencode:
            return opencode[0]
        if rows:
            raise SessionError(409, "editor_not_supported", "editor supports opencode projects")
        raise SessionError(404, "project_not_found", slug)

    async def _mirror(self, session_id: str) -> WorkbenchSession:
        async with self._sf() as s:
            row = await s.get(WorkbenchSession, session_id)
        if row is None:
            raise SessionError(404, "session_not_found", session_id)
        return row

    async def create(
        self,
        slug: str,
        *,
        model: str | None = None,
        small_model: str | None = None,
        confirm_nonlocal: bool = False,
        title: str | None = None,
    ) -> dict[str, Any]:
        project = await self._project(slug)
        model = model or default_model()
        small_model = small_model or default_small_model()
        for m in (model, small_model):
            if model_needs_confirm(m, confirm_nonlocal):
                raise SessionError(409, "nonlocal_confirm_required", f"{m} is not a local route")
        try:
            sid = await asyncio.wait_for(
                self._client.create_session(
                    title=title or project.title,
                    directory=f"{PROJECTS_ROOT}/{project.slug}",
                    push_approved=project.push_approved,
                    model=model,
                    small_model=small_model,
                ),
                REQUEST_TIMEOUT_S,
            )
        except (OpenCodeError, TimeoutError):
            raise _unavailable() from None
        now = datetime.now(UTC)
        async with self._sf() as s:
            async with s.begin():
                s.add(
                    WorkbenchSession(
                        id=sid,
                        project_id=project.id,
                        title=title or project.title,
                        status="running",
                        updated_at=now,
                        model=model,
                        small_model=small_model,
                    )
                )
                p = await s.get(WorkbenchProject, project.id)
                if p is not None:
                    p.last_activity_at = now
        return {
            "id": sid,
            "project_slug": project.slug,
            "location": f"{PROJECTS_ROOT}/{project.slug}",
            "status": "running",
            "model": model,
            "small_model": small_model,
        }

    async def send_message(self, session_id: str, text: str) -> None:
        row = await self._mirror(session_id)
        if not validate_message(text, row.status == "running"):
            raise SessionError(422, "invalid_message", "empty, too long, or session not running")
        try:
            await asyncio.wait_for(self._client.send_message(session_id, text), REQUEST_TIMEOUT_S)
        except (OpenCodeError, TimeoutError):
            raise _unavailable() from None
        await self._touch(row, row.status)

    async def abort(self, session_id: str) -> dict[str, str]:
        """Abort; 504 and status stays ``running`` if not confirmed within 10 s (Req 3.10)."""
        row = await self._mirror(session_id)
        try:
            await asyncio.wait_for(self._client.abort_session(session_id), ABORT_TIMEOUT_S)
        except TimeoutError:
            raise SessionError(504, "abort_timeout", "abort not confirmed within 10 s") from None
        except OpenCodeError:
            raise _unavailable() from None
        await self._touch(row, "aborted")
        return {"id": session_id, "status": "aborted"}

    async def list(self, slug: str) -> list[dict[str, Any]]:
        project = await self._project(slug)
        async with self._sf() as s:
            rows = (
                await s.execute(
                    select(WorkbenchSession)
                    .where(WorkbenchSession.project_id == project.id)
                    .order_by(WorkbenchSession.updated_at.desc())
                )
            ).scalars().all()
        return [
            {
                "id": r.id, "title": r.title, "status": r.status, "model": r.model,
                "small_model": r.small_model,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]

    async def _touch(self, row: WorkbenchSession, status: str | None) -> None:
        now = datetime.now(UTC)
        async with self._sf() as s:
            async with s.begin():
                r = await s.get(WorkbenchSession, row.id)
                if r is not None:
                    r.status = status
                    r.updated_at = now
                    p = await s.get(WorkbenchProject, r.project_id)
                    if p is not None:
                        p.last_activity_at = now

    async def events(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield this Session's events in received order, plus ``file_changed`` events."""
        row = await self._mirror(session_id)
        names = file_edit_event_names()
        try:
            async for event in self._client.stream_events():
                if event_session_id(event) != session_id:
                    continue
                yield event
                for changed in file_changed_events(event, names):
                    yield changed
                usage = extract_usage(event)
                if usage and row.model:
                    try:
                        await self._usage.record(session_id, row.model, usage[0], usage[1])
                    except Exception:  # noqa: BLE001 - usage must not break the stream
                        logger.warning("workbench usage record failed", exc_info=True)
        except OpenCodeError:
            raise _unavailable() from None
