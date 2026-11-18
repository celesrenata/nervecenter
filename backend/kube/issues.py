"""Issue ledger and report retention for the kube agent (NS-ADR-120)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from backend.kube.collector import scope_unread, snapshot_coverage, snapshot_problems
from backend.kube.settings import _utc, classify_namespace
from backend.persistence.models.kube import KubeIssue, KubeReport

KEEP_REPORTS = {"snapshot": 24, "waste": 14, "settings": 100}
_MODES = {"known_down": "known_down", "protected": "diagnose_only",
          "override_active": "normal", "normal": "normal"}
_EVIDENCE_SKIP = {"key", "kind", "namespace", "name", "reason", "severity"}


def issue_dict(row: KubeIssue) -> dict[str, Any]:
    return {
        "key": row.key, "namespace": row.namespace, "kind": row.kind, "name": row.name,
        "reason": row.reason, "severity": row.severity, "mode": row.mode, "state": row.state,
        "first_seen": _utc(row.first_seen), "last_seen": _utc(row.last_seen),
        "resolved_at": _utc(row.resolved_at), "last_agent_run_at": _utc(row.last_agent_run_at),
        "evidence": row.evidence or {},
    }


class IssueStore:
    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def sync(self, snapshot: dict[str, Any], settings: dict[str, Any], now: datetime,
                   overrides: Any = ()) -> dict[str, list[dict[str, Any]]]:
        """Upsert open issues, resolve vanished ones.

        ``new``: key was not open at the previous sync. Known-down issues are stored with mode
        ``known_down`` and reported only under ``known_down`` (never as new or open failures).
        """
        found = {p["key"]: p for p in snapshot_problems(snapshot)}
        new: list[dict[str, Any]] = []
        open_: list[dict[str, Any]] = []
        known_down: list[dict[str, Any]] = []
        resolved: list[dict[str, Any]] = []
        async with self._sessions() as s:
            rows = {r.key: r for r in (await s.execute(select(KubeIssue))).scalars()}
            for key, p in found.items():
                mode = _MODES[classify_namespace(p["namespace"], settings, overrides, key)]
                evidence = {k: v for k, v in p.items() if k not in _EVIDENCE_SKIP}
                row = rows.get(key)
                was_open = row is not None and row.state == "open"
                if row is None:
                    row = KubeIssue(key=key, first_seen=now)
                    s.add(row)
                elif not was_open:
                    row.first_seen, row.resolved_at = now, None
                row.namespace, row.kind, row.name = p["namespace"], p["kind"], p["name"]
                row.reason, row.severity = p["reason"], p["severity"]
                row.mode, row.state, row.last_seen, row.evidence = mode, "open", now, evidence
                view = issue_dict(row)
                if mode == "known_down":
                    known_down.append(view)
                else:
                    open_.append(view)
                    if not was_open:
                        new.append(view)
            cov = snapshot_coverage(snapshot)
            for key, row in rows.items():
                if scope_unread(row.kind, row.namespace, cov):
                    continue  # not seen is not fixed: this check could not read that scope
                if key not in found and row.state == "open":
                    row.state, row.resolved_at = "resolved", now
                    resolved.append(issue_dict(row))
            await s.commit()
        return {"new": new, "resolved": resolved, "open": open_, "known_down": known_down}

    async def list(self, state: str | None = "open") -> list[dict[str, Any]]:
        async with self._sessions() as s:
            q = select(KubeIssue).order_by(KubeIssue.last_seen.desc())
            if state:
                q = q.where(KubeIssue.state == state)
            return [issue_dict(r) for r in (await s.execute(q)).scalars()]

    async def mark_agent_run(self, key: str, when: datetime | None = None) -> None:
        async with self._sessions() as s:
            row = (await s.execute(select(KubeIssue).where(KubeIssue.key == key))).scalars().first()
            if row is not None:
                row.last_agent_run_at = when or datetime.now(UTC)
                await s.commit()


class ReportStore:
    """Snapshots and waste reports; only the newest 24 and 14 are kept."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def add(self, kind: str, data: dict[str, Any], now: datetime | None = None
                  ) -> dict[str, Any]:
        if kind not in KEEP_REPORTS:
            raise ValueError("kind must be snapshot, waste or settings")
        async with self._sessions() as s:
            row = KubeReport(kind=kind, data=data, created_at=now or datetime.now(UTC))
            s.add(row)
            await s.flush()
            old = (await s.execute(
                select(KubeReport.id).where(KubeReport.kind == kind)
                .order_by(KubeReport.created_at.desc()).offset(KEEP_REPORTS[kind])
            )).scalars().all()
            if old:
                await s.execute(delete(KubeReport).where(KubeReport.id.in_(old)))
            await s.commit()
            return {"id": row.id, "kind": kind, "created_at": _utc(row.created_at), "data": data}

    async def recent(self, kind: str, limit: int = 10) -> list[dict[str, Any]]:
        async with self._sessions() as s:
            rows = (await s.execute(
                select(KubeReport).where(KubeReport.kind == kind)
                .order_by(KubeReport.created_at.desc()).limit(limit))).scalars().all()
        return [{"created_at": _utc(r.created_at), "data": r.data} for r in rows]

    async def latest(self, kind: str) -> dict[str, Any] | None:
        async with self._sessions() as s:
            row = (await s.execute(
                select(KubeReport).where(KubeReport.kind == kind)
                .order_by(KubeReport.created_at.desc()).limit(1))).scalars().first()
            if row is None:
                return None
            return {"id": row.id, "kind": kind, "created_at": _utc(row.created_at),
                    "data": row.data}
