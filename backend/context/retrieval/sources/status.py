"""Status for Ask: open incidents, NerveShield and mail-security status, systems, Kubernetes.

Counts and one-line summaries only, read through the same service summaries the screens show:
``IncidentService.list_incidents`` (Incidents), ``SystemsService.list_systems`` (Systems),
``KubeServices`` issue ledger (Kubernetes), and the mail-security health view and gate-hold
counts (Email / NerveShield). No incident description, message, address, or assessment body
is read. The internal stores (``mail_security``, ``mail_evidence``, ``mail_intel``, and the
rest of ``EXCLUDED_STORES``) are never queried directly. A source that is not wired, or that
fails, is skipped; the adapter returns None only when no source is wired at all.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.context.retrieval.intent import Intent
from backend.context.retrieval.sources._util import make_all
from backend.context.retrieval.types import Record

log = structlog.get_logger(__name__)

_CLOSED = {"resolved", "closed"}
_MAX_INCIDENTS = 20


def _counts_text(counter: Counter[str]) -> str:
    return ", ".join(f"{n} {k}" for k, n in sorted(counter.items())) or "none"


async def _incidents(state: Any, now: datetime) -> list[tuple[Any, ...]] | None:
    services = getattr(state, "services", None)
    svc = services.get("incidents") if isinstance(services, dict) else None
    if svc is None:
        engine = getattr(state, "db_engine", None)
        if engine is None:
            return None
        from backend.services.incident_agent import IncidentService
        svc = IncidentService(engine)
    rows = [r for r in await svc.list_incidents()
            if str(r.get("status") or "").lower() not in _CLOSED]
    sev = Counter(str(r.get("severity") or "unknown") for r in rows)
    specs: list[tuple[Any, ...]] = [
        ("status", "incidents:open", "Open incidents",
         f"{len(rows)} open ({_counts_text(sev)})", now)]
    for r in rows[:_MAX_INCIDENTS]:
        specs.append(("status", f"incident:{r.get('id')}", f"Incident: {r.get('title')}",
                      f"{r.get('severity')} {r.get('status')}",
                      r.get("updatedAt") or r.get("createdAt")))
    return specs


async def _systems(state: Any, now: datetime) -> list[tuple[Any, ...]] | None:
    services = getattr(state, "services", None)
    svc = services.get("systems") if isinstance(services, dict) else None
    if svc is None:
        return None
    rows = await svc.list_systems()
    states = Counter(str(r.get("status") or "unknown") for r in rows)
    specs: list[tuple[Any, ...]] = [
        ("status", "systems:summary", "Systems health",
         f"{len(rows)} systems: {_counts_text(states)}", now)]
    for r in rows:
        specs.append(("status", f"system:{r.get('name')}", f"System: {r.get('name')}",
                      f"{r.get('status')}, {r.get('message') or ''}".rstrip(", "),
                      r.get("checked_at") or r.get("checkedAt") or now))
    return specs


async def _kube(state: Any, now: datetime) -> list[tuple[Any, ...]] | None:
    svc = getattr(state, "kube_services", None)
    if svc is None:
        registry = getattr(state, "repository_registry", None)
        factory = getattr(registry, "session_factory", None)
        if factory is None:
            return None
        from backend.kube.store import KubeServices
        svc = KubeServices(factory)
    from backend.kube.settings import classify_namespace
    settings = await svc.settings.get()
    overrides = await svc.overrides.active(now)
    modes: Counter[str] = Counter()
    for issue in await svc.issues.list("open"):
        kind = classify_namespace(issue["namespace"], settings, overrides, issue["key"])
        modes[{"known_down": "known down", "protected": "diagnose only"}.get(kind, "open")] += 1
    total = sum(modes.values())
    return [("status", "kube:summary", "Kubernetes status",
             f"{total} issues ({_counts_text(modes)}), {len(overrides)} active overrides", now)]


async def _mail_security(state: Any, now: datetime) -> list[tuple[Any, ...]] | None:
    """Per-stage health and gate-hold counts for the enabled mail connections (ids only)."""
    registry = getattr(state, "repository_registry", None)
    factory = getattr(registry, "session_factory", None)
    if registry is None or factory is None:
        return None
    from backend.mail_security import observability as obs
    from backend.mail_security.backfill import _SCOPE_RE, _supported_kinds
    from backend.persistence.repositories.mail_security_ops_repo import load_ops_counts
    ids = await registry.connections.list_enabled_ids_for_kinds(set(_supported_kinds()))
    scopes = [f"conn:{i}" for i in ids if _SCOPE_RE.fullmatch(f"conn:{i}")]
    if not scopes:
        return None
    feed = await obs.collect_feed_rows(factory) if obs.mail_security_enabled() else None
    snap = await obs.collect_snapshot(registry.mail_security, scopes, now=now, feed_rows=feed)
    view = obs.health_view(snap)
    stages = Counter(str(s["status"]) for s in view["stages"])
    depth = sum(int(v) for v in (view["queue"]["depth"] or {}).values())
    holds = (await load_ops_counts(factory, scopes)).get("gate_holds") or {}
    held = ", ".join(f"{n} {k}" for k, n in sorted(holds.items())) or "none"
    return [("status", "mail-security:summary", "Mail security status",
             f"stages: {_counts_text(stages)}; {depth} queued; gate holds: {held}", now)]


async def status(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    now = intent.now or datetime.now(UTC)
    specs: list[tuple[Any, ...]] = []
    wired = False
    for name, load in (("incidents", _incidents), ("systems", _systems), ("kube", _kube),
                       ("mail_security", _mail_security)):
        try:
            got = await load(state, now)
        except Exception:  # noqa: BLE001 - one unavailable source must not hide the others
            log.warning("ask.status_source_failed", source=name)
            continue
        if got is not None:
            wired = True
            specs.extend(got)
    if not wired:
        return None
    return await make_all(specs[:limit])
