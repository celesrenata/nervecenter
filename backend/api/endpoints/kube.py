"""Owner API for the kube agent (NS-ADR-120).

Signed-in people only (SSO or NerveCenter session; never devices or service tokens). Shows the
issue ledger, the waste report and the settings, and holds the owner's per-issue Override of a
protected namespace (``kube_overrides``: who, when, expiry; revocable).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from backend.api.auth import current_access
from backend.api.endpoints.mail_security import _eligible_for_server_scopes
from backend.kube.monitor import CheckBusy, KubeMonitor, get_monitor
from backend.kube.settings import KubeSettingsError, classify_namespace, settings_view
from backend.kube.store import KubeServices, get_services

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/kube", tags=["kube"])
Claims = Annotated[dict[str, Any], Depends(current_access)]
MAX_BODY = 64 * 1024


def _actor(claims: dict[str, Any]) -> str:
    if not _eligible_for_server_scopes(claims):
        raise HTTPException(status_code=403, detail="A signed-in person is required.")
    return str(claims.get("preferred_username") or claims.get("email") or claims.get("sub")
               or "sso-user")


def _svc(request: Request) -> KubeServices:
    svc = get_services(request.app)
    if svc is None:
        raise HTTPException(status_code=503, detail="durable storage is not configured")
    return svc


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(status_code=413, detail="Request body is too large.")
    try:
        data = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Send a JSON object.") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Send a JSON object.")
    return data


def _bad(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Not found.")
    return HTTPException(status_code=422, detail=str(exc))


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _override_view(ov: dict[str, Any]) -> dict[str, Any]:
    return {k: _iso(v) for k, v in ov.items()}


async def _issue_views(svc: KubeServices, now: datetime) -> list[dict[str, Any]]:
    settings = await svc.settings.get()
    overrides = await svc.overrides.active(now)
    out: list[dict[str, Any]] = []
    for issue in await svc.issues.list("open"):
        kind = classify_namespace(issue["namespace"], settings, overrides, issue["key"])
        mode = {"known_down": "known_down", "protected": "diagnose_only"}.get(kind, "normal")
        active = next((o for o in overrides if o["issue_key"] == issue["key"]
                       and o["namespace"] == issue["namespace"]), None)
        view = {k: _iso(v) for k, v in issue.items()}
        view["mode"] = mode
        view["override"] = _override_view(active) if active else None
        out.append(view)
    return out


@router.get("/status")
async def status(request: Request, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    svc = _svc(request)
    settings = await svc.settings.get()
    now = datetime.now(UTC)
    issues = await _issue_views(svc, now)
    monitor = get_monitor(request.app)
    latest = await svc.reports.latest("snapshot")
    snap = (latest or {}).get("data") or {}
    return {
        "settings_log": [{"at": _iso(r["created_at"]), **r["data"]}
                         for r in await svc.reports.recent("settings", 10)],
        "coverage": snap.get("coverage"),
        "coverage_at": _iso((latest or {}).get("created_at")),
        "settings": settings_view(settings),
        "last_check_at": _iso(settings["last_check_at"]),
        "last_waste_at": _iso(settings["last_waste_at"]),
        "monitor": {"running": bool(monitor and monitor.running),
                    "last": monitor.last if monitor else None},
        "counts": {
            "open": sum(1 for i in issues if i["mode"] != "known_down"),
            "known_down": sum(1 for i in issues if i["mode"] == "known_down"),
            "diagnose_only": sum(1 for i in issues if i["mode"] == "diagnose_only"),
            "overrides": len(await svc.overrides.active(now)),
        },
    }


@router.get("/issues")
async def list_issues(request: Request, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    svc = _svc(request)
    now = datetime.now(UTC)
    return {"issues": await _issue_views(svc, now),
            "overrides": [_override_view(o) for o in await svc.overrides.active(now)]}


@router.get("/waste")
async def latest_waste(request: Request, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    return {"report": await _svc(request).reports.latest("waste")}


@router.put("/settings")
async def put_settings(request: Request, claims: Claims) -> dict[str, Any]:
    actor = _actor(claims)
    body = await _body(request)
    svc = _svc(request)
    try:
        before = await svc.settings.get()
        after = await svc.settings.update(body, actor)
    except KubeSettingsError as exc:
        raise _bad(exc) from None
    changes = {k: {"from": before.get(k), "to": after.get(k)} for k in body
               if before.get(k) != after.get(k)}
    if changes:  # who changed what, kept with the reports and shown on /kube
        log.info("kube.settings_changed", actor=actor, fields=sorted(changes))
        await svc.reports.add("settings", {"actor": actor, "changes": changes})
    return settings_view(after)


@router.post("/check")
async def check_now(request: Request, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    _svc(request)
    monitor: KubeMonitor | None = get_monitor(request.app)
    if monitor is None:
        raise HTTPException(status_code=503, detail="The kube monitor is not available.")
    try:
        return await monitor.check()
    except CheckBusy:
        raise HTTPException(status_code=409, detail="A check is already running.") from None


@router.post("/issues/{key:path}/override", status_code=201)
async def create_override(request: Request, key: str, claims: Claims) -> dict[str, Any]:
    """Only for an open issue in a protected namespace that is diagnose-only right now."""
    actor = _actor(claims)
    svc = _svc(request)
    now = datetime.now(UTC)
    issue = next((i for i in await _issue_views(svc, now) if i["key"] == key), None)
    if issue is None:
        raise HTTPException(status_code=404, detail="Not found.")
    if issue["mode"] != "diagnose_only":
        raise HTTPException(status_code=422,
                            detail="Only a diagnose-only issue in a protected namespace "
                                   "can be overridden.")
    try:
        return _override_view(await svc.overrides.create(key, issue["namespace"], actor, now))
    except KubeSettingsError as exc:
        raise _bad(exc) from None


@router.delete("/overrides/{override_id}", status_code=204)
async def revoke_override(request: Request, override_id: str, claims: Claims) -> Response:
    _actor(claims)
    try:
        await _svc(request).overrides.revoke(override_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Not found.") from None
    return Response(status_code=204)


@router.post("/enable-proposals")
async def enable_proposals(request: Request, claims: Claims) -> dict[str, Any]:
    """Clears the diagnose-only-first-run flag so kube_doctor may propose plans."""
    actor = _actor(claims)
    return settings_view(await _svc(request).settings.update(
        {"diagnose_only_until_first_report": False}, actor))
