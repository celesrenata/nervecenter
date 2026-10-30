"""Obligation service — orchestration over the durable obligation plane (P2-A).

Composition (spec: ``docs/contracts/obligation.md``):

- :class:`ObligationsService` — orchestration over the
  :class:`~backend.persistence.repositories.obligations_repo.ObligationsRepository`.
  Exposes create / upsert / accept / snooze / complete / dismiss / reverify,
  a ``needs_you`` projection (Today "Needs You"), a Low Energy Mode
  single-next-step picker, and a deterministic dedup-key builder.

- :func:`build_obligation_from_source` — bridges a *plane record* (a bill,
  an appointment, a routine, a follow-up, a commitment, or a waiting-on
  item) into an :class:`ObligationRow` via the repository's
  ``upsert_by_dedup_key``. Each plane contributes its own
  ``source_record_ref`` so the obligation *references* rather than
  *duplicates* the concrete record. The plane's state is authoritative —
  this service never writes to the source plane.

- :func:`register_obligations_today_provider` — exposes the
  :class:`ObligationsTodayProvider` (Today ``needs_you`` + ``coming_up``
  contributors: actionable obligations sorted by priority, due-within-7-days,
  stale-flagged).

Hard rules (carry-forward from P2-A contract):

- **No execution.** This service only reads + writes obligation rows; it
  does NOT call bills/calendar/routines repos to mutate their state. The
  obligation plane *references* plane records via ``source_record_ref``.
- **Inferred ≠ actionable.** An ``origin="inferred"`` obligation requires an
  explicit user accept (``accept``) before it is actionable in
  ``needs_you`` / Today.
- **Terminal states are single-writer.** ``done`` / ``dismissed`` are written
  ONLY by the repository's ``complete`` / ``dismiss``; no service path flips
  them implicitly.
- **Snooze is reversible.** ``wake_up_snoozed`` (called on the attention
  sweep) returns snoozed rows whose window has passed back to ``active``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend.persistence.models.obligations import (
    OBLIGATION_ORIGINS,
    OBLIGATION_STATUSES,
    OBLIGATION_TYPES,
    ObligationRow,
)
from backend.persistence.repositories.obligations_repo import (
    ObligationsRepository,
    _ensure_utc,
    _parse_iso,
    _require,
)

# How old (in days) must ``last_verified_at`` be before an obligation is
# flagged ``stale`` by the service-level sweep. The repository's
# ``mark_stale`` uses the same default; a service-level sweep (called on
# Today reads) is the caller.
STALE_AFTER_DAYS: float = 30.0

#: "Coming up" window: obligations whose due date falls within this many
#: days from now are surfaced on Today.
COMING_UP_WINDOW_DAYS: int = 14

#: How many items at most are surfaced on Today's "Needs You" section.
NEEDS_YOU_LIMIT: int = 3


# ---------------------------------------------------------------------------
# Dedup-key builder (frozen grammar — contract §3)
# ---------------------------------------------------------------------------


def dedup_key_for(
    *,
    obligation_type: str,
    source_ref: dict[str, Any] | None,
    due_at: datetime | None = None,
) -> str:
    """Build the canonical ``dedup_key`` string for an obligation.

    Grammar: ``{type}|{plane}:{record_id}|{YYYY-MM-DD}`` when a source record
    exists, else ``{type}|adhoc:{hash_seed}|{YYYY-MM-DD}``. The ``due_at`` is
    normalised to its date part (not time) so the same bill seen on the
    same day across email + calendar + Messages collapses to one key.

    Examples:
    - ``bill|bills:a1b2c3|2026-11-15``
    - ``appointment|calendar:e4f5g6|2026-11-20``
    - ``follow_up|communications:h7i8j9|2026-11-05``
    - ``task|adhoc:<sha1[:12]>|2026-11-10``
    """
    if not obligation_type:
        raise ValueError("obligation_type is required for a dedup key")
    _require(obligation_type, OBLIGATION_TYPES, "obligation type")
    if source_ref and source_ref.get("plane") and source_ref.get("id"):
        plane = source_ref["plane"]
        rid = source_ref["id"]
        src = f"{plane}:{rid}"
    else:
        # For ad-hoc obligations (no source record), the caller is expected
        # to include a ``hash_seed`` in ``source_ref`` or use a stable title
        # prefix. We derive a short hash from the title to keep dedup
        # deterministic across restarts.
        seed = (source_ref or {}).get("hash_seed", "") or ""
        import hashlib

        h = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12] if seed else "adhoc"
        src = f"adhoc:{h}"
    day = due_at.date().isoformat() if due_at is not None else "nodate"
    return f"{obligation_type}|{src}|{day}"


# ---------------------------------------------------------------------------
# Bridge from plane records
# ---------------------------------------------------------------------------


def _plane_ref(plane: str, record_id: str) -> dict[str, Any]:
    return {"plane": plane, "id": record_id}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def build_obligation_from_bill(
    bill: dict[str, Any] | Any,
) -> dict[str, Any]:
    """Bridge a bill record (dict or row) into an obligation spec.

    The bill dict is the ``_bill_dict`` projection from
    :mod:`backend.bills.service` (or a raw :class:`BillRow`). The bill's
    escalation level is mirrored as the obligation's ``priority``:
    level 0 → priority 0, level 1 → 1, level 2 → 2, level 3 (overdue) → 3.
    """
    payee = getattr(bill, "payee", None) or (bill.get("payee") if isinstance(bill, dict) else None) or ""
    amount = getattr(bill, "amount_cents", None) or (bill.get("amount_cents") if isinstance(bill, dict) else None)
    currency = getattr(bill, "currency", None) or (bill.get("currency") if isinstance(bill, dict) else None) or "USD"
    due_raw = getattr(bill, "due_at", None) or (bill.get("due_at") if isinstance(bill, dict) else None)
    status = getattr(bill, "status", None) or (bill.get("status") if isinstance(bill, dict) else None) or "pending"
    recurrence = getattr(bill, "recurrence", None) or (bill.get("recurrence") if isinstance(bill, dict) else None) or "one-time"
    bill_id = getattr(bill, "id", None) or (bill.get("id") if isinstance(bill, dict) else None) or ""
    due_at = _parse_iso(due_raw) if isinstance(due_raw, str) else _ensure_utc(due_raw)
    # priority mirrors escalation level (0–3)
    if status == "stale":
        priority = 3
    elif due_at is not None:
        now = datetime.now(UTC)
        delta = due_at - now
        if delta.total_seconds() < 0:
            priority = 3
        elif delta <= timedelta(hours=48):
            priority = 2
        elif delta <= timedelta(days=7):
            priority = 1
        else:
            priority = 0
    else:
        priority = 1
    evidence = getattr(bill, "evidence", None) or (
        bill.get("evidence") if isinstance(bill, dict) else None
    ) or []
    return {
        "obligation_type": "bill",
        "dedup_key": dedup_key_for(
            obligation_type="bill",
            source_ref=_plane_ref("bills", bill_id),
            due_at=due_at,
        ),
        "title": f"Bill: {payee} — {currency} {amount} due",
        "origin": "explicit",
        "source_account": "",
        "due_at": due_at,
        "evidence": list(evidence),
        "priority": priority,
        "next_action": "Review and confirm payment" if status == "pending" else None,
        "consequence": "Unpaid bill may incur late fees or service interruption",
        "source_record_ref": _plane_ref("bills", bill_id),
        "relationships": (
            [{"kind": "recurrence", "target_id": bill_id}] if recurrence != "one-time" else []
        ),
    }


def build_obligation_from_appointment(
    appointment: dict[str, Any] | Any,
) -> dict[str, Any]:
    """Bridge a calendar event into an obligation spec."""
    event_id = (
        appointment.get("id")
        if isinstance(appointment, dict)
        else getattr(appointment, "id", None)
        or "unknown"
    )
    title = (
        appointment.get("title")
        if isinstance(appointment, dict)
        else getattr(appointment, "title", "Appointment")
        or "Appointment"
    )
    start_raw = (
        appointment.get("start")
        if isinstance(appointment, dict)
        else getattr(appointment, "start", None)
    )
    start_at = _parse_iso(start_raw) if isinstance(start_raw, str) else _ensure_utc(start_raw)
    prep = (
        appointment.get("prep")
        if isinstance(appointment, dict)
        else getattr(appointment, "prep", None)
    ) or []
    docs = (
        appointment.get("required_docs")
        if isinstance(appointment, dict)
        else getattr(appointment, "required_docs", None)
    ) or []
    return {
        "obligation_type": "appointment",
        "dedup_key": dedup_key_for(
            obligation_type="appointment",
            source_ref=_plane_ref("calendar", str(event_id)),
            due_at=start_at,
        ),
        "title": f"Appointment: {title}",
        "origin": "explicit",
        "source_account": "",
        "due_at": start_at,
        "evidence": [],
        "priority": 1,
        "next_action": (
            f"Prepare: {prep[0]['title']}" if prep else "Review appointment"
        ),
        "consequence": (
            f"{len(docs)} required doc(s) to bring" if docs else "Missing the appointment"
        ),
        "source_record_ref": _plane_ref("calendar", str(event_id)),
        "relationships": [
            {"kind": "prep_task", "target_id": p.get("id", "unknown")} for p in prep
        ]
        + [
            {"kind": "required_doc", "target_id": d.get("id", "unknown")}
            for d in docs
        ],
    }


def build_obligation_from_followup(
    followup: dict[str, Any] | Any,
) -> dict[str, Any]:
    """Bridge a follow-up / commitment into an obligation spec."""
    fu_id = (
        followup.get("id")
        if isinstance(followup, dict)
        else getattr(followup, "id", None)
        or "unknown"
    )
    title = (
        followup.get("title")
        if isinstance(followup, dict)
        else getattr(followup, "title", "Follow-up")
        or "Follow-up"
    )
    due_raw = (
        followup.get("due_at")
        if isinstance(followup, dict)
        else getattr(followup, "due_at", None)
    )
    due_at = _parse_iso(due_raw) if isinstance(due_raw, str) else _ensure_utc(due_raw)
    urgency = (
        followup.get("urgency")
        if isinstance(followup, dict)
        else getattr(followup, "urgency", "normal")
    ) or "normal"
    priority = 2 if urgency == "high" else 1
    return {
        "obligation_type": "follow_up",
        "dedup_key": dedup_key_for(
            obligation_type="follow_up",
            source_ref=_plane_ref("communications", str(fu_id)),
            due_at=due_at,
        ),
        "title": f"Follow up: {title}",
        "origin": "explicit",
        "source_account": "",
        "due_at": due_at,
        "evidence": [],
        "priority": priority,
        "next_action": title,
        "consequence": "A committed follow-up left unaddressed",
        "source_record_ref": _plane_ref("communications", str(fu_id)),
        "relationships": [],
    }


def build_obligation_from_source(
    source: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch to the right bridge based on ``source['plane']``.

    ``source`` must carry a ``plane`` key (``bills`` / ``calendar`` /
    ``communications``) and a nested ``record`` key (the plane record).
    Returns an obligation spec (the dict the service will upsert).
    """
    plane = source.get("plane")
    record = source.get("record") or source
    if plane == "bills":
        return build_obligation_from_bill(record)
    if plane == "calendar":
        return build_obligation_from_appointment(record)
    if plane in ("communications", "followups"):
        return build_obligation_from_followup(record)
    raise ValueError(f"unsupported plane for obligation bridge: {plane!r}")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ObligationsService:
    """Orchestration over the durable obligation plane.

    Holds a single :class:`ObligationsRepository`. Constructed by the API
    layer from ``registry.obligations`` (the house degradation pattern).
    """

    def __init__(self, repo: ObligationsRepository) -> None:
        self._repo = repo

    # -- upsert ---------------------------------------------------------

    async def upsert_from_source(
        self,
        source: dict[str, Any],
        *,
        registry: Any | None = None,
    ) -> dict[str, Any]:
        """Bridge a plane record into an obligation (upsert by dedup key).

        Returns the obligation spec merged with the row state. Mirrors the
        result into the durable attention plane if the obligation is
        actionable and due-within the coming-up window (house pattern).
        """
        spec = build_obligation_from_source(source)
        row, created = await self._repo.upsert_by_dedup_key(**spec)
        out = self._to_dict(row)
        out["created"] = created
        # attention mirror: actionable, due-within-window, not terminal
        if (
            registry is not None
            and row.status not in ("done", "dismissed")
            and row.due_at is not None
            and (row.due_at - datetime.now(UTC)) <= timedelta(days=COMING_UP_WINDOW_DAYS)
        ):
            await self._mirror_attention(row, registry)
        return out

    async def create(
        self,
        *,
        obligation_type: str,
        title: str,
        origin: str = "explicit",
        due_at: datetime | None = None,
        dedup_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Insert a new obligation (explicit, user-created).

        The dedup key is built if not supplied.
        """
        if not dedup_key:
            dedup_key = dedup_key_for(
                obligation_type=obligation_type,
                source_ref={"hash_seed": title[:64]},
                due_at=due_at,
            )
        row = await self._repo.create(
            obligation_type=obligation_type,
            dedup_key=dedup_key,
            title=title,
            origin=origin,
            due_at=due_at,
            **kwargs,
        )
        return self._to_dict(row)

    # -- reads ----------------------------------------------------------

    async def needs_you(
        self,
        *,
        limit: int = NEEDS_YOU_LIMIT,
    ) -> list[dict[str, Any]]:
        """The Today "Needs You" section: actionable obligations.

        Excludes ``done`` / ``dismissed`` / ``snoozed`` / ``stale`` /
        ``waiting``; inferred obligations are included but flagged
        ``requires_acceptance``. Ordered by (priority desc, due asc
        nulls-last).
        """
        rows = await self._repo.list(
            status_in=("active", "in_progress"), limit=limit
        )
        out = []
        for row in rows:
            d = self._to_dict(row)
            d["requires_acceptance"] = row.origin == "inferred"
            out.append(d)
        return out

    async def coming_up(
        self,
        *,
        days: int = COMING_UP_WINDOW_DAYS,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """The Today "Coming Up" section: due-within-window obligations."""
        now = datetime.now(UTC)
        rows = await self._repo.list(
            due_after=now,
            due_before=now + timedelta(days=days),
            actionable_only=True,
            limit=limit,
        )
        return [self._to_dict(r) for r in rows]

    async def waiting_on(self, limit: int = 50) -> list[dict[str, Any]]:
        """The Today "Waiting On" section: obligations awaiting others."""
        rows = await self._repo.list(
            obligation_type="waiting_on",
            actionable_only=True,
            limit=limit,
        )
        return [self._to_dict(r) for r in rows]

    async def stale(self, limit: int = 50) -> list[dict[str, Any]]:
        """The Today "Source Health" stale-flagged section."""
        rows = await self._repo.list(status="stale", limit=limit)
        return [self._to_dict(r) for r in rows]

    async def get(self, obligation_id: str) -> dict[str, Any] | None:
        row = await self._repo.get(obligation_id)
        return self._to_dict(row) if row else None

    # -- Low Energy Mode 2.0 -------------------------------------------

    def pick_next_action(
        self,
        needs_you: list[dict[str, Any]],
        *,
        urgent_only: bool = False,
    ) -> dict[str, Any] | None:
        """Low Energy Mode 2.0 — pick ONE next step from Needs You.

        Returns the single most-actionable, smallest next step:
        - Filter to ``active`` / ``in_progress`` (not ``stale`` /
          ``snoozed`` / ``waiting`` / ``scheduled``).
        - Exclude inferred (``requires_acceptance``) — those need explicit
          acceptance before they are actionable.
        - Prefer the highest-priority item; tiebreak by earliest due.
        - Return its ``next_action`` prose + a ``tiny_step`` (the first
          sentence of the next_action) + a ``done_later_help_me`` shape.

        ``urgent_only=True`` restricts to priority ≥ 3 (overdue / due
        within 48h). Returns ``None`` when nothing qualifies (the caller
        should surface "Nothing needs your attention right now").
        """
        actionable = [
            d
            for d in needs_you
            if d.get("status") in ("active", "in_progress")
            and not d.get("requires_acceptance")
            and d.get("next_action")
        ]
        if urgent_only:
            actionable = [d for d in actionable if int(d.get("priority", 0)) >= 3]
        if not actionable:
            return None
        # sort: priority desc, due asc (nulls last)
        actionable.sort(
            key=lambda d: (
                -int(d.get("priority", 0)),
                d.get("due_at") or "9999-12-31T23:59:59+00:00",
            )
        )
        top = actionable[0]
        next_action = top["next_action"]
        # tiny step: first sentence
        tiny_step = next_action.split(".")[0].strip()
        if len(tiny_step) < 8:
            tiny_step = next_action[:40]
        return {
            "obligation_id": top["id"],
            "next_action": next_action,
            "tiny_step": tiny_step,
            "done_later_help_me": {
                "done": f"Mark '{top['title']}' as done",
                "later": f"Snooze '{top['title']}' for later",
                "help_me": f"Break '{top['title']}' into smaller steps",
            },
            "urgency": int(top.get("priority", 0)),
            "due_at": top.get("due_at"),
        }

    # -- status transitions ----------------------------------------------

    async def accept(self, obligation_id: str) -> dict[str, Any] | None:
        row = await self._repo.accept(obligation_id)
        return self._to_dict(row) if row else None

    async def snooze(self, obligation_id: str, until: datetime) -> dict[str, Any] | None:
        row = await self._repo.snooze(obligation_id, until)
        return self._to_dict(row) if row else None

    async def wake_up_snoozed(self, now: datetime | None = None) -> list[dict[str, Any]]:
        rows = await self._repo.wake_up_snoozed(now)
        return [self._to_dict(r) for r in rows]

    async def complete(self, obligation_id: str) -> dict[str, Any] | None:
        row = await self._repo.complete(obligation_id)
        return self._to_dict(row) if row else None

    async def dismiss(self, obligation_id: str) -> dict[str, Any] | None:
        row = await self._repo.dismiss(obligation_id)
        return self._to_dict(row) if row else None

    async def reverify(self, obligation_id: str) -> dict[str, Any] | None:
        row = await self._repo.reverify(obligation_id)
        return self._to_dict(row) if row else None

    async def sweep_stale(self) -> list[dict[str, Any]]:
        """Sweep: mark obligations whose last_verified_at is old as stale."""
        rows = await self._repo.mark_stale(threshold_days=STALE_AFTER_DAYS)
        return [self._to_dict(r) for r in rows]

    # -- attention mirror (house pattern) --------------------------------

    async def _mirror_attention(
        self, row: ObligationRow, registry: Any
    ) -> None:
        """Mirror an actionable, due-within-window obligation into a
        durable attention item (house pattern, same as bills / routines).
        One item per obligation, updated in place.
        """
        if row.status in ("done", "dismissed"):
            return
        urgency = min(0.2 + 0.15 * int(row.priority), 1.0)
        importance = min(0.3 + 0.15 * int(row.priority), 1.0)
        source_refs = [f"obligation:{row.id}"]
        try:
            rows = await registry.attention.list_attention_items(state="active")
            existing = next(
                (
                    r
                    for r in rows
                    if f"obligation:{row.id}" in list(r.source_refs or [])
                ),
                None,
            )
            if existing is None:
                row2 = await registry.attention.create_attention_item(
                    title=row.title,
                    body=row.next_action or "",
                    urgency=urgency,
                    importance=importance,
                    deadline_at=row.due_at,
                    source_refs=source_refs,
                )
            else:
                existing.urgency = urgency
                existing.importance = importance
                existing.deadline_at = row.due_at
                row2 = existing
            await registry.attention.set_state(row2.id, "active")
        except Exception:  # noqa: BLE001 — degrade to in-memory only
            try:
                from backend.attention.service import get_attention_service

                service = get_attention_service()
                service.create(
                    {
                        "reason": row.title,
                        "urgency": urgency,
                        "importance": importance,
                        "due": row.due_at,
                        "entities": [row.id],
                        "sources": ["obligations"],
                        "suggested_action": row.next_action or "Review",
                        "why": ["obligation due"],
                    }
                )
            except Exception:  # noqa: BLE001 — fully degraded
                pass

    # -- projections -----------------------------------------------------

    def _to_dict(self, row: ObligationRow) -> dict[str, Any]:
        """Serialize a row to a JSON-friendly dict (API boundary)."""
        return {
            "id": row.id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "schema_version": row.schema_version,
            "obligation_type": row.obligation_type,
            "origin": row.origin,
            "source_account": row.source_account,
            "owner_id": row.owner_id,
            "dedup_key": row.dedup_key,
            "title": row.title,
            "due_at": row.due_at.isoformat() if row.due_at else None,
            "trigger": row.trigger or {},
            "confidence": row.confidence,
            "uncertainty": row.uncertainty or {},
            "evidence": row.evidence or [],
            "status": row.status,
            "next_action": row.next_action,
            "suggested_action": row.suggested_action,
            "priority": row.priority,
            "consequence": row.consequence,
            "last_verified_at": (
                row.last_verified_at.isoformat() if row.last_verified_at else None
            ),
            "snooze_until": (
                row.snooze_until.isoformat() if row.snooze_until else None
            ),
            "dismissed_at": (
                row.dismissed_at.isoformat() if row.dismissed_at else None
            ),
            "completed_at": (
                row.completed_at.isoformat() if row.completed_at else None
            ),
            "relationships": row.relationships or [],
            "source_record_ref": row.source_record_ref or {},
        }


# ---------------------------------------------------------------------------
# Today provider
# ---------------------------------------------------------------------------


class ObligationsTodayProvider:
    """Today ``needs_you`` + ``coming_up`` + ``handled`` contributor.

    Implements the :class:`~backend.api.endpoints.today.TodaySectionProvider`
    protocol (the house pattern used by bills / routines). The provider reads
    the durable obligation plane via the registry (degraded → empty sections).
    """

    async def name(self) -> str:
        return "obligations"

    async def needs_you(self, ctx: Any) -> list[dict[str, Any]]:
        registry = getattr(ctx, "registry", None)
        if registry is None:
            return []
        try:
            svc = ObligationsService(registry.obligations)
            return await svc.needs_you(limit=NEEDS_YOU_LIMIT)
        except Exception:  # noqa: BLE001 — degrade
            return []

    async def coming_up(self, ctx: Any) -> list[dict[str, Any]]:
        registry = getattr(ctx, "registry", None)
        if registry is None:
            return []
        try:
            svc = ObligationsService(registry.obligations)
            return await svc.coming_up(days=COMING_UP_WINDOW_DAYS)
        except Exception:  # noqa: BLE001 — degrade
            return []

    async def handled(self, ctx: Any) -> list[dict[str, Any]]:
        """Completed / dismissed obligations in the last 24h (handled log)."""
        registry = getattr(ctx, "registry", None)
        if registry is None:
            return []
        try:
            svc = ObligationsService(registry.obligations)
            now = getattr(ctx, "now", None) or datetime.now(UTC)
            rows = await svc._repo.list(
                status="done",
                limit=50,
            )
            out = []
            for r in rows:
                if r.completed_at and (now - r.completed_at) <= timedelta(hours=24):
                    out.append(svc._to_dict(r))
            return out
        except Exception:  # noqa: BLE001 — degrade
            return []

    async def waiting_on(self, ctx: Any) -> list[dict[str, Any]]:
        """Obligations whose ball is in someone else's court (P2-F §4).

        Normalised to the Today item shape (``kind`` / ``deadline_at``) so the
        aggregator can dedup against ``needs_you`` and sort by deadline.
        """
        registry = getattr(ctx, "registry", None)
        if registry is None:
            return []
        try:
            svc = ObligationsService(registry.obligations)
            rows = await svc.waiting_on(limit=50)
            out = []
            for d in rows:
                out.append(
                    {
                        "id": d["id"],
                        "kind": "obligation",
                        "title": d.get("title"),
                        "deadline_at": d.get("due_at"),
                        "obligation_type": d.get("obligation_type"),
                        "status": d.get("status"),
                        "next_action": d.get("next_action"),
                        "consequence": d.get("consequence"),
                    }
                )
            return out
        except Exception:  # noqa: BLE001 — degrade
            return []


_obligations_today_provider_singleton: ObligationsTodayProvider | None = None


def register_obligations_today_provider() -> ObligationsTodayProvider:
    """Register :class:`ObligationsTodayProvider` with the Today aggregator.

    Call once at app startup. Returns the single process-wide provider
    instance so that repeated registrations (multiple ``create_app()``
    calls in tests, restarts) resolve to the same object — the aggregator's
    identity dedup then keeps exactly one copy, so a waiting-on obligation
    is never double-counted across sections.
    """
    global _obligations_today_provider_singleton
    if _obligations_today_provider_singleton is None:
        _obligations_today_provider_singleton = ObligationsTodayProvider()
    from backend.api.endpoints import today

    today.register_today_provider(_obligations_today_provider_singleton)
    return _obligations_today_provider_singleton


__all__ = [
    "ObligationsService",
    "ObligationsTodayProvider",
    "COMING_UP_WINDOW_DAYS",
    "NEEDS_YOU_LIMIT",
    "STALE_AFTER_DAYS",
    "build_obligation_from_bill",
    "build_obligation_from_appointment",
    "build_obligation_from_followup",
    "build_obligation_from_source",
    "dedup_key_for",
    "register_obligations_today_provider",
]
