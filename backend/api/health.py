"""Kubernetes and load-balancer health endpoints.

Additive reporting (existing ``db`` / ``nats`` checks and the HTTP-200
semantics are unchanged): an ``ingest`` section reports the mail-ingest
supervisor liveness (installed?, worker count, per-worker summary) and a
``connections`` section reports durable per-connection status (kind,
auth/operational status, last success/attempt, redacted last error). Both
degrade to an honest ``not configured`` when the supervisor / durable
registry are absent. Credentials and mail content are never included.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


async def _database_check(request: Request) -> dict[str, Any]:
    """Run the configured database probe without failing the HTTP request."""
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        return {"status": "unreachable", "message": "database is not configured"}
    try:
        from backend.persistence.connection import check_health

        report = await check_health(engine)
        result = report.as_dict()
        result["status"] = "ok" if report.ok else "unreachable"
        return result
    except Exception as exc:  # noqa: BLE001 - health must degrade gracefully
        return {"status": "unreachable", "message": str(exc)}


async def _nats_check(request: Request) -> dict[str, Any]:
    """Run the configured NATS probe without failing the HTTP request."""
    client = getattr(request.app.state, "nats_client", None)
    if client is None:
        return {"status": "unreachable", "message": "NATS is not configured"}
    try:
        report = await client.health_check()
        return {
            **report,
            "status": "ok" if report.get("connected") else "unreachable",
        }
    except Exception as exc:  # noqa: BLE001 - health must degrade gracefully
        return {"status": "unreachable", "message": str(exc)}


# -- ingest + connections (additive; the checks above are unchanged) --------


def _ts(value: Any) -> str | None:
    """ISO-8601 string for a timestamp (``None`` passthrough); never raises."""
    try:
        return value.isoformat() if value is not None else None
    except Exception:  # noqa: BLE001 - report, never fail health
        return None


async def _connections_check(registry: Any) -> dict[str, Any]:
    """Durable per-connection status for the health ``connections`` section.

    Fields per row: kind, auth_status, operational_status, last_success_at,
    last_attempt_at, last_error_code, and a redacted (truncated)
    last_error_message. No config payloads, secrets, or message bodies.
    """
    if registry is None:
        return {"status": "not configured", "count": 0, "connections": []}
    try:
        rows = await registry.connections.list_connections()
    except Exception as exc:  # noqa: BLE001 - health must degrade gracefully
        return {"status": "unreachable", "message": str(exc), "count": 0,
                "connections": []}
    entries: list[dict[str, Any]] = []
    for row in rows:
        message = getattr(row, "last_error_message", None)
        entries.append(
            {
                "connection_id": row.id,
                "kind": getattr(row, "kind", None),
                "auth_status": getattr(row, "auth_status", None),
                "operational_status": getattr(row, "operational_status", None),
                "last_success_at": _ts(getattr(row, "last_success_at", None)),
                "last_attempt_at": _ts(getattr(row, "last_attempt_at", None)),
                "last_error_code": getattr(row, "last_error_code", None),
                "last_error_message": (str(message)[:256] if message is not None else None),
            }
        )
    return {"status": "ok", "count": len(entries), "connections": entries}


async def _ingest_check(request: Request) -> dict[str, Any]:
    """Supervisor liveness snapshot for the health ``ingest`` section.

    When the supervisor is installed (even with zero workers) the report is
    ``installed`` with the worker count and a *per-worker* summary
    (liveness: running, failure count, last poll outcome). Per-connection
    durable status — including disabled connections that have no worker —
    is reported in the ``connections`` section, so it is not duplicated
    here. When the supervisor is not installed — plane disabled, no durable
    registry, or no object store — the section is the honest
    ``not configured`` degradation. Never raises.
    """
    supervisor = getattr(request.app.state, "ingest_supervisor", None)
    if supervisor is None:
        return {"status": "not configured", "workers": 0, "summary": []}
    try:
        workers = dict(getattr(supervisor, "workers", None) or {})
        summary = list(supervisor.liveness())
    except Exception as exc:  # noqa: BLE001 - health must degrade gracefully
        return {"status": "error", "message": str(exc), "workers": 0, "summary": []}
    running = sum(1 for w in workers.values() if getattr(w, "is_running", False))
    return {
        "status": "installed",
        "workers": len(workers),
        "workers_running": running,
        "summary": summary,
    }


async def _health(request: Request) -> dict[str, Any]:
    db, nats = await _database_check(request), await _nats_check(request)
    checks = {"db": db, "nats": nats}
    healthy = all(check.get("status") == "ok" for check in checks.values())
    registry = getattr(request.app.state, "repository_registry", None)
    return {
        "status": "ok" if healthy else "degraded",
        "version": request.app.version,
        "checks": checks,
        "ingest": await _ingest_check(request),
        "connections": await _connections_check(registry),
    }


@router.get("/api/health")
async def api_health(request: Request) -> dict[str, Any]:
    """Return a dependency-tolerant health report for Kubernetes probes."""
    return await _health(request)


@router.get("/api/v1/health")
async def versioned_api_health(request: Request) -> dict[str, Any]:
    """Compatibility alias for clients using the versioned API prefix."""
    return await _health(request)
