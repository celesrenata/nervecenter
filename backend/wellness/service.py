"""Wellness service (Personal-Ops Recovery §10 — workstream I).

``WellnessService`` fronts the private Wellness Journal: per-day check-in
upsert, windowed listing, deterministic trends, enable/pause control,
hard-delete, and the explicit clinician export with audit logging.

Privacy contract (normative, spec §10)
--------------------------------------
- **Local inference by default; no external-model path at all.** Wellness
  rows are never sent to inference, OmniRoute, or any cloud transport.
  Trends are computed deterministically by :class:`backend.wellness.trends.TrendEngine`
  (statistics only, whitelisted data-describing vocabulary). Unlike
  mail/calendar — which may route through the PDP — wellness data has no
  external-model path, period.
- **User-controlled:** ``wellness.enabled=false`` (durable
  ``preferences`` table) stops collection while keeping data; a
  ``wellness.paused_until`` window pauses it temporarily; ``DELETE
  /api/wellness/entries`` hard-deletes every entry.
- **Sensitive at rest:** every row is the personal-plane sensitivity
  label (:data:`~backend.persistence.models.wellness.WELLNESS_SENSITIVITY`)
  — applied in :func:`WellnessService.entry_dict` on every read.
- **No Today provider:** wellness deliberately does not register a Today
  provider (see the endpoint module docstring) — privacy isolation:
  wellness never surfaces as an action item.

The workstream-H sleep seam
---------------------------
Sleep duration/consistency *consumes* H's ``/api/health/sleep`` data. To
avoid any module-import coupling to a plane that may not exist in a given
deployment (and to keep the import graph audit clean), the sleep records
arrive through an injectable *seam*: ``sleep_provider`` is an optional
async callable ``(days) -> list[dict]`` returning records shaped like
``{bed_at, wake_at, duration_minutes, source}``. The endpoint resolves it
from ``app.state.wellness_sleep_provider`` **at request time**; when it is
absent the sleep sections degrade gracefully (empty / "no sleep data").
Tests inject a scripted provider directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from backend.persistence.models.wellness import WELLNESS_SENSITIVITY
from backend.persistence.repositories.wellness_repo import (
    EXPORT_AUDIT_CODE,
    PREF_ENABLED,
    PREF_PAUSED_UNTIL,
    WellnessRepository,
)

from .trends import TrendEngine

#: An async callable returning H's sleep records for a window:
#: ``(days) -> list[{"bed_at", "wake_at", "duration_minutes", "source"}]``.
SleepProvider = Callable[[int], Awaitable[list[dict[str, Any]] | None]]

#: The one-line privacy statement surfaced with every export/status — the
#: data stays in NerveCenter and is never sent to other services or models.
PRIVACY_STATEMENT = (
    "Wellness data stays in NerveCenter and is never sent to other services or models."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


class WellnessService:
    """Application service for the private wellness plane."""

    def __init__(
        self,
        repo: WellnessRepository,
        *,
        sleep_provider: SleepProvider | None = None,
    ) -> None:
        self._repo = repo
        self._sleep_provider = sleep_provider
        self._engine = TrendEngine()

    # -- status / control ------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        """``GET /api/wellness/status`` — {enabled, paused_until, entry_count,
        last_entry_at, sensitivity, privacy_statement}."""
        enabled = await self._pref_bool(PREF_ENABLED, default=True)
        paused_until = await self._repo.get_preference(PREF_PAUSED_UNTIL)
        paused_until_iso: str | None = None
        if isinstance(paused_until, dict) and paused_until.get("iso"):
            paused_until_iso = str(paused_until["iso"])
        return {
            "enabled": enabled,
            "paused_until": paused_until_iso,
            "entry_count": await self._repo.count_entries(),
            "last_entry_at": _iso(await self._repo.last_entry_at()),
            "sensitivity": WELLNESS_SENSITIVITY,
            "privacy_statement": PRIVACY_STATEMENT,
        }

    async def set_status(
        self,
        *,
        enabled: bool | None = None,
        paused_until: str | None = None,
        clear_pause: bool = False,
    ) -> dict[str, Any]:
        """``PATCH /api/wellness/status`` — persist the control flags.

        ``paused_until`` is an ISO-8601 instant to (re)start the pause
        window; ``clear_pause`` (or re-enabling tracking) removes it.
        Unchanged fields are left as-is.
        """
        if enabled is not None:
            await self._repo.set_preference(PREF_ENABLED, {"enabled": enabled})
            if enabled:
                # re-enabling resumes collection → clear any pause window
                await self._repo.set_preference(PREF_PAUSED_UNTIL, {"iso": None})
        if paused_until is not None:
            await self._repo.set_preference(PREF_PAUSED_UNTIL, {"iso": paused_until})
        elif clear_pause or enabled is True:
            await self._repo.set_preference(PREF_PAUSED_UNTIL, {"iso": None})
        return await self.status()

    async def collection_allowed(self) -> tuple[bool, str | None]:
        """Whether a new check-in may be recorded (enabled and not paused)."""
        if not await self._pref_bool(PREF_ENABLED, default=True):
            return False, "wellness tracking is disabled"
        paused_until = await self._repo.get_preference(PREF_PAUSED_UNTIL)
        if isinstance(paused_until, dict) and paused_until.get("iso"):
            try:
                until = datetime.fromisoformat(str(paused_until["iso"]).replace("Z", "+00:00"))
            except ValueError:
                until = None
            if until is not None and until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until is not None and until > _utcnow():
                return False, f"wellness tracking is paused until {until.isoformat()}"
        return True, None

    # -- entries ---------------------------------------------------------------

    async def entry_dict(self, row: Any) -> dict[str, Any]:
        """Shape a row for the API, with the sensitivity label at rest."""
        return {
            "id": row.id,
            "at": _iso(row.at),
            "mood": row.mood,
            "sleep_minutes": row.sleep_minutes,
            "sleep_consistency": row.sleep_consistency,
            "energy": row.energy,
            "stress": row.stress,
            "journal_entry": row.journal_entry,
            "observations": row.observations,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
            "sensitivity": WELLNESS_SENSITIVITY,
        }

    async def upsert_checkin(
        self,
        *,
        at: datetime,
        mood: int | None = None,
        sleep_minutes: int | None = None,
        sleep_consistency: dict[str, Any] | None = None,
        energy: int | None = None,
        stress: int | None = None,
        journal_entry: str | None = None,
        observations: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Per-day check-in upsert (one row per day, updates in place)."""
        allowed, reason = await self.collection_allowed()
        if not allowed:
            raise CollectionBlockedError(reason or "wellness tracking is off")
        row, created = await self._repo.upsert_by_day(
            at=at,
            mood=mood,
            sleep_minutes=sleep_minutes,
            sleep_consistency=sleep_consistency,
            energy=energy,
            stress=stress,
            journal_entry=journal_entry,
            observations=observations,
        )
        return await self.entry_dict(row), created

    async def list_entries(
        self,
        *,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> list[dict[str, Any]]:
        rows = await self._repo.list_entries(from_dt=from_dt, to_dt=to_dt)
        return [await self.entry_dict(r) for r in rows]

    async def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        row = await self._repo.get_entry(entry_id)
        return await self.entry_dict(row) if row else None

    async def update_entry(
        self,
        *,
        entry_id: str,
        mood: int | None = None,
        sleep_minutes: int | None = None,
        sleep_consistency: dict[str, Any] | None = None,
        energy: int | None = None,
        stress: int | None = None,
        journal_entry: str | None = None,
        observations: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        row = await self._repo.update_entry(
            entry_id=entry_id,
            mood=mood,
            sleep_minutes=sleep_minutes,
            sleep_consistency=sleep_consistency,
            energy=energy,
            stress=stress,
            journal_entry=journal_entry,
            observations=observations,
        )
        return await self.entry_dict(row) if row else None

    async def delete_entry(self, entry_id: str) -> bool:
        return await self._repo.delete_entry(entry_id)

    async def delete_all(self) -> int:
        """Hard-delete every wellness entry (spec §10)."""
        return await self._repo.delete_all()

    # -- trends ----------------------------------------------------------------

    async def trends(self, *, days: int = 30) -> dict[str, Any]:
        """Deterministic trend payload (statistics only — see TrendEngine)."""
        rows = await self._repo.trend_rows(days=days)
        sleep_records: list[dict[str, Any]] | None = None
        if self._sleep_provider is not None:
            try:
                sleep_records = await self._sleep_provider(days)
            except Exception:  # H's plane is optional — degrade, never fail trends
                sleep_records = None
        result = self._engine.trends(rows=rows, days=days, sleep_records=sleep_records)
        result["sleep_data_present"] = bool(
            any(r.get("sleep_minutes") is not None for r in rows)
            or bool(sleep_records)
        )
        return result

    # -- export ----------------------------------------------------------------

    async def export(
        self,
        *,
        fmt: str,
        from_dt: datetime | None,
        to_dt: datetime | None,
    ) -> dict[str, Any] | None:
        """Clinician export — explicit request only.

        Returns ``None`` when ``wellness.enabled=false`` (the endpoint turns
        that into a 403 — an explicit opt-in gate). On success, writes the
        stable-code audit row (``wellness_export``) and returns the windowed
        raw entries plus the audit id.
        """
        if fmt not in ("csv", "json"):
            raise ValueError("export format must be 'csv' or 'json'")
        if not await self._pref_bool(PREF_ENABLED, default=True):
            return None
        rows = await self._repo.list_entries(from_dt=from_dt, to_dt=to_dt)
        entries = [await self.entry_dict(r) for r in rows]
        window = f"{_iso(from_dt) or '*'}..{_iso(to_dt) or '*'}"
        audit_id = await self._repo.log_export(
            entries=len(entries), window=window, fmt=fmt
        )
        return {
            "format": fmt,
            "entries": len(entries),
            "rows": entries,
            "window": window,
            "audit": {"code": EXPORT_AUDIT_CODE, "id": audit_id},
            "privacy_statement": PRIVACY_STATEMENT,
        }

    # -- helpers ----------------------------------------------------------------

    async def _pref_bool(self, key: str, *, default: bool) -> bool:
        value = await self._repo.get_preference(key)
        if value is None:
            return default
        if isinstance(value, dict) and "enabled" in value:
            return bool(value["enabled"])
        return default


class CollectionBlockedError(RuntimeError):
    """Raised when a check-in is submitted while tracking is disabled/paused."""
