"""In-memory Follow-up service (R104 — AC-R07, spec §57).

Design notes
------------
- **No persistence yet.** In-memory singleton, house style
  (``attention`` / ``waiting_on`` / ``drafts``): the API and the tests work
  without ``NERVE_DB_URL``. Public method signatures are designed to survive a
  later swap to a real table.
- **Due items are first-class.** A :class:`~backend.followups.models.FollowUp`
  is created from an unanswered outgoing request or an unfulfilled commitment
  (``target_kind`` / ``target_id`` / ``created_from`` name the origin). The
  deterministic :meth:`FollowUpService.evaluate_due` hook is the only thing
  that promotes a due follow-up to the Now screen.
- **One live AttentionItem per live follow-up.** A due OPEN (or SNOOZED)
  follow-up creates — or *refreshes* if one is already live — exactly one
  :class:`~backend.api.models.AttentionItem` through the shared P0
  :class:`~backend.attention.service.AttentionService` scorer, sourced
  ``followups`` with a reference back to the follow-up (§21/§22/§49).
- **Lifecycle events are inspectable.** Each lifecycle step emits a
  :class:`schemas.events.Event` (``followup.created`` /
  ``followup.triggered`` / ``followup.completed`` / ``followup.forgotten`` /
  ``followup.updated``) with a deterministic ``idempotency_key``
  (``<event_type>:<followup_id>``) and is buffered in-process so tests and the
  UI can assert on it without a NATS bus (same convention as
  :class:`backend.communications.detection.service.DetectionService`, which
  buffers its §8 events per report). No network I/O happens here.
- **State machine (AC-R07).** ``OPEN <-> SNOOZED``; ``OPEN|SNOOZED ->
  COMPLETED`` or ``OPEN|SNOOZED -> FORGOTTEN``. Terminal states are
  immutable — any transition out of them raises
  :class:`~backend.followups.models.FollowUpTransitionError`.
- **Attention → follow-up propagation.** A live follow-up remembers its
  ``attention_id``; :meth:`snooze` and :meth:`complete` propagate to that
  item (suppressed / resolved) so "attention snooze/resolve must propagate to
  the live follow-up" holds in both directions.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

from backend.api.models import AttentionState
from backend.attention.service import AttentionService, get_attention_service
from backend.followups.models import (
    FollowUp,
    FollowUpStatus,
    FollowUpTransitionError,
    can_transition,
    make_followup_id,
)
from schemas.events import Event

_EVENT_CATEGORY = "knowledge"  # frozen §10 set has no "personal" bucket
_SOURCE = "followups.engine"

#: Default delay between a follow-up's creation and its first due time
#: (spec R104 step 3: "configurable default timing"). Overridable via
#: ``NERVE_FOLLOWUP_DELAY_HOURS`` (e.g. ``72`` = 3 days).
DEFAULT_FOLLOW_UP_DELAY_HOURS = 72


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def default_follow_up_delay() -> timedelta:
    """Configurable default timing for a newly created follow-up (R104 step 3)."""
    hours = os.environ.get("NERVE_FOLLOWUP_DELAY_HOURS", "").strip()
    if hours:
        try:
            return timedelta(hours=float(hours))
        except ValueError:
            pass
    return timedelta(hours=DEFAULT_FOLLOW_UP_DELAY_HOURS)


def _followup_payload(fu: FollowUp, *, now: datetime, days_overdue: int) -> dict[str, Any]:
    """The explainable payload for a follow-up's live AttentionItem."""
    factor = (
        f"follow-up due: {fu.reason}"
        + (f" ({days_overdue} day{'s' if days_overdue != 1 else ''} overdue)"
           if days_overdue > 0 else "")
    )
    # A due follow-up is a real, time-pressing open loop: urgency climbs with
    # how far past its due it is (capped), importance is a steady high-mid
    # value. Deterministic — same inputs, same payload.
    urgency = min(0.9, 0.6 + 0.05 * days_overdue)
    entities = [fu.person_id] if fu.person_id else []
    return {
        "reason": f"Follow through: {fu.reason}",
        "urgency": urgency,
        "importance": 0.8,
        "due": fu.due_at,
        "entities": entities,
        "sources": ["followups"],
        "suggested_action": f"Follow through: {fu.reason}",
        "why": [factor, f"target: {fu.target_kind}/{fu.target_id}"],
    }


