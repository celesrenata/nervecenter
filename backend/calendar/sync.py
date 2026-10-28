"""Per-account calendar sync runner (Personal-Ops Recovery §8 — workstream E).

:func:`sync_account` drives one sync pass for a connected calendar account:

1. resolve the account row and its credential from the connector secrets
   vault (workstream B — ``upsert_secret``/``get_secret`` on the connections
   repository; the vault module in :mod:`backend.connectors.vault` is the
   codec boundary),
2. build the right provider with an injectable :class:`HttpTransport` (tests
   script the transport; production uses :class:`HttpxTransport`),
3. fetch the window of events (Google: sync-token incremental; CalDAV: etag
   incremental; Apple: inbound relayed events only — no outbound network),
4. **bridge both planes** — every ingested event is (a) upserted into the
   durable ``calendar_events`` row by ``(account_id, ical_uid)`` and (b)
   fed to the in-memory :class:`CalendarService.ingest` so the
   availability/conflict endpoints keep seeing it. Durable rows are the
   source workstream L's Today endpoint queries (row 7).
5. handle explicit deletion markers (provider ``deleted`` / ``STATUS:CANCELLED``)
   by *removing the in-memory copy* while keeping the durable row marked
   ``deleted=True`` — events are never silently dropped,
6. persist the per-account sync cursor (sync tokens / etags) in
   ``calendar_accounts.sync_state`` and record liveness.

A lightweight supervisor (:func:`install_calendar_sync`) polls every enabled
account on an interval, guarded exactly like workstream C's mail supervisor
(``NERVE_CALENDAR_SYNC_ENABLED`` off by default; no durable registry → not
installed; startup never raises).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from backend.calendar.providers import (
    DEFAULT_BACK_WINDOW,
    DEFAULT_FORE_WINDOW,
    ProviderEvent,
    build_provider,
)
from backend.calendar.service import CalendarService
from backend.connectors.redact import redact_message

log = structlog.get_logger(__name__)

#: Environment toggle (mirrors workstream C's ``NERVE_INGEST_ENABLED``):
#: anything but ``"0"`` / empty enables the calendar sync plane.
ENV_CALENDAR_SYNC_ENABLED = "NERVE_CALENDAR_SYNC_ENABLED"

#: State key where the wiring stores the live supervisor.
SUPERVISOR_STATE_KEY = "calendar_sync_supervisor"

#: Vault secret kind / owner for calendar credentials.
SECRET_KIND = "calendar_credential"
SECRET_OWNER_KIND = "connection"


def calendar_sync_enabled(env: dict[str, str] | None = None) -> bool:
    """Return ``True`` when the calendar sync plane is enabled (anything but
    ``"0"`` / empty enables — same contract as the mail ingest toggle)."""
    source = dict(env) if env is not None else dict(os.environ)
    return source.get(ENV_CALENDAR_SYNC_ENABLED, "").strip() not in ("", "0")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class HttpxTransport:
    """Production :class:`HttpTransport` backed by ``httpx.AsyncClient``.

    Never constructed in tests (the scripted fakes are injected instead), so
    the house gate runs without real network.
    """

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> Any:
        import httpx

        from backend.calendar.providers import HttpResponse

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                content=body,
            )
            return HttpResponse(resp.status_code, dict(resp.headers), resp.content)


# ---------------------------------------------------------------------------
# Dual-plane ingest bridge
# ---------------------------------------------------------------------------
def event_ical_id(account_id: str, ical_uid: str) -> str:
    """Stable in-memory service id for a durable event.

    Scoped by account so two CalDAV accounts that happen to share a
    provider ``UID`` never clobber each other in the in-memory store.
    """
    return f"{account_id}:{ical_uid}"


def to_ingest_payload(ev: ProviderEvent, *, account_id: str) -> dict[str, Any]:
    """Shape a :class:`ProviderEvent` into the payload
    :meth:`CalendarService.ingest` accepts (the in-memory ``CalendarEvent``
    shape, ISO-8601 tz-bearing strings)."""

    def iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.isoformat() if value.tzinfo else value.replace(tzinfo=UTC).isoformat()

    extra = ev.extra or {}
    return {
        "event_id": event_ical_id(account_id, ev.ical_uid),
        "title": ev.title,
        "description": ev.description,
        "start": iso(ev.start) if not ev.all_day else None,
        "end": iso(ev.end) if not ev.all_day else None,
        "all_day": ev.all_day,
        "location": ev.location,
        "participants": extra.get("attendees") or [],
        "source": f"calendar-sync:{ev.timezone or 'utc'}",
        "sync_id": ev.ical_uid,
    }


# ---------------------------------------------------------------------------
# Sync runner
# ---------------------------------------------------------------------------
class CalendarSyncRunner:
    """Runs per-account sync passes and bridges both calendar planes."""

    def __init__(
        self,
        registry: Any,
        *,
        calendar_service: CalendarService,
        transport: Any | None = None,
        back_window: timedelta = DEFAULT_BACK_WINDOW,
        fore_window: timedelta = DEFAULT_FORE_WINDOW,
    ) -> None:
        #: The durable :class:`RepositoryRegistry` (workstream F).
        self.registry = registry
        #: In-memory availability service (never replaced, only fed).
        self.service = calendar_service
        #: Injectable transport seam (scripted in tests).
        self.transport = transport
        self.back_window = back_window
        self.fore_window = fore_window

    # -- credential resolution (vault, workstream B) -------------------------
    async def _resolve_secret(self, account: Any) -> str | None:
        """Pull the credential plaintext from the secrets vault for the
        account. ``secret_ref`` addresses the vault row; absent/undecryptable
        → ``None`` (the provider then reports ``no_credential``)."""
        if not getattr(account, "secret_ref", None):
            return None
        try:
            return await self.registry.connections.get_secret(
                kind=SECRET_KIND,
                owner_kind=SECRET_OWNER_KIND,
                owner_id=account.id,
            )
        except Exception as exc:  # noqa: BLE001 — vault failure is a sync error
            log.warning("calendar.sync.secret_unreadable", error=redact_message(exc))
            return None

    # -- per-account pass ----------------------------------------------------
    async def sync_account(self, account_id: str) -> dict[str, Any]:
        """Run one sync pass for ``account_id``. Returns a small summary;
        never raises (errors are recorded on the durable row)."""

        now = datetime.now(UTC)
        repo = self.registry.calendar
        account = await repo.get_account(account_id)
        if account is None:
            return {"synced": False, "reason": "account_not_found"}
        if not account.enabled:
            return {"synced": False, "reason": "account_disabled"}

        await repo.mark_attempt(
            account_id,
            operational_status="syncing",
            last_attempt_at=now,
        )

        try:
            secret = await self._resolve_secret(account)
            provider = build_provider(
                account.provider,
                self.transport,
                config=account.config or {},
                secret=secret,
                account_timezone=account.timezone or "UTC",
                sync_state=account.sync_state or {},
            )

            window_start = now - self.back_window
            window_end = now + self.fore_window

            if account.provider == "apple":
                # Authorized relay only: materialize the inbound events the
                # Mac relay already posted via POST /api/calendar/ingest. No
                # outbound network.
                result = await provider.sync(
                    window_start=window_start,
                    window_end=window_end,
                    relayed_events=self._relayed_events(account),
                )
            else:
                result = await provider.sync(
                    window_start=window_start, window_end=window_end
                )

            ingested = 0
            changed = 0
            deleted = 0
            for ev in result.events:
                # ``ingested`` counts events (same unit as ``changed`` /
                # ``deleted``); the dual-plane return value is not an event count.
                await self._ingest_both_planes(account, ev)
                ingested += 1
                if ev.deleted:
                    deleted += 1
                else:
                    changed += 1

            # Persist the sync cursor (per-calendar sync tokens / etags).
            state = dict(account.sync_state or {})
            if result.new_sync_tokens:
                tokens = dict(state.get("sync_tokens") or {})
                tokens.update(result.new_sync_tokens)
                state["sync_tokens"] = tokens
            if result.etags:
                etags = dict(state.get("etags") or {})
                etags.update(result.etags)
                state["etags"] = etags
            state["last_sync_at"] = now.isoformat()
            state["last_calendars"] = result.calendars
            await repo.save_sync_state(account_id, state)

            await repo.mark_attempt(
                account_id,
                operational_status="healthy",
                last_attempt_at=now,
                last_success_at=now,
                last_error_code=None,
                last_error_message=None,
            )
            return {
                "synced": True,
                "account_id": account_id,
                "ingested": ingested,
                "changed": changed,
                "deleted": deleted,
                "calendars": result.calendars,
            }
        except Exception as exc:  # noqa: BLE001 — a flaky account must not kill the loop
            code = getattr(exc, "code", None) or "sync_failed"
            raw_message = getattr(exc, "message", None) or str(exc)
            message = redact_message(raw_message, limit=500)
            log.warning("calendar.sync.account_failed",
                        account_id=account_id, error_code=code, error=message)
            await repo.mark_attempt(
                account_id,
                operational_status="error",
                last_attempt_at=now,
                last_error_code=code,
                last_error_message=message,
            )
            return {
                "synced": False,
                "account_id": account_id,
                "error_code": code,
                "error": message,
            }

    # -- dual-plane ingest ----------------------------------------------------
    async def _ingest_both_planes(self, account: Any, ev: ProviderEvent) -> int:
        """(a) upsert the durable row by ``(account_id, ical_uid)`` and
        (b) feed the in-memory service (idempotent replay). Returns the
        number of planes actually written (0–2)."""

        repo = self.registry.calendar
        payload = to_ingest_payload(ev, account_id=account.id)

        durable_written = False
        try:
            await repo.upsert_event(
                account_id=account.id,
                ical_uid=ev.ical_uid,
                title=ev.title,
                start=self._event_start(ev),
                end=ev.end,
                all_day=ev.all_day,
                location=ev.location,
                description=ev.description,
                timezone=ev.timezone,
                changed_at=ev.changed_at,
                appt=ev.appt,
                deleted=ev.deleted,
                status=ev.status,
                extra=ev.extra or {},
            )
            durable_written = True
        except Exception as exc:  # noqa: BLE001 — keep the in-memory plane live
            log.error("calendar.sync.durable_write_failed", error=redact_message(exc))

        if ev.deleted:
            # Explicit deletion marker: remove the in-memory copy so
            # availability reasoning no longer blocks on it; the durable row
            # keeps the deleted=True audit marker (never silently dropped).
            self.service.delete(event_ical_id(account.id, ev.ical_uid))
        else:
            self.service.ingest(payload)

        return int(durable_written) + 1

    @staticmethod
    def _event_start(ev: ProviderEvent) -> datetime | None:
        """Durable ``start`` for all-day events: local midnight in the
        authored zone (tz-bearing, per the freeze)."""
        if ev.start is not None:
            return ev.start
        if ev.all_day and ev.timezone:
            from backend.calendar.providers import resolve_zone

            return datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0,
                tzinfo=resolve_zone(ev.timezone, "UTC"),
            )
        return None

    def _relayed_events(self, account: Any) -> list[dict[str, Any]]:
        """Collect inbound Apple-relay events from the in-memory service
        (posted via ``POST /api/calendar/ingest`` with a relay ``source``)."""
        sources = set((account.config or {}).get("relay_sources") or ["apple-relay"])
        out: list[dict[str, Any]] = []
        for ev in self.service.list_events(start=None, end=None, limit=1000):
            if ev.source and ev.source in sources:
                out.append(
                    {
                        "event_id": ev.event_id,
                        "ical_uid": ev.event_id,
                        "title": ev.title,
                        "start": ev.start.isoformat() if ev.start else None,
                        "end": ev.end.isoformat() if ev.end else None,
                        "all_day": ev.all_day,
                        "location": ev.location,
                        "description": ev.description,
                        "changed_at": ev.sync_id,
                        "appt": True,
                        "deleted": False,
                        "timezone": ev.source.split(":", 1)[1] if ":" in ev.source else None,
                    }
                )
        return out


# ---------------------------------------------------------------------------
# App-lifespan supervisor (mirrors workstream C's guarded hook)
# ---------------------------------------------------------------------------
class CalendarSyncSupervisor:
    """Polls every enabled calendar account on an interval; one task, serial
    passes (calendar windows are small and the plane is user-scale). A failed
    account is recorded durably and does not stop the loop."""

    def __init__(
        self,
        runner: CalendarSyncRunner,
        *,
        interval_s: float = 60.0,
    ) -> None:
        self.runner = runner
        self.interval_s = interval_s
        self._task: asyncio.Task | None = None
        self._running = False
        self.liveness: dict[str, Any] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="calendar-sync-supervisor")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                accounts = await self.runner.registry.calendar.list_accounts(
                    enabled_only=True
                )
                for account in accounts:
                    self.liveness[account.id] = await self.runner.sync_account(
                        account.id
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the loop must survive
                log.error("calendar.sync.loop_failed", error=redact_message(exc))
            await asyncio.sleep(self.interval_s)


async def install_calendar_sync(target: Any) -> CalendarSyncSupervisor | None:
    """Lifespan hook: install the calendar sync supervisor when
    ``NERVE_CALENDAR_SYNC_ENABLED`` is set and a durable registry exists.
    Returns the supervisor (for tests / health) or ``None``. Never raises."""
    if not calendar_sync_enabled():
        return None
    registry = getattr(target.state, "repository_registry", None)
    if registry is None or not hasattr(registry, "calendar"):
        log.info("calendar.sync.supervisor_skipped", reason="no_durable_registry")
        return None
    service = getattr(target.state, "calendar_service", None)
    if service is None:
        from backend.calendar.service import get_calendar_service

        service = get_calendar_service()
        target.state.calendar_service = service
    transport = getattr(target.state, "calendar_transport", None) or HttpxTransport()
    runner = CalendarSyncRunner(registry, calendar_service=service, transport=transport)
    target.state.calendar_sync_runner = runner
    supervisor = CalendarSyncSupervisor(runner)
    await supervisor.start()
    setattr(target.state, SUPERVISOR_STATE_KEY, supervisor)
    return supervisor


async def stop_calendar_sync(target: Any) -> None:
    """Lifespan hook: stop the supervisor (no-op when not installed)."""
    supervisor = getattr(target.state, SUPERVISOR_STATE_KEY, None)
    if supervisor is not None:
        await supervisor.stop()
    for key in (SUPERVISOR_STATE_KEY, "calendar_sync_runner"):
        if hasattr(target.state, key):
            delattr(target.state, key)


def build_runner(
    registry: Any,
    calendar_service: CalendarService,
    transport: Any | None = None,
) -> CalendarSyncRunner:
    """Convenience constructor for endpoints/tests."""
    return CalendarSyncRunner(registry, calendar_service=calendar_service, transport=transport)


__all__ = [
    "CalendarSyncRunner",
    "CalendarSyncSupervisor",
    "HttpxTransport",
    "install_calendar_sync",
    "stop_calendar_sync",
    "build_runner",
    "calendar_sync_enabled",
    "to_ingest_payload",
    "event_ical_id",
    "SECRET_KIND",
    "SECRET_OWNER_KIND",
    "SUPERVISOR_STATE_KEY",
    "ENV_CALENDAR_SYNC_ENABLED",
]
