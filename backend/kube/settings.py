"""Kube agent settings and overrides (NS-ADR-120).

``KubeSettingsStore`` holds the single settings row; ``KubeOverrideStore`` the owner's
time-limited permissions to change one protected namespace for one issue.
``classify_namespace`` decides how a namespace may be treated.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from backend.persistence.models.kube import KubeOverride, KubeSettings

DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,198}[A-Za-z0-9])?$")
_PATH = re.compile(r"^/[A-Za-z0-9._/-]{0,250}$")
MAX_LIST = 100
_INT_RANGES = {
    "override_hours": (1, 168), "retention_daily": (1, 60), "retention_weekly": (0, 52),
    "daily_run_cap": (1, 50), "issue_cooldown_hours": (0, 168),
}
_LISTS = ("protected_namespaces", "known_down")
_TOUCH = ("last_check_at", "last_waste_at")


class KubeSettingsError(ValueError):
    pass


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _row_dict(row: KubeSettings) -> dict[str, Any]:
    return {
        "protected_namespaces": list(row.protected_namespaces or []),
        "known_down": list(row.known_down or []),
        "override_hours": row.override_hours, "retention_daily": row.retention_daily,
        "retention_weekly": row.retention_weekly, "backup_nfs_server": row.backup_nfs_server,
        "backup_nfs_base": row.backup_nfs_base, "daily_run_cap": row.daily_run_cap,
        "issue_cooldown_hours": row.issue_cooldown_hours,
        "last_check_at": _utc(row.last_check_at), "last_waste_at": _utc(row.last_waste_at),
        "updated_by": row.updated_by, "updated_at": _utc(row.updated_at),
    }


def settings_view(data: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe copy (datetimes as ISO strings)."""
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in data.items()}


def _clean_namespaces(field: str, value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise KubeSettingsError(f"{field}: a list of at most {MAX_LIST} namespaces")
    out: list[str] = []
    for ns in value:
        if not isinstance(ns, str) or not DNS_LABEL.match(ns):
            raise KubeSettingsError(f"{field}: {str(ns)[:40]!r} is not a namespace name")
        if ns not in out:
            out.append(ns)
    return out


def validate_patch(patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise KubeSettingsError("settings must be an object")
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key in _LISTS:
            out[key] = _clean_namespaces(key, value)
        elif key in _INT_RANGES:
            lo, hi = _INT_RANGES[key]
            if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
                raise KubeSettingsError(f"{key}: an integer from {lo} to {hi}")
            out[key] = value
        elif key == "backup_nfs_server":
            if not isinstance(value, str) or not _HOST.match(value):
                raise KubeSettingsError("backup_nfs_server: a host name or address")
            out[key] = value
        elif key == "backup_nfs_base":
            if not isinstance(value, str) or not _PATH.match(value) or ".." in value:
                raise KubeSettingsError("backup_nfs_base: an absolute path")
            out[key] = value.rstrip("/") or "/"
        else:
            raise KubeSettingsError(f"unknown setting {str(key)[:40]!r}")
    return out


class KubeSettingsStore:
    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def _row(self, s: Any) -> KubeSettings:
        row = await s.get(KubeSettings, "default")
        if row is None:
            row = KubeSettings(id="default")
            s.add(row)
            await s.flush()
            await s.refresh(row)
        return row

    async def get(self) -> dict[str, Any]:
        async with self._sessions() as s:
            row = await self._row(s)
            await s.commit()
            return _row_dict(row)

    async def update(self, patch: Any, actor: str) -> dict[str, Any]:
        clean = validate_patch(patch)
        async with self._sessions() as s:
            row = await self._row(s)
            for key, value in clean.items():
                setattr(row, key, value)
            row.updated_by, row.updated_at = (actor or "")[:200], datetime.now(UTC)
            await s.commit()
            await s.refresh(row)
            return _row_dict(row)

    async def touch(self, field: str, when: datetime) -> None:
        """Record ``last_check_at`` or ``last_waste_at`` (used by the monitor)."""
        if field not in _TOUCH:
            raise KubeSettingsError("unknown timestamp")
        async with self._sessions() as s:
            setattr(await self._row(s), field, when)
            await s.commit()


def override_dict(row: KubeOverride) -> dict[str, Any]:
    return {
        "id": row.id, "issue_key": row.issue_key, "namespace": row.namespace,
        "created_by": row.created_by, "created_at": _utc(row.created_at),
        "expires_at": _utc(row.expires_at), "revoked_at": _utc(row.revoked_at),
    }


def override_is_active(ov: dict[str, Any], now: datetime) -> bool:
    expires = _utc(ov.get("expires_at"))
    return ov.get("revoked_at") is None and expires is not None and expires > now


class KubeOverrideStore:
    def __init__(self, session_factory: Any, settings: KubeSettingsStore | None = None) -> None:
        self._sessions = session_factory
        self._settings = settings or KubeSettingsStore(session_factory)

    async def create(self, issue_key: str, namespace: str, actor: str,
                     now: datetime | None = None) -> dict[str, Any]:
        if not isinstance(issue_key, str) or not 1 <= len(issue_key) <= 300:
            raise KubeSettingsError("issue_key: 1-300 characters")
        if not isinstance(namespace, str) or not DNS_LABEL.match(namespace):
            raise KubeSettingsError("namespace: not a namespace name")
        now = now or datetime.now(UTC)
        hours = (await self._settings.get())["override_hours"]
        row = KubeOverride(issue_key=issue_key, namespace=namespace, created_by=actor[:200],
                           created_at=now, expires_at=now + timedelta(hours=hours))
        async with self._sessions() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
        return override_dict(row)

    async def revoke(self, override_id: str, now: datetime | None = None) -> None:
        async with self._sessions() as s:
            row = await s.get(KubeOverride, override_id)
            if row is None:
                raise KeyError(override_id)
            row.revoked_at = now or datetime.now(UTC)
            await s.commit()

    async def active(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Overrides that are not revoked and not expired."""
        now = now or datetime.now(UTC)
        async with self._sessions() as s:
            rows = (await s.execute(select(KubeOverride))).scalars().all()
        return [d for d in map(override_dict, rows) if override_is_active(d, now)]

    async def is_active(self, issue_key: str, namespace: str,
                        now: datetime | None = None) -> bool:
        """Per issue and namespace (widen here to allow a namespace-wide override)."""
        return any(o["issue_key"] == issue_key and o["namespace"] == namespace
                   for o in await self.active(now))


def classify_namespace(ns: str, settings: dict[str, Any], overrides: Any = (),
                       issue_key: str | None = None) -> str:
    """``known_down``, ``protected`` (diagnose only), ``override_active`` or ``normal``.

    ``overrides`` are active override dicts; when ``issue_key`` is given only that issue's count.
    """
    if ns in (settings.get("known_down") or []):
        return "known_down"
    if ns in (settings.get("protected_namespaces") or []):
        for ov in overrides or ():
            if ov.get("namespace") == ns and issue_key in (None, ov.get("issue_key")):
                return "override_active"
        return "protected"
    return "normal"
