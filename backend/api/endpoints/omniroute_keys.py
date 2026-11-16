"""OmniRoute key management for Settings (NS-ADR-118).

Two write-only keys: ``regular`` (inference) and ``privileged`` (Skill Hub
catalog and read-only OmniRoute MCP tools). No endpoint ever returns, echoes or
logs a value; responses carry set/not-set, the source (``vault`` or ``env``) and
when the vault value last changed.

Every route needs a signed-in human session (``current_access``, no device
credentials). Writes read the raw body under a hard size cap and parse it
by hand, so a framework validation error can never echo the submitted value.
After a write the in-process resolver is refreshed, so the new key applies to
the next request in this replica; other replicas pick it up on the agent-runner
tick (15 s).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.auth import current_access
from backend.api.endpoints.mail_security import _eligible_for_server_scopes
from backend.inference import key_resolver as kr
from backend.inference.key_store import OmniRouteKeyStore, VaultUnavailableError

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/settings/omniroute", tags=["settings"])

MAX_BODY_BYTES = 2_048
MIN_KEY_CHARS = 8
MAX_KEY_CHARS = 512
Claims = Annotated[dict[str, Any], Depends(current_access)]


def _human(claims: dict[str, Any]) -> str:
    if not _eligible_for_server_scopes(claims):
        raise HTTPException(status_code=403, detail="A signed-in person is required.")
    return str(claims.get("preferred_username") or claims.get("sub"))


def _kind(kind: str) -> str:
    if kind not in kr.KINDS:
        raise HTTPException(status_code=404, detail="Unknown key. Use regular or privileged.")
    return kind


def _store(request: Request) -> OmniRouteKeyStore:
    store = getattr(request.app.state, "omniroute_key_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="Key storage is not configured; set NERVE_DB_URL."
        )
    return store


async def _read_value(request: Request) -> str:
    """The submitted key. Errors never include any part of the body."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body is too large.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body is too large.")
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
        value = body["value"] if isinstance(body, dict) else None
    except (ValueError, KeyError):
        value = None
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail='Send JSON like {"value": "<key>"}.')
    value = value.strip()
    if not (MIN_KEY_CHARS <= len(value) <= MAX_KEY_CHARS) or not value.isprintable():
        raise HTTPException(
            status_code=422,
            detail=f"The key must be {MIN_KEY_CHARS}-{MAX_KEY_CHARS} printable characters.",
        )
    if any(ch.isspace() for ch in value):
        raise HTTPException(status_code=422, detail="The key must not contain spaces.")
    return value


async def _status(store: OmniRouteKeyStore | None) -> dict[str, Any]:
    return {
        "keys": {kind: kr.status_for(kind) for kind in kr.KINDS},
        "vault_writable": bool(store and store.writable),
        "audit": await store.recent_audit() if store else [],
    }


async def _rebind(request: Request) -> None:
    """Re-evaluate consumers that bind only when a key exists (best effort)."""
    try:
        from backend.api.wiring import _bind_semantic_observer

        _bind_semantic_observer(request.app)
    except Exception as exc:  # noqa: BLE001 - the key change itself succeeded
        log.warning("omniroute_keys.rebind_failed", error=type(exc).__name__)


@router.get("")
async def get_status(request: Request, claims: Claims) -> dict[str, Any]:
    _human(claims)
    return await _status(getattr(request.app.state, "omniroute_key_store", None))


@router.put("/keys/{kind}")
async def set_key(request: Request, kind: str, claims: Claims) -> dict[str, Any]:
    actor = _human(claims)
    kind = _kind(kind)
    store = _store(request)
    try:
        value = await _read_value(request)
    except HTTPException as exc:
        outcome = f"rejected_{exc.status_code}"
        await store.audit(actor=actor, key=kind, action="set", outcome=outcome)
        raise
    try:
        await store.set(kind, value)
    except VaultUnavailableError:
        await store.audit(actor=actor, key=kind, action="set", outcome="no_vault")
        raise HTTPException(
            status_code=503,
            detail="No encryption key is configured (NERVE_SECRET_KEY), so keys cannot be stored.",
        ) from None
    except Exception as exc:  # noqa: BLE001 - never surface the cause (it could hold the value)
        log.warning("omniroute_keys.set_failed", key=kind, error=type(exc).__name__)
        await store.audit(actor=actor, key=kind, action="set", outcome="error")
        raise HTTPException(status_code=500, detail="The key could not be stored.") from None
    await store.refresh()
    await store.audit(actor=actor, key=kind, action="set", outcome="ok")
    await _rebind(request)
    return await _status(store)


