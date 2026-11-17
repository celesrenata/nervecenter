"""Owner API for infrastructure connectors and plans (NS-ADR-120).

Signed-in people only (SSO or NerveCenter session; never devices), behind the
normal SSO ingress. Secrets are write-only: ``PUT .../secret`` stores a token
or SSH key in the vault and no response ever contains it.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from backend.api.auth import current_access
from backend.api.endpoints.mail_security import _eligible_for_server_scopes
from backend.infra import executor
from backend.infra.service import InfraInputError, InfraService, get_service

router = APIRouter(prefix="/api/infra", tags=["infra"])
Claims = Annotated[dict[str, Any], Depends(current_access)]
MAX_BODY = 64 * 1024


def _actor(claims: dict[str, Any]) -> str:
    if not _eligible_for_server_scopes(claims):
        raise HTTPException(status_code=403, detail="A signed-in person is required.")
    return str(claims.get("preferred_username") or claims.get("email") or claims.get("sub")
               or "sso-user")


def _svc(request: Request) -> InfraService:
    svc = get_service(request.app)
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
    if isinstance(exc, executor.ExecutorError):
        return HTTPException(status_code=502, detail=f"Command failed: {exc.code}")
    return HTTPException(status_code=422, detail=str(exc))


_EXPECTED = (KeyError, InfraInputError, executor.ExecutorError)


@router.get("/connectors")
async def list_connectors(request: Request, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    return {"connectors": await _svc(request).list_connectors()}


@router.post("/connectors", status_code=201)
async def create_connector(request: Request, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    body = await _body(request)
    try:
        return await _svc(request).create_connector(
            str(body.get("kind") or ""), body.get("name"), body.get("config"))
    except _EXPECTED as exc:
        raise _bad(exc) from None


@router.patch("/connectors/{ident}")
async def update_connector(request: Request, ident: str, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    body = await _body(request)
    try:
        return await _svc(request).update_connector(ident, body.get("config"))
    except _EXPECTED as exc:
        raise _bad(exc) from None


@router.put("/connectors/{ident}/secret")
async def set_secret(request: Request, ident: str, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    body = await _body(request)
    try:
        return await _svc(request).set_secret(ident, body)
    except _EXPECTED as exc:
        raise _bad(exc) from None


@router.delete("/connectors/{ident}", status_code=204)
async def delete_connector(request: Request, ident: str, claims: Claims) -> Response:
    _actor(claims)
    try:
        await _svc(request).delete_connector(ident)
    except _EXPECTED as exc:
        raise _bad(exc) from None
    return Response(status_code=204)


@router.post("/connectors/{ident}/scan-host-key")
async def scan_host_key(request: Request, ident: str, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    try:
        return await _svc(request).scan_host_key(ident)
    except _EXPECTED as exc:
        raise _bad(exc) from None


@router.post("/connectors/{ident}/trust-host-key")
async def trust_host_key(request: Request, ident: str, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    body = await _body(request)
    prints = body.get("fingerprints")
    if not isinstance(prints, list) or not all(isinstance(p, str) for p in prints):
        raise HTTPException(status_code=422, detail="fingerprints must be a list of strings")
    try:
        return await _svc(request).trust_host_key(ident, prints)
    except _EXPECTED as exc:
        raise _bad(exc) from None


@router.post("/connectors/{ident}/test")
async def test_connector(request: Request, ident: str, claims: Claims) -> dict[str, Any]:
    """A harmless read: ``kubectl version`` or ``uname -a``."""
    _actor(claims)
    svc = _svc(request)
    try:
        conns = {c["name"]: c for c in await svc.list_connectors()}
        conn = conns.get(ident) or next(c for c in conns.values() if c["id"] == ident)
    except StopIteration:
        raise HTTPException(status_code=404, detail="Not found.") from None
    step = ({"args": ["version", "-o", "json"]} if conn["kind"] == "kubernetes"
            else {"command": "uname"})
    return await svc.run_read(conn["name"], step)


@router.get("/plans")
async def list_plans(request: Request, claims: Claims, status: str | None = None
                     ) -> dict[str, Any]:
    _actor(claims)
    return {"plans": await _svc(request).list_plans(status)}


@router.post("/plans", status_code=201)
async def create_plan(request: Request, claims: Claims) -> dict[str, Any]:
    """The owner can write a plan too; it still needs an explicit approval."""
    actor = _actor(claims)
    try:
        return await _svc(request).propose(f"owner:{actor}", await _body(request))
    except _EXPECTED as exc:
        raise _bad(exc) from None


@router.get("/plans/{plan_id}")
async def get_plan(request: Request, plan_id: str, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    try:
        return await _svc(request).get_plan(plan_id)
    except _EXPECTED as exc:
        raise _bad(exc) from None


@router.post("/plans/{plan_id}/approve")
async def approve_plan(request: Request, plan_id: str, claims: Claims) -> dict[str, Any]:
    actor = _actor(claims)
    body = await _body(request)
    selected = body.get("selected_steps")
    if selected is not None and (not isinstance(selected, list) or any(
            not isinstance(i, int) or isinstance(i, bool) for i in selected)):
        raise HTTPException(status_code=422, detail="selected_steps must be a list of integers.")
    try:
        return await _svc(request).decide(plan_id, approve=True, actor=actor,
                                          act_mode=str(body.get("act_mode") or "opencode"),
                                          selected_steps=selected)
    except _EXPECTED as exc:
        raise _bad(exc) from None


@router.post("/plans/{plan_id}/reject")
async def reject_plan(request: Request, plan_id: str, claims: Claims) -> dict[str, Any]:
    actor = _actor(claims)
    try:
        return await _svc(request).decide(plan_id, approve=False, actor=actor)
    except _EXPECTED as exc:
        raise _bad(exc) from None


@router.post("/plans/{plan_id}/verify")
async def verify_plan(request: Request, plan_id: str, claims: Claims) -> dict[str, Any]:
    _actor(claims)
    try:
        return {"verify": await _svc(request).run_verify(plan_id)}
    except _EXPECTED as exc:
        raise _bad(exc) from None