def _make_event(event_type: str, followup_id: str, payload: dict[str, Any],
                *, occurred_at: datetime, severity: str = "info") -> Event:
    """Build a bus :class:`schemas.events.Event` with a stable idempotency key
    (``<event_type>:<followup_id>``) so a replayed evaluation never double-emits
    (AC-R07: deterministic due evaluation)."""
    return Event(
        id=str(uuid4()),
        event_type=event_type,
        category=_EVENT_CATEGORY,
        source=_SOURCE,
        subject=followup_id,
        payload=payload,
        severity=severity,  # type: ignore[arg-type]
        occurred_at=occurred_at,
        created_at=occurred_at,
        idempotency_key=f"{event_type}:{followup_id}",
    )


def _wire_payload(fu: FollowUp) -> dict[str, Any]:
    """A wire-safe, reference-only event payload (no plaintext)."""
    return {
        "followup_id": fu.id,
        "target_kind": fu.target_kind,
        "target_id": fu.target_id,
        "person_id": fu.person_id,
        "due": _iso(fu.due_at),
        "reason": fu.reason,
        "created_from": fu.created_from,
        "status": fu.status,
        "attention_id": fu.attention_id,
    }


def _followup_urgency_level(urgency: Any) -> str:
    """Map the in-memory float ``urgency`` (0..1) to a durable urgency level
    (``low``/``normal``/``high``/``critical`` — the frozen §6 set)."""
    try:
        u = float(urgency)
    except (TypeError, ValueError):
        return "normal"
    if u >= 0.8:
        return "critical"
    if u >= 0.6:
        return "high"
    if u >= 0.3:
        return "normal"
    return "low"


def _followup_importance(importance: Any) -> int:
    """Map the in-memory float ``importance`` (0..1) to the durable 1..10 scale."""
    try:
        v = float(importance)
    except (TypeError, ValueError):
        return 5
    return max(1, min(10, round(v * 10)))


def _log_durable_task_exception(task: "asyncio.Task[None]") -> None:
    """Log (never raise) a background durability failure — mirroring the
    pipeline's commitment-task handler. Durability is best-effort."""
    try:
        exc = task.exception()
    except BaseException as exc:  # cancelled
        log.warning("durable follow-up persistence cancelled: %s", exc)
        return
    if exc is not None:
        log.warning("durable follow-up persistence failed: %s", exc, exc_info=exc)