@router.delete("/keys/{kind}")
async def clear_key(request: Request, kind: str, claims: Claims) -> dict[str, Any]:
    actor = _human(claims)
    kind = _kind(kind)
    store = _store(request)
    try:
        removed = await store.clear(kind)
    except Exception as exc:  # noqa: BLE001
        log.warning("omniroute_keys.clear_failed", key=kind, error=type(exc).__name__)
        await store.audit(actor=actor, key=kind, action="clear", outcome="error")
        raise HTTPException(status_code=500, detail="The key could not be cleared.") from None
    await store.refresh()
    await store.audit(actor=actor, key=kind, action="clear", outcome="ok" if removed else "not_set")
    await _rebind(request)
    return await _status(store)


# -- tests (counts and status only) -----------------------------------------------


def _classify(code: int | str | None) -> str:
    text = str(code or "")
    if text in {"401", "403", "http_401", "http_403"}:
        return "unauthorized"
    if text.startswith("http_") or text.isdigit():
        return f"http_{text.removeprefix('http_')}"
    return text or "error"


async def _test_regular() -> dict[str, Any]:
    """Inference health: the OmniRoute route list, read with the regular key."""
    key = kr.regular_key()
    if not key:
        return {"ok": False, "status": "not_set"}
    base = (os.environ.get("NERVE_OMNIROUTE_URL") or "").strip().rstrip("/").removesuffix("/v1")
    if not base:
        return {"ok": False, "status": "unconfigured"}
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            resp = await client.get(f"{base}/v1/routes", headers={"Authorization": f"Bearer {key}"})
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "status": _classify(exc.response.status_code)}
    except Exception:  # noqa: BLE001 - status class only
        return {"ok": False, "status": "unreachable"}
    routes = data.get("routes") if isinstance(data, dict) else None
    routes = [r for r in routes or [] if isinstance(r, dict)]
    ready = sum(1 for r in routes if r.get("availability") == "ready")
    return {"ok": True, "status": "ok", "routes": len(routes), "ready": ready}


async def _test_catalog() -> dict[str, Any]:
    from backend.agents.runner.skillhub import SkillHubClient

    key = kr.privileged_key()
    client = SkillHubClient(key=key or "")
    if not client.configured:
        return {"ok": False, "status": "not_set"}
    try:
        params = {"query": "a", "limit": 1}
        data = await client._request("GET", "/api/skills/catalog", params=params)
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "status": _classify(exc.response.status_code)}
    except Exception:  # noqa: BLE001
        return {"ok": False, "status": "unreachable"}
    total = data.get("total") if isinstance(data, dict) else None
    count = total if isinstance(total, int) else len((data or {}).get("results", []))
    return {"ok": True, "status": "ok", "skills": count}


async def _test_mcp() -> dict[str, Any]:
    from backend.agents.tools.gateway import DEFAULT_OMNIROUTE_URL, OMNIROUTE_URL_ENV
    from backend.agents.tools.mcp_client import McpClient, McpToolError

    key = kr.privileged_key()
    if not key:
        return {"ok": False, "status": "not_set"}
    url = os.environ.get(OMNIROUTE_URL_ENV, "") or DEFAULT_OMNIROUTE_URL
    try:
        await McpClient(url, token=key, timeout_s=10.0).ping()
    except McpToolError as exc:
        return {"ok": False, "status": _classify(exc.reason)}
    return {"ok": True, "status": "ok"}


@router.post("/keys/{kind}/test")
async def test_key(request: Request, kind: str, claims: Claims) -> dict[str, Any]:
    actor = _human(claims)
    kind = _kind(kind)
    store = getattr(request.app.state, "omniroute_key_store", None)
    if kind == kr.REGULAR:
        result: dict[str, Any] = {"key": kind, "inference": await _test_regular()}
        ok = result["inference"]["ok"]
    else:
        catalog, mcp = await asyncio.gather(_test_catalog(), _test_mcp())
        result = {"key": kind, "catalog": catalog, "mcp": mcp}
        ok = catalog["ok"] and mcp["ok"]
    result["ok"] = ok
    if store is not None:
        await store.audit(actor=actor, key=kind, action="test", outcome="ok" if ok else "failed")
    return result
