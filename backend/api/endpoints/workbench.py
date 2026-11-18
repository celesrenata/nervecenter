"""Workbench HTTP API (spec opencode-menagerie-workbench, "HTTP interface").

Prefix ``/api/workbench``. Every route needs a signed-in person
(``current_access``; Req 6.1, 6.4) and every state-changing request must carry
``X-NC-Workbench: 1`` (Req 6.10). Routers are mounted by ``WORKBENCH_PHASE``
through :func:`mounted_routers`. Create, archive and delete write an audit entry
on success and on failure (Req 6.11).
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.auth import current_access
from backend.api.endpoints.mail_security import _eligible_for_server_scopes
from backend.handoffs.central import PASSWORD_ENV, URL_ENV, OpenCodeClient
from backend.workbench import phase
from backend.workbench.audit import AuditLog
from backend.workbench.files import FileError, FileService
from backend.workbench.health import store_health
from backend.workbench.ide import (
    AmbiguousSlug,
    IdeInstance,
    IdeLimitReached,
    IdeProject,
    IdeStartFailed,
)
from backend.workbench.ide_runtime import get_ide_manager
from backend.workbench.projects import (
    ProjectConflictError,
    ProjectNotFoundError,
    ProjectService,
    ProjectValidationError,
)
from backend.workbench.sessions import SessionError, SessionService
from backend.workbench.store import (
    StoreRejected,
    StoreUnavailable,
    get_store_clients,
)
from backend.workbench.usage import UsageRecorder
from backend.workbench.validation import (
    INSTRUCTION_MAX,
    MESSAGE_MAX,
    SELECTION_MAX,
    validate_selection_action,
)

log = logging.getLogger("nervecenter.workbench.api")

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
WORKBENCH_HEADER = "X-NC-Workbench"

Claims = Annotated[dict[str, Any], Depends(current_access)]


def _actor(claims: dict[str, Any]) -> str:
    if not _eligible_for_server_scopes(claims):
        raise HTTPException(status_code=403, detail="A signed-in person is required.")
    return str(
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("sub")
        or "sso-user"
    )


async def _require_person(claims: Claims) -> dict[str, Any]:
    _actor(claims)
    return claims


async def _require_header(request: Request) -> None:
    if request.method in WRITE_METHODS and request.headers.get(WORKBENCH_HEADER) != "1":
        raise HTTPException(status_code=403, detail=f"{WORKBENCH_HEADER}: 1 is required.")


router = APIRouter(
    prefix="/api/workbench",
    tags=["workbench"],
    dependencies=[Depends(_require_person), Depends(_require_header)],
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ProjectCreate(_Body):
    slug: str
    kind: str
    title: str | None = None
    git_url: str | None = None


class ProjectImport(_Body):
    kind: str
    name: str | None = None
    slug: str | None = None
    title: str | None = None


class DeleteBody(_Body):
    confirm: str | None = None


class PushApproval(_Body):
    approved: bool


def _factory(request: Request) -> async_sessionmaker:
    factory = getattr(request.app.state, "workbench_session_factory", None)
    if factory is None:
        engine = getattr(request.app.state, "db_engine", None)
        if engine is None:
            raise HTTPException(status_code=503, detail="durable storage is not configured")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        request.app.state.workbench_session_factory = factory
    return factory


def _service(request: Request) -> ProjectService:
    svc = getattr(request.app.state, "workbench_projects", None)
    if svc is None:
        svc = ProjectService(_factory(request), get_store_clients())
        request.app.state.workbench_projects = svc
    return svc


def _audit(request: Request) -> AuditLog:
    return AuditLog(_factory(request))


def _store_error(exc: Exception, *, clone: bool = False) -> HTTPException:
    """Map a store failure to an HTTP error that names the store (Req 1.8, 2.8)."""
    if isinstance(exc, StoreUnavailable):
        if clone:
            reason = exc.reason
            if reason == "http 504" or "Timeout" in reason:
                return HTTPException(
                    status_code=504,
                    detail={"error": "clone_timeout", "store": exc.kind},
                )
            if reason == "http 502":
                return HTTPException(
                    status_code=502,
                    detail={"error": "clone_failed", "store": exc.kind},
                )
        return HTTPException(
            status_code=503, detail={"error": "store_unavailable", "store": exc.kind}
        )
    if isinstance(exc, StoreRejected):
        if exc.status == 409:
            return HTTPException(status_code=409, detail={"error": "project_exists"})
        status = 400 if exc.status == 400 else 422
        return HTTPException(
            status_code=status,
            detail={"error": exc.code, "store": exc.kind, "message": exc.message},
        )
    raise exc


def _map(exc: Exception, *, clone: bool = False) -> HTTPException:
    if isinstance(exc, ProjectValidationError):
        return HTTPException(status_code=422, detail={"error": "validation", "message": str(exc)})
    if isinstance(exc, ProjectConflictError):
        return HTTPException(status_code=409, detail={"error": "project_exists"})
    if isinstance(exc, ProjectNotFoundError):
        return HTTPException(status_code=404, detail={"error": "not_found"})
    if isinstance(exc, FileError):
        return HTTPException(status_code=exc.status, detail=exc.body())
    if isinstance(exc, SessionError):
        return HTTPException(
            status_code=exc.status, detail={"error": exc.code, "message": exc.message}
        )
    if isinstance(exc, AmbiguousSlug):
        return HTTPException(status_code=409, detail={"error": "ambiguous_slug"})
    if isinstance(exc, IdeLimitReached):
        return HTTPException(
            status_code=409,
            detail={
                "error": "ide_limit_reached",
                "message": f"limit of {exc.limit} concurrent IDE instances",
            },
        )
    if isinstance(exc, IdeStartFailed):
        return HTTPException(
            status_code=502,
            detail={"error": "ide_start_failed", "state": "failed", "reason": exc.message},
        )
    return _store_error(exc, clone=clone)


_HANDLED = (
    AmbiguousSlug,
    IdeLimitReached,
    IdeStartFailed,
    FileError,
    SessionError,
    ProjectValidationError,
    ProjectConflictError,
    ProjectNotFoundError,
    StoreUnavailable,
    StoreRejected,
)


async def _record(
    request: Request, actor: str, slug: str | None, action: str, outcome: str
) -> None:
    try:
        await _audit(request).record(actor, slug, action, outcome)
    except Exception as exc:  # noqa: BLE001 - auditing must not mask the result
        log.warning("workbench audit write failed: %s", type(exc).__name__)


async def _audited(
    request: Request,
    claims: dict[str, Any],
    slug: str | None,
    action: str,
    call: Any,
    *,
    clone: bool = False,
) -> Any:
    actor = _actor(claims)
    try:
        result = await call
    except _HANDLED as exc:
        await _record(request, actor, slug, action, "failure")
        raise _map(exc, clone=clone) from exc
    except Exception:
        await _record(request, actor, slug, action, "failure")
        raise
    await _record(request, actor, slug, action, "success")
    return result


@router.get("/config")
async def get_config() -> dict[str, Any]:
    return {
        "phase": phase.workbench_phase(),
        "ide_enabled": phase.phase_at_least(3)
        and phase.ide_enabled()
        and bool((os.environ.get("WORKBENCH_IDE_IMAGE") or "").strip()),
        "default_models": {
            "model": phase.default_model(),
            "small_model": phase.default_small_model(),
        },
    }


@router.get("/health")
async def get_health() -> dict[str, Any]:
    return await store_health()


@router.get("/projects")
async def list_projects(request: Request) -> dict[str, Any]:
    return {"projects": await _service(request).list()}


@router.post("/projects", status_code=201)
async def create_project(body: ProjectCreate, request: Request, claims: Claims) -> dict[str, Any]:
    title = body.title if body.title is not None else body.slug
    return await _audited(
        request,
        claims,
        body.slug,
        "project.create",
        _service(request).create(
            slug=body.slug, title=title, kind=body.kind, git_url=body.git_url
        ),
        clone=body.git_url is not None,
    )


@router.get("/unregistered")
async def list_unregistered(request: Request) -> dict[str, Any]:
    try:
        rows = await _service(request).list_unregistered()
    except _HANDLED as exc:
        raise _map(exc) from exc
    return {"unregistered": [{**r, "name": r["slug"]} for r in rows]}


@router.post("/projects/import", status_code=201)
async def import_project(body: ProjectImport, request: Request) -> dict[str, Any]:
    slug = body.name if body.name is not None else body.slug
    if slug is None:
        raise HTTPException(
            status_code=422, detail={"error": "validation", "message": "name is required"}
        )
    try:
        return await _service(request).import_dir(kind=body.kind, slug=slug, title=body.title)
    except _HANDLED as exc:
        raise _map(exc) from exc


@router.get("/projects/{kind}/{slug}")
async def get_project(kind: str, slug: str, request: Request) -> dict[str, Any]:
    try:
        return await _service(request).get(kind, slug)
    except _HANDLED as exc:
        raise _map(exc) from exc


@router.post("/projects/{kind}/{slug}/archive")
async def archive_project(
    kind: str, slug: str, request: Request, claims: Claims
) -> dict[str, Any]:
    return await _audited(
        request, claims, slug, "project.archive", _service(request).archive(kind, slug)
    )


@router.delete("/projects/{kind}/{slug}")
async def delete_project(
    kind: str, slug: str, body: DeleteBody, request: Request, claims: Claims
) -> dict[str, Any]:
    await _audited(
        request,
        claims,
        slug,
        "project.delete",
        _service(request).delete(kind, slug, body.confirm),
    )
    return {"deleted": True, "kind": kind, "slug": slug}


@router.post("/projects/{kind}/{slug}/push-approval")
async def set_push_approval(
    kind: str, slug: str, body: PushApproval, request: Request
) -> dict[str, Any]:
    try:
        return await _service(request).set_push_approved(kind, slug, body.approved)
    except _HANDLED as exc:
        raise _map(exc) from exc


# Phase 2 routes. Same SSO and header guards.
phase2_router = APIRouter(
    prefix="/api/workbench",
    tags=["workbench"],
    dependencies=[Depends(_require_person), Depends(_require_header)],
)
class FileWrite(_Body):
    path: str
    content: str
    base_mtime_ns: int | None = None
class FsOperation(_Body):
    op: str
    path: str
    type: str | None = None
    is_dir: bool | None = None
    new_name: str | None = None
    dest: str | None = None
class SessionCreate(_Body):
    model: str | None = None
    small_model: str | None = None
    confirm_nonlocal: bool = False
    title: str | None = None
class MessageBody(_Body):
    text: str = Field(min_length=1, max_length=MESSAGE_MAX)
class SelectionBody(_Body):
    path: str
    selection: str = Field(max_length=SELECTION_MAX)
    instruction: str = Field(min_length=1, max_length=INSTRUCTION_MAX)
def _files(request: Request) -> FileService:
    svc = getattr(request.app.state, "workbench_files", None)
    if svc is None:
        svc = FileService(_factory(request), get_store_clients()["opencode"])
        request.app.state.workbench_files = svc
    return svc
def _sessions(request: Request) -> SessionService:
    svc = getattr(request.app.state, "workbench_sessions", None)
    if svc is None:
        url = (os.environ.get(URL_ENV) or "").strip()
        password = (os.environ.get(PASSWORD_ENV) or "").strip()
        if not url or not password:
            raise HTTPException(
                status_code=503,
                detail={"error": "opencode_unavailable", "message": "opencode is not configured"},
            )
        svc = SessionService(_factory(request), OpenCodeClient(url, password))
        request.app.state.workbench_sessions = svc
    return svc
async def _run(call: Any) -> Any:
    try:
        return await call
    except _HANDLED as exc:
        raise _map(exc) from exc
@phase2_router.get("/projects/{slug}/files")
async def list_files(slug: str, request: Request, path: str = "") -> dict[str, Any]:
    out = await _run(_files(request).list(slug, path))
    return {**out, "path": out.get("path", path), "entries": out.get("entries", [])}
@phase2_router.get("/projects/{slug}/file")
async def read_file(slug: str, path: str, request: Request) -> dict[str, Any]:
    return await _run(_files(request).read(slug, path))
@phase2_router.put("/projects/{slug}/file")
async def write_file(
    slug: str, body: FileWrite, request: Request, claims: Claims
) -> dict[str, Any]:
    return await _audited(
        request,
        claims,
        slug,
        "file.write",
        _files(request).write(slug, body.path, body.content, body.base_mtime_ns),
    )
@phase2_router.post("/projects/{slug}/fs")
async def fs_operation(slug: str, body: FsOperation, request: Request) -> dict[str, Any]:
    is_dir = body.is_dir if body.is_dir is not None else body.type == "dir"
    return await _run(
        _files(request).op(
            slug, body.op, body.path, is_dir=is_dir, new_name=body.new_name, dest=body.dest
        )
    )
@phase2_router.get("/projects/{slug}/changes")
async def file_changes(slug: str, request: Request, since: str | None = None) -> dict[str, Any]:
    try:
        since_ns = int(since) if since else 0
    except ValueError:
        raise HTTPException(
            status_code=422, detail={"error": "validation", "message": "since must be a token"}
        ) from None
    out = await _run(_files(request).changes(slug, max(since_ns, 0)))
    changed = out.get("changed", [])
    return {
        **out,
        "token": str(out.get("now_ns", since_ns)),
        "paths": [c["path"] if isinstance(c, dict) else c for c in changed],
    }
@phase2_router.post("/projects/{slug}/sessions", status_code=201)
async def create_session(
    slug: str, body: SessionCreate, request: Request, claims: Claims
) -> dict[str, Any]:
    actor = _actor(claims)
    try:
        svc = _sessions(request)
    except HTTPException:
        await _record(request, actor, slug, "session.start", "failure")
        raise
    return await _audited(
        request,
        claims,
        slug,
        "session.start",
        svc.create(
            slug,
            model=body.model,
            small_model=body.small_model,
            confirm_nonlocal=body.confirm_nonlocal,
            title=body.title,
        ),
    )
@phase2_router.get("/projects/{slug}/sessions")
async def list_sessions(slug: str, request: Request) -> dict[str, Any]:
    return {"sessions": await _run(_sessions(request).list(slug))}
def _sse_frame(event: dict[str, Any]) -> str:
    if "data" in event:
        name, data = str(event.get("type") or "message"), event["data"]
    else:
        name = str(event.get("type") or "message")
        data = {k: v for k, v in event.items() if k != "type"}
    name = name.replace("\r", " ").replace("\n", " ")
    return f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"
@phase2_router.get("/sessions/{session_id}/events")
async def session_events(session_id: str, request: Request) -> StreamingResponse:
    svc = _sessions(request)
    await _run(svc._mirror(session_id))  # 404 before the stream starts

    async def gen() -> AsyncIterator[str]:
        try:
            async for event in svc.events(session_id):
                yield _sse_frame(event)
        except SessionError as exc:
            yield _sse_frame({"type": "error", "data": {"error": exc.code, "status": exc.status}})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
@phase2_router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, body: MessageBody, request: Request) -> dict[str, Any]:
    await _run(_sessions(request).send_message(session_id, body.text))
    return {"id": session_id, "sent": True}
@phase2_router.post("/sessions/{session_id}/abort")
async def abort_session(session_id: str, request: Request) -> dict[str, Any]:
    return await _run(_sessions(request).abort(session_id))
@phase2_router.post("/sessions/{session_id}/selection-action")
async def selection_action(
    session_id: str, body: SelectionBody, request: Request
) -> dict[str, Any]:
    svc = _sessions(request)
    row = await _run(svc._mirror(session_id))
    if not validate_selection_action(body.selection, body.instruction, row.status == "running"):
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_action", "message": "invalid selection action"},
        )
    text = f"{body.instruction}\n\nFile: {body.path}\n\nSelection:\n{body.selection}"
    await _run(svc.send_message(session_id, text[:MESSAGE_MAX]))
    return {"id": session_id, "sent": True}
@phase2_router.get("/usage")
async def list_usage(
    request: Request, session_id: str | None = None, limit: int = 100
) -> dict[str, Any]:
    """Recorded model name and token counts per Session (Req 8.9)."""
    rows = await UsageRecorder(_factory(request)).list_usage(session_id, limit)
    return {
        "usage": [
            {
                "id": r.id,
                "session_id": r.session_id,
                "model": r.model,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "recorded_at": r.recorded_at.isoformat(),
            }
            for r in rows
        ]
    }


# Phase 3 routes (Browser_IDE). Same SSO and header guards.
phase3_router = APIRouter(
    prefix="/api/workbench",
    tags=["workbench"],
    dependencies=[Depends(_require_person), Depends(_require_header)],
)
STATUS_PORT = 8081
STATUS_TIMEOUT_S = 2.0


def _ide_json(inst: IdeInstance, state: str, kind: str | None = None) -> dict[str, Any]:
    """Public view of an IDE instance. Never includes pod names, IPs or keys."""
    return {
        "kind": kind or inst.kind,
        "slug": inst.slug,
        "state": state,
        "url": f"/ide/{inst.slug}/",
        "started_at": None,
        "last_traffic_at": inst.last_traffic_at.isoformat(),
        "reason": None,
    }


def _ide_manager(request: Request) -> Any:
    try:
        return get_ide_manager(request.app)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "ide_unavailable", "message": "IDE is not configured"},
        ) from exc


@phase3_router.get("/ide")
async def list_ide(request: Request) -> dict[str, Any]:
    try:
        instances = await _ide_manager(request).list()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail={"error": "ide_unavailable", "message": "cluster is unavailable"},
        ) from exc
    return {"instances": [_ide_json(i, "running") for i in instances]}


async def _start_ide(request: Request, kind: str, slug: str) -> dict[str, Any]:
    project = await _service(request).get(kind, slug)
    mgr = _ide_manager(request)
    inst = await mgr.start(
        IdeProject(slug=slug, kind=kind, push_approved=bool(project.get("push_approved")))
    )
    return _ide_json(inst, "running", kind)


@phase3_router.post("/ide/{kind}/{slug}/ticket", status_code=204)
async def ide_ticket_cookie(kind: str, slug: str, claims: Claims) -> Response:
    """Set the path-scoped ``nc_ide`` cookie the IDE iframe authenticates with."""
    from backend.workbench.validation import validate_slug

    if kind not in ("opencode", "menagerie") or not validate_slug(slug):
        raise HTTPException(status_code=422, detail={"error": "invalid_project"})
    from backend.workbench import ide_ticket

    resp = Response(status_code=204)
    resp.set_cookie(
        ide_ticket.COOKIE_NAME,
        ide_ticket.issue(_actor(claims), slug),
        max_age=ide_ticket.TTL_S,
        path=ide_ticket.cookie_path(slug),
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return resp


@phase3_router.post("/ide/{kind}/{slug}/start")
async def start_ide(kind: str, slug: str, request: Request, claims: Claims) -> dict[str, Any]:
    return await _audited(request, claims, slug, "ide.start", _start_ide(request, kind, slug))


async def _stop_ide(request: Request, kind: str, slug: str) -> dict[str, Any]:
    mgr = _ide_manager(request)
    inst = await mgr.get(slug)
    if inst is None or inst.kind != kind:
        return {
            "kind": kind,
            "slug": slug,
            "state": "stopped",
            "url": None,
            "started_at": None,
            "last_traffic_at": None,
            "reason": None,
        }
    await mgr.stop(slug)
    return _ide_json(inst, "stopped", kind)


@phase3_router.post("/ide/{kind}/{slug}/stop")
async def stop_ide(kind: str, slug: str, request: Request, claims: Claims) -> dict[str, Any]:
    return await _audited(request, claims, slug, "ide.stop", _stop_ide(request, kind, slug))


@phase3_router.get("/ide/{kind}/{slug}/status")
async def ide_task_status(kind: str, slug: str, request: Request) -> dict[str, Any]:
    unknown = {"kind": kind, "slug": slug, "state": "unknown"}
    try:
        inst = await _ide_manager(request).get(slug)
    except HTTPException:
        raise
    except AmbiguousSlug as exc:
        raise _map(exc) from exc
    except Exception:  # noqa: BLE001
        return unknown
    if inst is None or inst.kind != kind or not inst.pod_ip:
        return unknown
    try:
        async with httpx.AsyncClient(timeout=STATUS_TIMEOUT_S) as client:
            resp = await client.get(f"http://{inst.pod_ip}:{STATUS_PORT}/status")
        body = resp.json()
        if resp.status_code != 200 or not isinstance(body, dict):
            return unknown
    except Exception:  # noqa: BLE001 - any failure is "unknown"
        return unknown
    tasks = body.get("tasks")
    state = str(body.get("state") or "unknown")
    return {
        "kind": kind,
        "slug": slug,
        "state": state,
        "running": state == "running",
        "task": None,
        "detail": None,
        "tasks": tasks if isinstance(tasks, list) else [],
        "updated_at": None,
    }


def mounted_routers() -> list[APIRouter]:
    """Routers to include for the current WORKBENCH_PHASE (0 mounts nothing).

    Phase 3 (IDE routes, also needs ``WORKBENCH_IDE_ENABLED``) and Phase 2 go
    first: Phase 2's two-segment paths (``/projects/{slug}/files``) would
    otherwise be captured by the Phase 1 ``/projects/{kind}/{slug}`` route.
    """
    routers: list[APIRouter] = []
    if phase.phase_at_least(3) and phase.ide_enabled():
        routers.append(phase3_router)
    if phase.phase_at_least(2):
        routers.append(phase2_router)
    if phase.phase_at_least(1):
        routers.append(router)
    return routers
