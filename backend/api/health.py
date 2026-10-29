"""Kubernetes and load-balancer health endpoints.

Additive reporting (existing ``db`` / ``nats`` checks and the HTTP-200
semantics are unchanged):

- an ``ingest`` section reports the mail-ingest supervisor liveness
  (installed?, worker count, per-worker summary) plus a redacted
  ``reason`` when the plane is gated off (spec P1-A: surface *why*);
- a ``connections`` section reports durable per-connection status
  (kind, auth/operational status, last success/attempt, redacted last
  error) plus a derived P1-A ``operational_tier`` per account;
- an ``object_store`` section reports the object store readiness with
  redacted missing-variable names (spec P1-A subsystem readiness).

Both the ingest and connections sections degrade to an honest
``not configured`` when the supervisor / durable registry are absent.
Credentials and mail content are never included.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from backend.connectors.redact import redact_message

router = APIRouter(tags=["health"])

#: P1-A per-account operational taxonomy (ordered by severity; the health
#: endpoint must never report a source as "healthy"/operational when it is
#: actually stale or disconnected — see P1-A / P1-206).
_AUTH_NOT_CONFIGURED = ("unconfigured",)
_AUTH_REQUIRED = ("pairing_pending", "auth_expired", "auth_failed")
_OPERATIONAL_TIER_BY_STATUS = {
    "healthy": "healthy",
    "syncing": "syncing",
    "never_synced": "authenticated_no_sync",
    "degraded": "degraded",
    "error": "error",
}


def _operational_tier(auth_status: str | None,
                      operational_status: str | None) -> str:
    """Map a durable connection's raw statuses onto the P1-A per-account
    operational taxonomy.

    ``operational_tier`` is the value the web "Today" surface (P1-206)
    should display per source: it is never ``healthy`` unless the durable
    row says so, so a stale/disconnected source is never reported as clear.
    """
    auth = auth_status or "unconfigured"
    if auth in _AUTH_NOT_CONFIGURED:
        return "never_configured"
    if auth in _AUTH_REQUIRED:
        return "auth_required"
    # auth good (authenticated): reflect the operational status, defaulting
    # to "authenticated but not yet synced" when no operational state exists.
    op = operational_status or "never_synced"
    return _OPERATIONAL_TIER_BY_STATUS.get(op, "degraded")


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
        return {"status": "unreachable", "message": redact_message(exc)}


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
        return {"status": "unreachable", "message": redact_message(exc)}


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
        return {"status": "not configured", "count": 0, "connections": [],
                "personal_attention_operational": False}
    try:
        rows = await registry.connections.list_connections()
    except Exception as exc:  # noqa: BLE001 - health must degrade gracefully
        return {"status": "unreachable", "message": redact_message(exc), "count": 0,
                "connections": [], "personal_attention_operational": False}
    entries: list[dict[str, Any]] = []
    for row in rows:
        message = getattr(row, "last_error_message", None)
        auth_status = getattr(row, "auth_status", None)
        operational_status = getattr(row, "operational_status", None)
        entries.append(
            {
                "connection_id": row.id,
                "kind": getattr(row, "kind", None),
                "auth_status": auth_status,
                "operational_status": operational_status,
                # P1-A per-account operational taxonomy (never "healthy"
                # unless the durable row is healthy; see P1-206).
                "operational_tier": _operational_tier(auth_status, operational_status),
                "last_success_at": _ts(getattr(row, "last_success_at", None)),
                "last_attempt_at": _ts(getattr(row, "last_attempt_at", None)),
                "last_error_code": getattr(row, "last_error_code", None),
                "last_error_message": (redact_message(message) if message is not None else None),
            }
        )
    # P1-A: the "Today" surface (P1-206) may never say "you're clear" while
    # any mail/calendar source is stale, disconnected, or not yet configured.
    # ``personal_attention_operational`` is True only when at least one
    # connection is operational (healthy/syncing) — i.e. the personal
    # attention projection is backed by a live source.
    tiers = [e["operational_tier"] for e in entries]
    personal_attention_operational = any(
        tier in ("healthy", "syncing") for tier in tiers
    )
    return {
        "status": "ok",
        "count": len(entries),
        "connections": entries,
        "personal_attention_operational": personal_attention_operational,
    }


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
        # The plane is not installed. Surface the *why* (spec P1-A): the
        # install hook stores a redacted gate reason in app state when the
        # ingest plane is deliberately disabled or a dependency is absent.
        # Lazy import: importing backend.ingest pulls the full connector /
        # mail stack, which would create a cycle if loaded at module import.
        from backend.ingest.supervisor import INGEST_GATE_STATE_KEY

        reason = getattr(request.app.state, INGEST_GATE_STATE_KEY, None)
        return {
            "status": "not configured",
            "workers": 0,
            "summary": [],
            "reason": reason
            if reason
            else "ingest plane not installed (NERVE_INGEST_ENABLED not set "
                 "or wiring did not run)",
        }
    try:
        workers = dict(getattr(supervisor, "workers", None) or {})
        summary = list(supervisor.liveness())
    except Exception as exc:  # noqa: BLE001 - health must degrade gracefully
        return {"status": "error", "message": redact_message(exc), "workers": 0,
                "summary": [], "reason": None}
    running = sum(1 for w in workers.values() if getattr(w, "is_running", False))
    return {
        "status": "installed",
        "workers": len(workers),
        "workers_running": running,
        "summary": summary,
        "reason": None,
    }


def _object_store_check() -> dict[str, Any]:
    """Redacted object-store readiness (spec P1-A subsystem readiness).

    Uses the non-raising :func:`~backend.storage.object_store_status` probe so
    a missing endpoint is reported as ``not_configured`` / ``incomplete`` with
    the *names* of the missing ``NERVECENTER_OBJECT_STORE_*`` variables —
    never a value.  Never raises.
    """
    try:
        from backend.storage.config import object_store_status

        status = object_store_status()
        return {
            "status": status.get("status", "unknown"),
            "scheme": status.get("scheme"),
            "unsupported_scheme": bool(status.get("unsupported_scheme")),
            "missing": list(status.get("missing") or ()),
        }
    except Exception:  # noqa: BLE001 - health must never fail on a probe
        return {"status": "unknown", "scheme": None,
                "unsupported_scheme": False, "missing": []}


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
        "object_store": _object_store_check(),
    }


@router.get("/api/health")
async def api_health(request: Request) -> dict[str, Any]:
    """Return a dependency-tolerant health report for Kubernetes probes."""
    return await _health(request)


@router.get("/api/v1/health")
async def versioned_api_health(request: Request) -> dict[str, Any]:
    """Compatibility alias for clients using the versioned API prefix."""
    return await _health(request)