class FollowUpService:
    """Follow-up tracking, due evaluation, and attention promotion
    (R104 — AC-R07, §57).

    In-memory by default (house style ``attention`` / ``waiting_on`` /
    ``drafts``). When a ``RepositoryRegistry`` is injected the service also
    mirrors each follow-up (and, once a due follow-up is promoted, its single
    live AttentionItem) into the durable personal plane — the
    :class:`~backend.persistence.models.personal.FollowupRow` and
    :class:`~backend.persistence.models.personal.AttentionItemRow` tables —
    so a follow-up survives a process restart. The durable write is scheduled
    in the background (``ingest`` stays synchronous) and is **idempotent**:
    the row id is derived deterministically from the follow-up's ``created_from``
    provenance token, so a same-process replay or a replay through a fresh
    process never creates a duplicate row. With no registry the service
    performs zero I/O and behaves exactly as before.
    """

    def __init__(
        self,
        attention: AttentionService | None = None,
        *,
        registry: Any | None = None,
    ) -> None:
        self._attention = attention or get_attention_service()
        self._items: dict[str, FollowUp] = {}
        self._events: list[Event] = []
        #: The durable personal-plane registry (or ``None`` → pure, zero I/O).
        self._registry = registry
        #: Pending durable follow-up/attention writes (flushed by
        #: :meth:`wait_for_followups`).
        self._durable_tasks: list[Any] = []

    # -- durable mirror (extend-only) ----------------------------------------
    @property
    def registry(self) -> Any | None:
        """The injected durable registry, or ``None`` (pure in-memory mode)."""
        return self._registry

    def _schedule_durable(self, coro: Any) -> None:
        """Schedule a durable mirror write off the synchronous ingest path.

        Mirrors ``CommunicationPipeline._persist_commitments``: when a loop is
        running the coroutine is scheduled as a background task; otherwise (a
        synchronous caller) it is settled inline via ``asyncio.run`` so
        durability still lands. A failure is logged, never raised.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(coro)
            except Exception as exc:  # noqa: BLE001 - durability is best-effort
                log.warning("followup durability failed: %s", exc)
            return
        task = loop.create_task(coro)
        task.add_done_callback(_log_durable_task_exception)
        self._durable_tasks.append(task)

    async def wait_for_followups(self) -> None:
        """Await all pending durable follow-up/attention writes.

        ``ingest`` stays synchronous, so durable writes are scheduled in the
        background. Callers (and tests) that need the durable plane settled
        await this. A write that already ran inline has no pending task.
        """
        if not self._durable_tasks:
            return
        tasks, self._durable_tasks = self._durable_tasks, []
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _created_from(fu: FollowUp) -> str:
        """The follow-up's deterministic provenance token (the idempotency key)."""
        return fu.created_from or f"{fu.target_kind}:{fu.target_id}"

    @staticmethod
    def _stable_row_id(prefix: str, token: str) -> str:
        """A stable row id derived from ``token`` — survives a restart because
        the in-memory follow-up id (``fu-<uuid>``) is process-local."""
        import hashlib

        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        return f"{prefix}-{digest}"

    async def _mirror_followup(self, fu: FollowUp) -> None:
        """Persist the durable ``FollowupRow`` for a created follow-up.

        Idempotent: the row id is derived from the ``created_from`` token, and
        an existing row is reused (not duplicated) on replay.
        """
        registry = self._registry
        if registry is None:
            return
        token = self._created_from(fu)
        row_id = self._stable_row_id("fu", token)
        if await registry.commitments.get_followup(row_id) is not None:
            return
        await registry.commitments.create_followup(
            id=row_id,
            text=(fu.reason or "")[:2048],
            person_id=fu.person_id,
            due_at=fu.due_at,
            state="open",
            linked_refs=[
                {"kind": "followup", "created_from": token},
            ],
        )

    async def _mirror_attention(self, fu: FollowUp, item: Any) -> None:
        """Persist the durable ``AttentionItemRow`` for a promoted follow-up.

        Idempotent: the row id is derived from the ``created_from`` token, so a
        replay (same or fresh process) never stacks a second attention item.
        """
        registry = self._registry
        if registry is None or item is None:
            return
        token = self._created_from(fu)
        item_id = self._stable_row_id("att-fu", token)
        if await registry.attention.get_attention_item(item_id) is not None:
            return
        urgency = _followup_urgency_level(getattr(item, "urgency", None))
        importance = _followup_importance(getattr(item, "importance", None))
        await registry.attention.create_attention_item(
            id=item_id,
            title=f"Follow through: {fu.reason}"[:512],
            importance=importance,
            urgency=urgency,
            body=(fu.reason or "")[:4096],
            deadline_at=fu.due_at,
            source_refs=[f"followup:{token}", f"attention:{item.attention_id}"],
            recommended_action=getattr(item, "suggested_action", None),
            escalation_policy={"source": "followups"},
            state="active",
        )

    async def _mirror_state(self, fu: FollowUp, *, followup_state: str,
                            attention_state: str | None) -> None:
        """Propagate a follow-up lifecycle transition to the durable rows."""
        registry = self._registry
        if registry is None:
            return
        token = self._created_from(fu)
        row_id = self._stable_row_id("fu", token)
        await registry.commitments.set_followup_state(row_id, followup_state)
        if attention_state is not None:
            att_id = self._stable_row_id("att-fu", token)
            await registry.attention.set_state(att_id, attention_state)

    # -- events --------------------------------------------------------------
    def events(self, event_type: str | None = None) -> list[Event]:
        """In-process lifecycle events, optionally filtered by type."""
        return [
            e for e in self._events
            if event_type is None or e.event_type == event_type
        ]

    def _emit(self, event_type: str, fu: FollowUp, *, now: datetime,
              severity: str = "info", **extra: Any) -> Event:
        payload = _wire_payload(fu)
        payload.update(extra)
        event = _make_event(event_type, fu.id, payload, occurred_at=now,
                            severity=severity)
        self._events.append(event)
        return event

    # -- creation ------------------------------------------------------------
    def create(
        self,
        *,
        target_kind: str,
        target_id: str,
        reason: str,
        person_id: str | None = None,
        due_at: datetime | str | None = None,
        created_from: str | None = None,
        status: str = FollowUpStatus.OPEN,
    ) -> dict[str, Any]:
        """Create one follow-up.

        Returns ``{"followup": FollowUp, "event": Event}``. ``due_at`` defaults
        to *now + configurable default timing* (R104 step 3). Only a live
        status (OPEN / SNOOZED) is creatable here — terminal states are only
        reachable via :meth:`complete` / :meth:`forget`.
        """
        now = _now()
        FollowUpStatus.parse(status)  # raises on unknown status
        if status in FollowUpStatus.TERMINAL:
            raise FollowUpTransitionError(
                f"cannot create a follow-up directly in terminal status {status!r}"
            )
        fu = FollowUp(
            id=make_followup_id(),
            target_kind=target_kind,
            target_id=target_id,
            person_id=person_id or None,
            due_at=_ensure_utc(due_at) or (now + default_follow_up_delay()),
            reason=reason,
            created_from=created_from or f"{target_kind}:{target_id}",
            status=status,
        )
        fu.record("created", at=now,
                  detail=f"created from {fu.created_from}")
        fu.updated_at = now
        self._items[fu.id] = fu
        # Durable mirror (extend-only): persist the FollowupRow. Scheduled off
        # the synchronous create path; a no-op when no registry is injected.
        self._schedule_durable(self._mirror_followup(fu))
        event = self._emit("followup.created", fu, now=now)
        return {"followup": fu, "event": event}

    # -- queries -------------------------------------------------------------
    def get(self, followup_id: str) -> FollowUp | None:
        return self._items.get(followup_id)

    def list(self, status: str | None = None) -> list[FollowUp]:
        """All follow-ups, optionally filtered by status.

        ``status=None`` returns everything, ordered by ``due_at`` ascending
        (most-due first), so the screen leads with what is due now.
        """
        if status is not None:
            status = FollowUpStatus.parse(status)
        items = [
            f for f in self._items.values()
            if status is None or f.status == status
        ]
        return sorted(items, key=lambda f: (f.due_at, f.created_at, f.id))

    def list_due(self, *, now: datetime | None = None) -> list[FollowUp]:
        """Live (OPEN or SNOOZED) follow-ups at or past their due time,
        ordered by ``due_at`` ascending (most-overdue first)."""
        now = now or _now()
        due = [
            f for f in self._items.values()
            if f.status in (FollowUpStatus.OPEN, FollowUpStatus.SNOOZED)
            and f.due_at <= now
        ]
        return sorted(due, key=lambda f: (f.due_at, f.created_at, f.id))

    # -- state transitions -----------------------------------------------------
    def _check_transition(self, fu: FollowUp, target: str) -> None:
        if not can_transition(fu.status, target):
            raise FollowUpTransitionError(
                f"illegal follow-up transition {fu.status} -> {target} "
                f"(follow-up {fu.id})"
            )

    def update(
        self,
        followup_id: str,
        *,
        reason: str | None = None,
        person_id: str | None = None,
        due_at: datetime | str | None = None,
        now: datetime | str | None = None,
    ) -> FollowUp:
        """Edit a live follow-up (reason / person / due).

        Terminal follow-ups are immutable (raises
        :class:`FollowUpTransitionError`).
        """
        fu = self._items.get(followup_id)
        if fu is None:
            raise KeyError(followup_id)
        if fu.is_terminal:
            raise FollowUpTransitionError(
                f"follow-up {fu.id} is {fu.status} (terminal) and immutable"
            )
        now = _ensure_utc(now) or _now()
        changes: list[str] = []
        if reason is not None:
            reason = reason.strip()
            if not reason:
                raise ValueError("reason may not be empty")
            fu.reason = reason
            changes.append("reason")
        if person_id is not None:
            fu.person_id = person_id or None
            changes.append("person_id")
        if due_at is not None:
            fu.due_at = _ensure_utc(due_at)  # type: ignore[assignment]
            changes.append("due_at")
        if changes:
            fu.updated_at = now
            fu.record("updated", at=now, detail=f"updated: {', '.join(changes)}")
            self._emit("followup.updated", fu, now=now,
                       detail=fu.audit_history[-1].detail)
        return fu

    def snooze(self, followup_id: str, *,
               until: datetime | str | None = None,
               for_delta: timedelta | None = None,
               detail: str | None = None,
               now: datetime | str | None = None) -> FollowUp:
        """Snooze a live follow-up (OPEN <-> SNOOZED).

        ``until`` (absolute) or ``for_delta`` (relative to now; default 1 day)
        sets the new due time. Propagates to the live AttentionItem: a
        snoozed follow-up's item is SUPPRESSED until the follow-up is
        re-opened or re-triggers (AC-R07: attention snooze/resolve must
        propagate to the live follow-up — and vice versa).
        """
        fu = self._items.get(followup_id)
        if fu is None:
            raise KeyError(followup_id)
        self._check_transition(fu, FollowUpStatus.SNOOZED)
        now = _ensure_utc(now) or _now()
        if until is not None:
            new_due = _ensure_utc(until) or now
        else:
            new_due = now + (for_delta or timedelta(days=1))
        fu.status = FollowUpStatus.SNOOZED
        fu.due_at = new_due
        fu.updated_at = now
        fu.record("snoozed", at=now,
                  detail=detail or f"snoozed until {new_due.isoformat()}")
        self._emit("followup.updated", fu, now=now, detail=fu.audit_history[-1].detail)
        # Propagate to the live item: hide it from the Now screen while
        # snoozed (re-shown if it is re-opened / re-triggers).
        if fu.attention_id:
            self._attention.update_status(
                fu.attention_id,
                AttentionState.SUPPRESSED,
                action="snoozed",
                reason=f"follow-up {fu.id} snoozed until {new_due.isoformat()}",
            )
        return fu

    def reopen(self, followup_id: str, *, detail: str | None = None,
               now: datetime | str | None = None) -> FollowUp:
        """Re-open a snoozed follow-up (SNOOZED -> OPEN), reviving its item."""
        fu = self._items.get(followup_id)
        if fu is None:
            raise KeyError(followup_id)
        self._check_transition(fu, FollowUpStatus.OPEN)
        now = _ensure_utc(now) or _now()
        fu.status = FollowUpStatus.OPEN
        fu.updated_at = now
        fu.record("updated", at=now, detail=detail or "reopened")
        self._emit("followup.updated", fu, now=now, detail=fu.audit_history[-1].detail)
        if fu.attention_id and fu.due_at <= now:
            self._attention.update_status(
                fu.attention_id,
                AttentionState.NOW,
                action="reopened",
                reason=f"follow-up {fu.id} reopened and due",
            )
        return fu

    def complete(self, followup_id: str, *, note: str | None = None,
                 now: datetime | str | None = None) -> FollowUp:
        """Mark a live follow-up COMPLETED and resolve its AttentionItem.

        Emits ``followup.completed``; the Now-screen item is resolved so it
        leaves the screen (mirrors the waiting-on ``resolve`` behavior).
        """
        fu = self._items.get(followup_id)
        if fu is None:
            raise KeyError(followup_id)
        self._check_transition(fu, FollowUpStatus.COMPLETED)
        now = _ensure_utc(now) or _now()
        fu.status = FollowUpStatus.COMPLETED
        fu.updated_at = now
        fu.record("completed", at=now, detail=note or "completed")
        self._emit("followup.completed", fu, now=now, note=note)
        # Durable mirror (extend-only): the durable row follows the
        # terminal transition (done + resolved attention).
        self._schedule_durable(
            self._mirror_state(fu, followup_state="done",
                               attention_state="resolved")
        )
        if fu.attention_id:
            self._attention.update_status(
                fu.attention_id,
                AttentionState.RESOLVED,
                action="resolve",
                reason=f"follow-up {fu.id} completed",
            )
        return fu

    def forget(self, followup_id: str, *, note: str | None = None,
               now: datetime | str | None = None) -> FollowUp:
        """Mark a live follow-up FORGOTTEN (it no longer matters) and resolve
        its AttentionItem. Emits ``followup.forgotten``."""
        fu = self._items.get(followup_id)
        if fu is None:
            raise KeyError(followup_id)
        self._check_transition(fu, FollowUpStatus.FORGOTTEN)
        now = _ensure_utc(now) or _now()
        fu.status = FollowUpStatus.FORGOTTEN
        fu.updated_at = now
        fu.record("forgotten", at=now, detail=note or "forgotten")
        self._emit("followup.forgotten", fu, now=now, note=note)
        # Durable mirror (extend-only): the durable row follows the
        # terminal transition (dropped + dismissed attention).
        self._schedule_durable(
            self._mirror_state(fu, followup_state="dropped",
                               attention_state="dismissed")
        )
        if fu.attention_id:
            self._attention.update_status(
                fu.attention_id,
                AttentionState.RESOLVED,
                action="resolve",
                reason=f"follow-up {fu.id} forgotten",
            )
        return fu

    # -- attention seam --------------------------------------------------------
    def _upsert_attention(self, fu: FollowUp, *, now: datetime) -> Any:
        """Create (or refresh) the single live AttentionItem for a triggered
        follow-up. Returns the item, or ``None`` if none is live."""
        days_overdue = max(0, int((now - fu.due_at).total_seconds() // 86400))
        payload = _followup_payload(fu, now=now, days_overdue=days_overdue)
        existing = self._attention.get(fu.attention_id) if fu.attention_id else None
        still_live = (
            existing is not None
            and existing.status
            in (
                AttentionState.NOW,
                AttentionState.TODAY,
                AttentionState.SOON,
                AttentionState.WAITING_ON,
                AttentionState.FYI,
            )
        )
        if still_live and fu.attention_id is not None:
            refreshed = self._attention.refresh(fu.attention_id, payload)
        else:
            created = self._attention.create(payload)
            fu.attention_id = created.attention_id
            refreshed = created
            # Durable mirror (extend-only): persist the promoted item's
            # AttentionItemRow. Scheduled off the synchronous path; no-op when
            # no registry is injected. Idempotent by the provenance token.
            self._schedule_durable(self._mirror_attention(fu, refreshed))
        return refreshed

    def evaluate_due(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Deterministic due-evaluation hook (R104 step 4, AC-R07).

        Returns one record per due live follow-up — ordered by ``due_at``
        ascending — each with the follow-up, its (re)triggered
        ``followup.triggered`` event, and the AttentionItem that was created or
        refreshed on the shared attention queue (source ``followups`` with a
        reference to the follow-up id). Idempotent: re-running at the same
        ``now`` refreshes the same single AttentionItem per follow-up and
        re-emits nothing new beyond one ``followup.triggered`` per call per
        follow-up — no stacked duplicates.
        """
        now = _ensure_utc(now) or _now()
        due = self.list_due(now=now)
        results: list[dict[str, Any]] = []
        for fu in due:
            item = self._upsert_attention(fu, now=now)
            days_overdue = max(0, int((now - fu.due_at).total_seconds() // 86400))
            event = self._emit(
                "followup.triggered",
                fu,
                now=now,
                severity="warning" if days_overdue > 0 else "info",
                days_overdue=days_overdue,
                attention_id=fu.attention_id,
            )
            results.append(
                {
                    "followup": fu,
                    "attention": item,
                    "event": event,
                    "days_overdue": days_overdue,
                }
            )
        return results

    # -- waiting-on integration (R104 step 3) ----------------------------------
    @staticmethod
    def from_waiting_on(item: Any, *, now: datetime | None = None,
                         follow_up_delay: timedelta | None = None) -> dict[str, Any]:
        """Build the normalized create-payload for a follow-up from a
        waiting-on request (R104 step 3: "the waiting-on integration").

        ``item`` is a :class:`~backend.api.models.WaitingOnItem` (or anything
        with ``request_id`` / ``person_display_name`` / ``description`` /
        ``requested_at`` / ``person_id``). Pure — performs no state changes.
        """
        now = _ensure_utc(now) or _now()
        delay = follow_up_delay or default_follow_up_delay()
        person = getattr(item, "person_display_name", "") or ""
        description = getattr(item, "description", "") or ""
        requested_at = _ensure_utc(getattr(item, "requested_at", None)) or now
        return {
            "target_kind": "waiting_on",
            "target_id": str(getattr(item, "request_id", "")),
            "person_id": getattr(item, "person_id", None),
            "due_at": requested_at + delay,
            "reason": (
                f"Follow up with {person} — {description}"
                if person else f"Follow up — {description}"
            ),
            "created_from": f"waiting_on:{getattr(item, 'request_id', '')}",
        }


# Process-wide singleton (see module docstring).
_service: FollowUpService | None = None


def get_followup_service() -> FollowUpService:
    global _service
    if _service is None:
        _service = FollowUpService()
    return _service


def reset_followup_service() -> None:
    """Test hook — replace the singleton with a fresh, empty service."""
    global _service
    _service = FollowUpService()


# Convenience for call sites that want the live attention item of a
# triggered follow-up.
__all__ = [
    "FollowUpService",
    "get_followup_service",
    "reset_followup_service",
    "default_follow_up_delay",
]
