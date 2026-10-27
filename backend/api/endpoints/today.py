"""Today endpoints — the default landing page (spec §13, workstream L).

Contract (frozen for later waves):

- ``GET  /api/today`` →
    ``{ needs_you, coming_up, handled, low_energy, simplified, generated_at }``

  - ``needs_you`` — top active durable attention items plus open commitments
    past-or-near due (due ≤ 48h out, including overdue), sorted by
    (importance desc, deadline asc — no deadline sorts last), capped at 5.
  - ``coming_up`` — the next 48 hours: durable calendar events (when the
    ``calendar_events`` table created by E's migration 010 is present),
    upcoming attention deadlines, and upcoming commitment deadlines. Bills
    are intentionally ABSENT (Wave 3; no demo records — spec §2 row 14).
  - ``handled`` — attention items resolved/dismissed/snoozed and
    commitments completed within the last 24 hours, capped at 10.
  - There are no streak fields and no "missed" counts anywhere in the
    response shape — Low Energy Mode must never punish (spec §13).
- ``PATCH /api/today/mode`` with body ``{ "low_energy": bool }`` → persists
  the preference to the durable ``preferences`` table under the key
  ``today.low_energy_mode`` (survives restarts) and is reflected in
  ``GET /api/today``.

Low Energy Mode (spec §13): when on, ``needs_you`` holds at most ONE item —
the single most important next action (attention items rank first because
the wave-1 Done/Later actions target the attention endpoints; a commitment
fills the slot only when no attention item is present) — ``coming_up``
keeps deadline items only (capped at 3), and ``simplified: true`` tells the
UI to render the reduced layout with Done / Later / Help Me on that single
item.

House degradation (honest semantics, matching ``/api/connections``):

- no durable registry (no DB configured) → ``GET`` answers 200 with empty
  sections and ``low_energy: false`` (absent preference); ``PATCH /mode``
  answers 202 with ``executed: false``.
- registry present but the database is unreachable → 200 with the sections
  that could be read (each provider isolates its own failures).

Aggregator architecture (freeze): Today is composed of
:class:`TodaySectionProvider` sections — the built-ins are ``attention``,
``commitments`` and ``calendar``. Later waves (G bills, H routines, …) call
:func:`register_today_provider` from their own wiring WITHOUT editing this
file.

Known gap (reported, not hidden): P0 *in-memory* attention items held by
``backend.attention`` (``POST /api/attention`` / dev seed) that are not yet
persisted as durable ``attention_items`` rows do not appear in Today — the
durable table is the source of truth for the landing page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text

from backend.persistence.models.personal import (
    AttentionItemRow,
    CommitmentRow,
    PreferenceRow,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/today", tags=["today"])

#: Durable preference key for Low Energy Mode (spec §13).
LOW_ENERGY_PREFERENCE_KEY = "today.low_energy_mode"

#: Section caps (small by design — the landing page must stay scannable).
NEEDS_YOU_CAP = 5
COMING_UP_CAP = 10
HANDLED_CAP = 10
LOW_ENERGY_COMING_UP_CAP = 3

#: "Next" windows (frozen for this wave; N hours/days per spec §13).
COMING_UP_WINDOW = timedelta(hours=48)
HANDLED_WINDOW = timedelta(hours=24)

#: Urgency ordering used only as a display hint (not part of the sort key).
_URGENCY_RANK = {"low": 0, "normal": 1, "high": 2, "critical": 3}


# ---------------------------------------------------------------------------
# Aggregator architecture (freeze for G/H/I/J)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TodayContext:
    """Everything a section provider needs for one request (frozen shape).

    ``registry`` is the durable :class:`RepositoryRegistry` or ``None`` when
    the API is running without a database (degraded mode). ``now`` is the
    request's UTC clock — providers must use it, never read the clock again.
    """

    registry: Any | None
    now: datetime


@runtime_checkable
class TodaySectionProvider(Protocol):
    """One Today section contributor.

    Later waves register their providers via :func:`register_today_provider`
    from their own wiring; this module is never edited for that. Providers
    return plain dicts shaped like the response items (``id``, ``kind``,
    ``title``, …) and must tolerate ``ctx.registry is None`` (return []).
    """

    async def name(self) -> str:
        """Stable provider id (e.g. ``"attention"``)."""
        ...

    async def needs_you(self, ctx: TodayContext) -> list[dict[str, Any]]:
        """Items that need the user now (active items, overdue/near-due)."""
        ...

    async def coming_up(self, ctx: TodayContext) -> list[dict[str, Any]]:
        """Items arriving in the next 48 hours."""
        ...

    async def handled(self, ctx: TodayContext) -> list[dict[str, Any]]:
        """Items completed/deferred in the last 24 hours."""
        ...


_TODAY_PROVIDERS: list[Any] = []


def register_today_provider(provider: TodaySectionProvider) -> None:
    """Register an additional Today section provider (idempotent).

    G/H/I/J waves call this from their wiring (e.g.
    ``today.register_today_provider(BillsProvider())``).
    """
    if any(p is provider for p in _TODAY_PROVIDERS):
        return
    _TODAY_PROVIDERS.append(provider)


def _providers() -> list[Any]:
    return list(_TODAY_PROVIDERS)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _deadline_ts(dt: datetime | None) -> float:
    """Sort key: no deadline sorts last (+inf)."""
    return dt.timestamp() if dt is not None else float("inf")


def _needs_you_sort(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """(importance desc, deadline asc, id) — frozen needs_you ordering."""
    return sorted(
        items,
        key=lambda it: (
            -(it.get("importance") or 0),
            _deadline_ts(_ensure_utc_iso(it.get("deadline_at"))),
            str(it.get("id")),
        ),
    )


def _ensure_utc_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return _ensure_utc(datetime.fromisoformat(iso))
    except ValueError:
        return None


def _attention_item_dict(row: AttentionItemRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": "attention",
        "title": row.title,
        "body": row.body,
        "importance": row.importance,
        "urgency": row.urgency,
        "deadline_at": _iso(_ensure_utc(row.deadline_at)),
        "source_refs": list(row.source_refs or []),
        "recommended_action": row.recommended_action,
    }


def _commitment_item_dict(
    row: CommitmentRow, *, importance: int | None = None, urgency: str = "normal"
) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": "commitment",
        "title": row.text,
        "body": None,
        # Commitments carry no importance column in migration 006 — null is
        # honest; the aggregator's sort treats null as 0 (lowest rank).
        "importance": importance,
        "urgency": urgency,
        "deadline_at": _iso(_ensure_utc(row.due_at)),
        "source_refs": (
            [f"communication:{row.source_communication_id}"]
            if row.source_communication_id
            else []
        ),
        "recommended_action": None,
    }


# ---------------------------------------------------------------------------
# Built-in providers
# ---------------------------------------------------------------------------


class _AttentionProvider:
    """Durable ``attention_items`` (the wave-1 source of truth)."""

    async def name(self) -> str:
        return "attention"

    async def needs_you(self, ctx: TodayContext) -> list[dict[str, Any]]:
        if ctx.registry is None:
            return []
        rows = await ctx.registry.attention.list_attention_items(state="active")
        return [_attention_item_dict(r) for r in rows]

    async def coming_up(self, ctx: TodayContext) -> list[dict[str, Any]]:
        if ctx.registry is None:
            return []
        rows = await ctx.registry.attention.list_attention_items(state="active")
        out: list[dict[str, Any]] = []
        for row in rows:
            deadline = _ensure_utc(row.deadline_at)
            if deadline is not None and ctx.now < deadline <= ctx.now + COMING_UP_WINDOW:
                out.append(
                    {
                        "id": row.id,
                        "kind": "attention",
                        "title": row.title,
                        "deadline_at": _iso(deadline),
                    }
                )
        return out

    async def handled(self, ctx: TodayContext) -> list[dict[str, Any]]:
        if ctx.registry is None:
            return []
        rows = await ctx.registry.attention.list_attention_items()
        since = ctx.now - HANDLED_WINDOW
        out: list[dict[str, Any]] = []
        for row in rows:
            if row.state in ("resolved", "dismissed"):
                at = _ensure_utc(row.resolved_at)
            elif row.state == "snoozed":
                # The snooze transition stamps state + snoozed_until; without
                # an onupdate trigger the recency signal is updated_at.
                at = _ensure_utc(row.updated_at)
            else:
                continue
            if at is not None and at >= since:
                out.append(
                    {
                        "id": row.id,
                        "kind": "attention",
                        "title": row.title,
                        "state": row.state,
                        "at": _iso(at),
                    }
                )
        return out


class _CommitmentsProvider:
    """Durable ``commitments`` (F+J plane)."""

    async def name(self) -> str:
        return "commitments"

    async def needs_you(self, ctx: TodayContext) -> list[dict[str, Any]]:
        """Open commitments past-or-near due (overdue or due within 48h)."""
        if ctx.registry is None:
            return []
        rows = await ctx.registry.commitments.list_commitments(state="open")
        out = []
        for row in rows:
            due = _ensure_utc(row.due_at)
            if due is not None and due <= ctx.now + COMING_UP_WINDOW:
                out.append(_commitment_item_dict(row))
        return out

    async def coming_up(self, ctx: TodayContext) -> list[dict[str, Any]]:
        if ctx.registry is None:
            return []
        rows = await ctx.registry.commitments.list_commitments(state="open")
        out = []
        for row in rows:
            due = _ensure_utc(row.due_at)
            if due is not None and ctx.now < due <= ctx.now + COMING_UP_WINDOW:
                out.append(
                    {
                        "id": row.id,
                        "kind": "commitment",
                        "title": row.text,
                        "deadline_at": _iso(due),
                    }
                )
        return out

    async def handled(self, ctx: TodayContext) -> list[dict[str, Any]]:
        """Commitments marked done in the last 24h (recency via updated_at)."""
        if ctx.registry is None:
            return []
        rows = await ctx.registry.commitments.list_commitments(state="done")
        since = ctx.now - HANDLED_WINDOW
        out = []
        for row in rows:
            at = _ensure_utc(row.updated_at)
            if at is not None and at >= since:
                out.append(
                    {
                        "id": row.id,
                        "kind": "commitment",
                        "title": row.text,
                        "state": "done",
                        "at": _iso(at),
                    }
                )
        return out


class _CalendarProvider:
    """Durable ``calendar_events`` (E's migration 010 — not in this snapshot).

    The model class does not exist in this checkout yet, so this provider
    *raw-probes* the table: it introspects the live columns and queries only
    when the table is present. Every failure mode (table missing, columns
    renamed, DB error) degrades to "no calendar items" — the landing page
    must never 500 because of the calendar (spec §8, workstream E lands
    providers in parallel).
    """

    _TITLE_CANDIDATES = ("title", "summary", "name")
    _START_CANDIDATES = ("start_at", "starts_at", "start", "begins_at", "dtstart")
    _END_CANDIDATES = ("end_at", "ends_at", "end", "dtend")

    async def name(self) -> str:
        return "calendar"

    async def _table_columns(self, ctx: TodayContext) -> list[str] | None:
        """Column names of ``calendar_events`` — None when the table is absent."""
        if ctx.registry is None:
            return None
        try:
            async with ctx.registry.session_factory() as session:
                connection = await session.connection()
                columns = await connection.run_sync(
                    lambda sync_conn: [
                        c["name"] for c in sa_inspect(sync_conn).get_columns("calendar_events")
                    ]
                )
            return [str(c) for c in columns]
        except Exception:  # noqa: BLE001 — table absent or DB error → no calendar
            return None

    async def coming_up(self, ctx: TodayContext) -> list[dict[str, Any]]:
        columns = await self._table_columns(ctx)
        if ctx.registry is None or not columns:
            return []
        title_col = next((c for c in self._TITLE_CANDIDATES if c in columns), None)
        start_col = next((c for c in self._START_CANDIDATES if c in columns), None)
        end_col = next((c for c in self._END_CANDIDATES if c in columns), None)
        if title_col is None or start_col is None:
            log.warning("today.calendar_columns_unrecognized", columns=columns)
            return []
        try:
            async with ctx.registry.session_factory() as session:
                # NOTE: E's durable model (migration 010) uses `start`/`end`
                # columns; the alias below is what the out-construction reads,
                # so select the REAL column name and alias it (a literal
                # `end_at` reference would 500-degrade on the real schema).
                stmt = text(
                    f"SELECT id, {title_col} AS title, {start_col} AS start_at, "
                    f"{end_col + ' AS end_at' if end_col else 'NULL AS end_at'} "
                    f"FROM calendar_events "
                    f"WHERE {start_col} >= :lo AND {start_col} <= :hi "
                    f"ORDER BY {start_col} ASC"
                )
                result = await session.execute(
                    stmt, {"lo": ctx.now, "hi": ctx.now + COMING_UP_WINDOW}
                )
                rows = result.mappings().all()
        except Exception as exc:  # noqa: BLE001 — degrade to no calendar items
            log.warning("today.calendar_probe_failed", error=str(exc))
            return []
        out = []
        for row in rows:
            # Postgres decodes timestamptz to datetime; SQLite scratch DBs
            # (the test suite's backend) return ISO strings — handle both.
            start_val = row["start_at"]
            start = (
                _ensure_utc_iso(str(start_val))
                if isinstance(start_val, str)
                else _ensure_utc(start_val)
            )
            if start is None:
                continue
            out.append(
                {
                    "id": str(row["id"]),
                    "kind": "calendar",
                    "title": str(row["title"] or "Calendar event"),
                    "deadline_at": _iso(start),
                }
            )
        return out

    async def needs_you(self, ctx: TodayContext) -> list[dict[str, Any]]:
        return []

    async def handled(self, ctx: TodayContext) -> list[dict[str, Any]]:
        return []


register_today_provider(_AttentionProvider())
register_today_provider(_CommitmentsProvider())
register_today_provider(_CalendarProvider())


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _log_provider_failed(provider: Any, section: str, exc: Exception) -> None:
    log.warning(
        "today.provider_failed",
        provider=provider.__class__.__name__,
        section=section,
        error=str(exc),
    )


async def _collect_sections(ctx: TodayContext) -> tuple[list, list, list]:
    """Run every provider, isolating per-provider failures (log, continue)."""
    needs: list[dict[str, Any]] = []
    coming: list[dict[str, Any]] = []
    handled: list[dict[str, Any]] = []
    for provider in _providers():
        try:
            needs.extend(await provider.needs_you(ctx))
        except Exception as exc:  # noqa: BLE001
            _log_provider_failed(provider, "needs_you", exc)
        try:
            coming.extend(await provider.coming_up(ctx))
        except Exception as exc:  # noqa: BLE001
            _log_provider_failed(provider, "coming_up", exc)
        try:
            handled.extend(await provider.handled(ctx))
        except Exception as exc:  # noqa: BLE001
            _log_provider_failed(provider, "handled", exc)

    # coming_up must not repeat an item already in needs_you (overlapping
    # due windows) — dedup against the pre-truncation set.
    needs_in = {(it.get("kind"), str(it.get("id"))) for it in needs}
    coming = [c for c in coming if (c.get("kind"), str(c.get("id"))) not in needs_in]

    needs = _needs_you_sort(needs)[:NEEDS_YOU_CAP]
    coming = sorted(
        coming, key=lambda c: (c.get("deadline_at") or "9999", str(c.get("id")))
    )[:COMING_UP_CAP]
    handled = sorted(
        handled, key=lambda h: (h.get("at") or "", str(h.get("id"))), reverse=True
    )[:HANDLED_CAP]
    return needs, coming, handled


def _apply_low_energy(needs: list, coming: list) -> tuple[list, list]:
    """Spec §13: at most one next action, deadlines only, simplified layout.

    The single most important item is taken from attention items first — the
    wave-1 Done/Later actions target the attention endpoints — with an
    overall top commitment filling the slot when no attention item exists.
    """
    attention_top = next((it for it in needs if it.get("kind") == "attention"), None)
    top = attention_top if attention_top is not None else (needs[0] if needs else None)
    single = [top] if top is not None else []
    deadlines_only = [c for c in coming if c.get("deadline_at")][:LOW_ENERGY_COMING_UP_CAP]
    return single, deadlines_only


# ---------------------------------------------------------------------------
# Low Energy Mode preference (durable ``preferences`` table)
# ---------------------------------------------------------------------------


async def _read_low_energy(request: Request) -> bool:
    registry = getattr(request.app.state, "repository_registry", None)
    if registry is None:
        return False  # no durable store → absent preference → off
    try:
        async with registry.session_factory() as session:
            row = (
                await session.execute(
                    select(PreferenceRow).where(
                        PreferenceRow.key == LOW_ENERGY_PREFERENCE_KEY
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return False
        return bool((row.value or {}).get("low_energy", False))
    except Exception as exc:  # noqa: BLE001
        log.warning("today.preference_read_failed", error=str(exc))
        return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class LowEnergyModeRequest(BaseModel):
    """Body for ``PATCH /api/today/mode``."""

    low_energy: bool


@router.get("")
@router.get("/")
async def get_today(request: Request) -> dict[str, Any]:
    """The default landing page (spec §13).

    House degradation: without a durable registry this answers 200 with
    empty sections and ``low_energy: false`` — never 503, and never demo data.
    """
    now = datetime.now(UTC)
    ctx = TodayContext(registry=getattr(request.app.state, "repository_registry", None), now=now)
    needs, coming, handled = await _collect_sections(ctx)
    low_energy = await _read_low_energy(request)
    if low_energy:
        needs, coming = _apply_low_energy(needs, coming)
    return {
        "needs_you": needs,
        "coming_up": coming,
        "handled": handled,
        "low_energy": low_energy,
        # Visual-complexity flag for the UI (spec §13 reduced layout). No
        # streak/missed fields exist in this shape by design.
        "simplified": low_energy,
        "generated_at": now.isoformat(),
    }


@router.patch("/mode")
async def set_low_energy_mode(
    request: Request, payload: LowEnergyModeRequest
) -> dict[str, Any]:
    """Persist Low Energy Mode to the durable ``preferences`` table.

    Survives restarts; ``GET /api/today`` reflects it on the next call.
    Without a durable registry this degrades honestly to
    202 ``executed: false`` (Area 4 write contract, as in ``/api/connections``).
    """
    registry = getattr(request.app.state, "repository_registry", None)
    if registry is None:
        return {
            "accepted": True,
            "executed": False,
            "low_energy": False,
            "timestamp": datetime.now(UTC).isoformat(),
            "detail": "durable storage is not configured",
        }
    try:
        async with registry.session_factory() as session:
            row = (
                await session.execute(
                    select(PreferenceRow).where(
                        PreferenceRow.key == LOW_ENERGY_PREFERENCE_KEY
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    PreferenceRow(
                        key=LOW_ENERGY_PREFERENCE_KEY,
                        value={"low_energy": payload.low_energy},
                    )
                )
            else:
                row.value = {"low_energy": payload.low_energy}
                row.updated_at = datetime.now(UTC)
            await session.commit()
        return {"low_energy": payload.low_energy}
    except Exception as exc:  # noqa: BLE001
        log.error("today.preference_write_failed", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail={"error_code": "persistence_unavailable", "message": str(exc)},
        ) from exc
