"""Bills service — orchestration over the durable bills plane (workstream G).

Composition (spec §7):

- :class:`BillService` — list / upcoming / monthly-calendar / confirm /
  accept / reject / dispute over the repository. **Nothing here ever
  transfers money or purchases** — the service exposes no payment
  integration at all; ``confirm_paid`` is the explicit user action that
  records "the user says they paid" (it does not pay anything).
- :func:`bill_escalation_level` — the deterministic 0–3 escalation ladder
  consumed by the Attention v2 workstream (K). It is a pure function of
  ``(bill, now)``:

    ============  =====================================================
    level 0       pending, more than 7 days before due
    level 1       pending, due within 7 days
    level 2       pending, due within 48 hours
    level 3       overdue (due date passed, unconfirmed)
    ============  =====================================================

  ``confirmed_paid`` bills carry no escalation (return ``None``);
  ``disputed`` bills are frozen at level 1 (attention without overdue
  pressure); ``stale`` behaves as level 3 (overdue + unconfirmed).
- :func:`refresh_bill_attention` — mirrors the escalation into the
  attention plane: a durable ``attention_items`` row (via
  ``registry.attention`` — the wave-1 source of truth the Today page and
  K consume) PLUS a mirror to the in-memory
  :mod:`backend.attention` service (the C mail-bridge pattern). One item
  per bill, level tracked in the durable item's ``escalation_policy``
  (``{"bills.escalation_level": N}``) so re-escalation updates in place.
- :func:`register_bills_today_provider` — exposes the
  :class:`BillsTodayProvider` (Today ``coming_up`` contributor: upcoming
  bills within 14 days, sorted) to ``backend.api.endpoints.today``. The
  integrator calls this at startup; the bills package never edits
  ``today.py``.

House degradation: with no durable registry the service methods return
empty shapes (the endpoints turn those into 200-empty / 202
``executed:false``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend.persistence.repositories.bills_repo import BillsRepository

#: "Due within" windows for the escalation ladder (frozen, deterministic).
ESCALATION_SOON_DAYS = 7
ESCALATION_SOON_HOURS = 48

#: P2-B: hours after due before a missing-payment confirmation reminder fires.
MISSING_PAYMENT_REMINDER_HOURS = 48

#: P2-B: ratio tolerance for amount drift before a price-increase notice fires.
#: An installment within 1.10× the head's amount is "same price"; beyond that
#: is a price increase worth flagging.
PRICE_DRIFT_TOLERANCE = 1.10


def bill_escalation_level(bill: Any, now: datetime | None = None) -> int | None:
    """Deterministic escalation level for a bill (contract consumed by K).

    Parameters
    ----------
    bill:
        Any object exposing ``status``, ``confirmed_at`` and a tz-aware
        ``due_at`` (a :class:`BillRow` or a dict-like with the same keys).
    now:
        The reference clock (UTC); defaults to the current UTC time.

    Returns
    -------
    ``0`` — pending, more than 7 days before due.
    ``1`` — pending, due within 7 days.
    ``2`` — pending, due within 48 hours.
    ``3`` — overdue (due passed, unconfirmed — the stale case).
    ``None`` — no escalation (confirmed paid; or a bill with no due date).

    ``disputed`` bills report level 1 (still needs eyes, no overdue
    pressure); ``stale`` reports level 3.
    """
    now = _ensure_utc(now)
    status = _get(bill, "status")
    if status == "confirmed_paid":
        return None
    due = _get(bill, "due_at")
    if due is None:
        return None
    due = _ensure_utc(due)
    if status in ("disputed",):
        return 1
    if status == "stale" or due < now:
        return 3
    within = now + timedelta(days=ESCALATION_SOON_DAYS)
    if due <= now + timedelta(hours=ESCALATION_SOON_HOURS):
        return 2
    if due <= within:
        return 1
    return 0


def _get(bill: Any, name: str) -> Any:
    if isinstance(bill, dict):
        return bill.get(name)
    return getattr(bill, name, None)


def _ensure_utc(dt: Any) -> datetime:
    if dt is None:
        return datetime.now(UTC)
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _bill_id(bill: Any) -> str:
    return str(_get(bill, "id"))


def _bill_payee(bill: Any) -> str:
    return str(_get(bill, "payee") or "Unknown biller")


def _bill_amount_cents(bill: Any) -> str:
    return str(_get(bill, "amount_cents") or "0")


def _bill_due_iso(bill: Any) -> str | None:
    due = _get(bill, "due_at")
    if due is None:
        return None
    return _ensure_utc(due).isoformat()


class BillService:
    """Orchestration over :class:`BillsRepository` (no payment paths).

    **No money actions** — this class (and the whole bills workstream)
    performs no payment, transfer, or purchase. The only state that means
    "paid" is written by :meth:`confirm_paid`, which is only ever called
    from the explicit ``POST /api/bills/{id}/confirm-paid`` user action.
    """

    def __init__(self, repo: BillsRepository) -> None:
        self._repo = repo

    # -- reads ----------------------------------------------------------------

    async def list_bills(self, *, status: str | None = None) -> list[dict[str, Any]]:
        rows = await self._repo.list_bills(status=status)
        return [self._bill_dict(r) for r in rows]

    async def get_bill(self, bill_id: str) -> dict[str, Any] | None:
        row = await self._repo.get_bill(bill_id)
        return self._bill_dict(row) if row is not None else None

    async def upcoming(self, *, days: int = 14, now: datetime | None = None) -> list[dict[str, Any]]:
        rows = await self._repo.upcoming(days=days, now=now)
        return [self._bill_dict(r) for r in rows]

    async def monthly_calendar(self, month: str) -> dict[str, Any]:
        """Monthly obligation calendar grouped by day.

        Returns ``{"month": "YYYY-MM", "days": [{"date": "YYYY-MM-DD",
        "obligations": [{"payee", "amount_cents", "currency", "status",
        "bill_id"}]}]}`` — days in calendar order, only days with
        obligations (the grid renders missing days as empty cells).
        """
        rows = await self._repo.monthly_calendar(month)
        by_day: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            day = _ensure_utc(r.due_at).date().isoformat()
            by_day.setdefault(day, []).append(
                {
                    "bill_id": r.id,
                    "payee": r.payee,
                    "amount_cents": r.amount_cents,
                    "currency": r.currency,
                    "status": r.status,
                }
            )
        days = [
            {"date": d, "obligations": by_day[d]} for d in sorted(by_day)
        ]
        return {"month": month, "days": days}

    # -- explicit writes --------------------------------------------------------

    async def create_bill(self, **fields: Any) -> dict[str, Any]:
        row = await self._repo.create_bill(**fields)
        return self._bill_dict(row)

    async def update_bill(self, bill_id: str, **fields: Any) -> dict[str, Any] | None:
        row = await self._repo.update_bill(bill_id, **fields)
        return self._bill_dict(row) if row is not None else None

    async def confirm_paid(self, bill_id: str, at: datetime | None = None) -> dict[str, Any] | None:
        """Explicit user action only: record "user confirms they paid".

        This does NOT pay anything — it records the user's confirmation
        (``confirmed_paid`` + ``confirmed_at``). There is no other writer
        of the paid state anywhere in the bills plane.
        """
        row = await self._repo.mark_confirmed_paid(bill_id, at=at)
        return self._bill_dict(row) if row is not None else None

    async def dispute(self, bill_id: str) -> dict[str, Any] | None:
        row = await self._repo.mark_disputed(bill_id)
        return self._bill_dict(row) if row is not None else None

    async def sweep_stale(self, now: datetime | None = None) -> int:
        """Flag overdue, unconfirmed bills ``stale`` (never confirm)."""
        rows = await self._repo.sweep_stale(now=now)
        return len(rows)

    # -- P2-B: recurring series / missing-payment confirmation -----------------
    #
    # **Nothing here ever infers payment from time passing.** The
    # missing-payment sweep only raises a ``needs_confirmation`` flag (a
    # question: "Did you pay this?"); the stale sweep owns the
    # ``pending → stale`` transition, and ``confirm_paid`` remains the
    # sole writer of the paid state.

    @staticmethod
    def series_key_for(payee: str, currency: str = "USD") -> str:
        """Stable series identity: lower-cased payee + currency.

        The amount is intentionally NOT part of the key — a recurring
        bill's price may drift (see :meth:`series_price_increases`); the
        key identifies *who*, and the amount is compared with a tolerance
        at link time.
        """
        return f"{(payee or '').strip().lower()}::{(currency or 'USD').strip().upper()}"

    @staticmethod
    def advance_due_by_interval(
        due_at: datetime, interval: dict[str, Any]
    ) -> datetime:
        """Advance ``due_at`` by one recurrence interval (deterministic).

        ``{"months": 1}`` → next month, same day-of-month (clamped to the
        month's last day); ``{"weeks": 1}`` → +7 days; ``{"years": 1}`` →
        same date next year (Feb 29 clamps to Feb 28); no/unknown interval
        → the date is returned unchanged.
        """
        interval = interval or {}
        months = int(interval.get("months", 0) or 0)
        years = int(interval.get("years", 0) or 0)
        weeks = int(interval.get("weeks", 0) or 0)
        if (years or months) and not weeks:
            d = due_at
            if years:
                try:
                    d = d.replace(year=d.year + years)
                except ValueError:  # Feb 29 → Feb 28
                    d = d.replace(year=d.year + years, day=28)
            if months:
                y = d.year + (d.month - 1 + months) // 12
                m = (d.month - 1 + months) % 12 + 1
                try:
                    d = d.replace(year=y, month=m, day=d.day)
                except ValueError:  # e.g. Jan 31 → Feb 28
                    import calendar

                    d = d.replace(year=y, month=m, day=calendar.monthrange(y, m)[1])
            return d
        if weeks:
            return due_at + timedelta(weeks=weeks)
        return due_at

    @staticmethod
    def _interval_days(interval: dict[str, Any]) -> int | None:
        """Approximate interval length in days (for drift tolerance)."""
        interval = interval or {}
        if interval.get("days"):
            return int(interval["days"])
        if interval.get("months"):
            return int(interval["months"]) * 30
        if interval.get("weeks"):
            return int(interval["weeks"]) * 7
        if interval.get("years"):
            return int(interval["years"]) * 365
        return None

    def match_series_head(
        self,
        candidate: dict[str, Any],
        head: dict[str, Any],
        *,
        drift_hours: int = 24 * 3,
    ) -> bool:
        """P2-B recurring-recognition: does ``candidate`` match ``head``?

        True when the payee (case-insensitive) and currency match, the
        amount is within :data:`PRICE_DRIFT_TOLERANCE`, and the due date
        follows the same interval (within ``drift_hours`` of the
        head's due advanced by one interval).
        """
        if (
            (candidate.get("payee") or "").strip().lower()
            != (head.get("payee") or "").strip().lower()
        ):
            return False
        if (
            (candidate.get("currency") or "USD").strip().upper()
            != (head.get("currency") or "USD").strip().upper()
        ):
            return False
        try:
            cand_amt = int(candidate.get("amount_cents") or 0)
            head_amt = int(head.get("amount_cents") or 0)
        except (TypeError, ValueError):
            return False
        if head_amt <= 0:
            return False
        if not (
            head_amt <= cand_amt <= head_amt * PRICE_DRIFT_TOLERANCE
        ):
            return False
        head_due = _ensure_utc(head.get("due_at"))
        cand_due = _ensure_utc(candidate.get("due_at"))
        expected = self.advance_due_by_interval(
            head_due, head.get("interval") or {}
        )
        tolerance = timedelta(hours=drift_hours)
        if abs((cand_due - expected).total_seconds()) > tolerance.total_seconds():
            return False
        return True

    async def link_candidate_to_series(
        self,
        *,
        candidate_id: str,
        series_key: str,
    ) -> dict[str, Any] | None:
        """P2-B: link an accepted candidate to an existing series head.

        If the candidate's ``(payee, currency)`` match an existing head
        (same ``series_key``, no ``recurrence_of``) and its amount falls
        within :data:`PRICE_DRIFT_TOLERANCE`, the candidate is promoted
        as an *installment* (``recurrence_of`` set to the head id,
        ``series_key`` set to the series). Otherwise it becomes the
        series head itself (``recurrence_of`` = None). Returns the
        promoted bill dict; ``None`` when the candidate is absent or
        not a valid proposal.
        """
        result = await self._repo.accept_candidate(candidate_id)
        if result is None:
            return None
        candidate, bill = result
        # Look for an existing head of this series.
        head = await self._repo.find_series_head(
            series_key=series_key,
            before=_ensure_utc(bill.due_at),
        )
        if head is not None:
            cand_dict = self._bill_dict(bill)
            if self.match_series_head(cand_dict, self._bill_dict(head)):
                # Link as an installment of the head (no new head created).
                linked = await self._repo.update_bill(
                    bill.id,
                    recurrence_of=head.id,
                    series_key=series_key,
                )
                return self._bill_dict(linked) if linked is not None else None
        # No matching head: this bill becomes the head of the series.
        linked = await self._repo.update_bill(
            bill.id, series_key=series_key
        )
        return self._bill_dict(linked) if linked is not None else None

    async def sweep_missing_payment(
        self, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """P2-B: flag ``pending`` bills past ``due + grace`` for confirmation.

        This is a *question*, never an assumption — the sweep sets only the
        ``needs_confirmation`` flag; it never flips ``status`` and never
        writes the paid state. Idempotent (re-running keeps the flags,
        returns only the bills flagged this run).
        """
        now = _ensure_utc(now)
        rows = await self._repo.sweep_needs_confirmation(now=now)
        return [self._bill_dict(r) for r in rows]

    async def clear_needs_confirmation(self, bill_id: str) -> bool:
        """Clear a bill's ``needs_confirmation`` flag (explicit user action)."""
        row = await self._repo.set_needs_confirmation(bill_id, False)
        return row is not None

    async def series_price_increases(
        self,
        *,
        tolerance: float | None = None,
    ) -> list[dict[str, Any]]:
        """P2-B: recurring bills whose amount drifted above the series head.

        For every series head (``recurrence_of`` is NULL, ``series_key``
        set), compare each installment's ``amount_cents`` to the head's.
        Installments above ``head × tolerance`` (default
        :data:`PRICE_DRIFT_TOLERANCE`) are price increases — surfaced as
        notice obligations by the caller (the obligations plane owns the
        notice record; this method only *detects*).
        """
        tol = tolerance if tolerance is not None else PRICE_DRIFT_TOLERANCE
        heads = await self._repo.list_bills()
        out: list[dict[str, Any]] = []
        for head in heads:
            if not getattr(head, "series_key", None):
                continue
            if getattr(head, "recurrence_of", None) is not None:
                continue
            installments = await self._repo.list_series_members(
                recurrence_of=head.id
            )
            head_amt = int(head.amount_cents or 0)
            if head_amt <= 0:
                continue
            for inst in installments:
                inst_amt = int(inst.amount_cents or 0)
                if inst_amt > head_amt * tol:
                    out.append(
                        {
                            "head": self._bill_dict(head),
                            "installment": self._bill_dict(inst),
                            "drift_ratio": round(inst_amt / head_amt, 4),
                        }
                    )
        return out

    async def briefing(
        self,
        *,
        days: int = 30,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """P2-B: "What do I owe / when / what have I paid / what needs
        attention" — a single composite answer.

        Returns
        -------
        ``{"owowing": [...], "due_soon": [...], "paid_recently": [...],
        "needs_attention": [...], "series": [{...}], "summary": {...}}``
        """
        now = _ensure_utc(now)
        all_bills = await self._repo.list_bills()
        due_soon = await self._repo.upcoming(days=days, now=now)
        needs_confirmation = [
            r for r in all_bills if getattr(r, "needs_confirmation", False)
        ]
        paid_recently = [
            r
            for r in all_bills
            if r.status == "confirmed_paid"
            and (
                r.confirmed_at is not None
                and _ensure_utc(r.confirmed_at) >= now - timedelta(days=90)
            )
        ]
        # Series summary: group by series_key.
        series_map: dict[str, list[dict[str, Any]]] = {}
        for r in all_bills:
            sk = getattr(r, "series_key", None)
            if not sk:
                continue
            series_map.setdefault(sk, []).append(self._bill_dict(r))
        series = []
        for sk, members in series_map.items():
            head = next(
                (m for m in members if m.get("recurrence_of") is None), None
            )
            series.append(
                {
                    "series_key": sk,
                    "head": head,
                    "member_count": len(members),
                    "members": members,
                }
            )
        owing = [
            self._bill_dict(r)
            for r in all_bills
            if r.status in ("pending", "stale")
        ]
        summary = {
            "total_pending": sum(
                1 for r in all_bills if r.status == "pending"
            ),
            "total_owing": len(owing),
            "total_due_soon": len(due_soon),
            "total_needs_confirmation": len(needs_confirmation),
            "total_paid_90d": len(paid_recently),
            "total_series": len(series),
        }
        # "Owing" = anything not yet settled. ``stale`` bills (overdue, no
        # confirmation of payment) are still owed — excluding them would let
        # an overdue bill vanish from "what do I owe". ``disputed`` bills are
        # contested and belong in needs_attention, not the owing list.
        return {
            "owing": owing,
            "due_soon": [self._bill_dict(r) for r in due_soon],
            "paid_recently": [self._bill_dict(r) for r in paid_recently],
            "needs_attention": [self._bill_dict(r) for r in needs_confirmation],
            "series": series,
            "summary": summary,
        }

    # -- candidates -------------------------------------------------------------

    async def list_candidates(self, *, status: str | None = None) -> list[dict[str, Any]]:
        rows = await self._repo.list_candidates(status=status)
        return [self._candidate_dict(r) for r in rows]

    async def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        row = await self._repo.get_candidate(candidate_id)
        return self._candidate_dict(row) if row is not None else None

    async def accept_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        """Explicit user action: promote the candidate to a bill (atomic).

        Returns ``{"candidate": …, "bill": …}``; ``None`` when the
        candidate is absent or not a valid proposal.
        """
        result = await self._repo.accept_candidate(candidate_id)
        if result is None:
            return None
        candidate, bill = result
        return {
            "candidate": self._candidate_dict(candidate),
            "bill": self._bill_dict(bill),
        }

    async def reject_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        row = await self._repo.set_candidate_status(candidate_id, "rejected")
        return self._candidate_dict(row) if row is not None else None

    # -- attention mirroring ------------------------------------------------------

    async def refresh_attention(
        self,
        bill: Any,
        *,
        level: int,
        registry: Any | None = None,
    ) -> str | None:
        """Create/refresh the attention item for an escalated bill.

        Durable row first (via ``registry.attention`` — the wave-1 source
        of truth), then mirrored to the in-memory
        :mod:`backend.attention` service (C mail-bridge pattern). Returns
        the durable item id (or the in-memory id when degraded), so
        re-escalation updates the same item instead of stacking.
        """
        payee = _bill_payee(bill)
        amount = _bill_amount_cents(bill)
        due_iso = _bill_due_iso(bill)
        title = f"Bill escalation {level}: {payee} (${_fmt_amount(amount)}) due"
        body = (
            f"Bill for {payee} (${_fmt_amount(amount)})"
            + (f", due {due_iso}" if due_iso else "")
            + f" — escalation level {level} (deterministic bill ladder). "
            "NerveCenter never pays this; you confirm payment explicitly."
        )
        source_refs = [f"bill:{_bill_id(bill)}"]
        escalation_policy = {"bills.escalation_level": level}

        item_id: str | None = None
        if registry is not None:
            try:
                rows = await registry.attention.list_attention_items(state="active")
                existing = next(
                    (
                        r
                        for r in rows
                        if f"bill:{_bill_id(bill)}" in list(r.source_refs or [])
                    ),
                    None,
                )
                if existing is None:
                    row = await registry.attention.create_attention_item(
                        title=title,
                        body=body,
                        urgency=_level_urgency(level),
                        importance=_level_importance(level),
                        deadline_at=_ensure_utc(due_iso) if due_iso else None,
                        source_refs=source_refs,
                        recommended_action="Confirm payment if paid, or dispute",
                        escalation_policy=escalation_policy,
                    )
                    item_id = row.id
                else:
                    row = existing
                    row.title = title
                    row.body = body
                    row.urgency = _level_urgency(level)
                    row.importance = _level_importance(level)
                    if due_iso:
                        row.deadline_at = _ensure_utc(due_iso)
                    row.escalation_policy = escalation_policy
                    await registry.attention.set_state(row.id, "active")
                    item_id = row.id
            except Exception:  # noqa: BLE001 — degrade to in-memory only
                item_id = None
        if item_id is None:
            item_id = self._mirror_in_memory(title, body, level, due_iso, source_refs)
        return item_id

    @staticmethod
    def _mirror_in_memory(
        title: str, body: str, level: int, due_iso: str | None, source_refs: list[str]
    ) -> str:
        """Mirror into the in-memory attention queue (degraded / no registry)."""
        from backend.attention.service import get_attention_service

        service = get_attention_service()
        item = service.create(
            {
                "reason": title,
                "urgency": min(0.4 + 0.2 * level, 1.0),
                "importance": min(0.5 + 0.12 * level, 1.0),
                "due": _ensure_utc(due_iso) if due_iso else None,
                "entities": [r.split(":", 1)[-1] for r in source_refs],
                "sources": ["bills"],
                "suggested_action": "Confirm payment if paid, or dispute",
                "why": [f"bill escalation level {level} (deterministic ladder)"],
            }
        )
        return item.attention_id

    # -- projections ------------------------------------------------------------

    def _bill_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "payee": row.payee,
            "amount_cents": row.amount_cents,
            "currency": row.currency,
            "due_at": _ensure_utc(row.due_at).isoformat(),
            "recurrence": row.recurrence,
            "interval": row.interval or {},
            "status": row.status,
            "confirmed_at": (
                _ensure_utc(row.confirmed_at).isoformat()
                if row.confirmed_at is not None
                else None
            ),
            "evidence": list(row.evidence or []),
            "duplicate_of": row.duplicate_of,
            "series_key": getattr(row, "series_key", None),
            "recurrence_of": getattr(row, "recurrence_of", None),
            "needs_confirmation": bool(
                getattr(row, "needs_confirmation", False)
            ),
            "created_at": _ensure_utc(row.created_at).isoformat(),
            "updated_at": _ensure_utc(row.updated_at).isoformat(),
        }

    def _candidate_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "source_kind": row.source_kind,
            "source_id": row.source_id,
            "status": row.status,
            "extracted_fields": row.extracted_fields or {},
            "evidence": list(row.evidence or []),
            "needs_review": list(row.needs_review or []),
            "duplicate_of": row.duplicate_of,
            "bill_id": row.bill_id,
            "created_at": _ensure_utc(row.created_at).isoformat(),
            "updated_at": _ensure_utc(row.updated_at).isoformat(),
        }


def _level_urgency(level: int) -> str:
    return {0: "low", 1: "normal", 2: "high", 3: "critical"}.get(level, "normal")


def _level_importance(level: int) -> int:
    return {0: 5, 1: 6, 2: 8, 3: 9}.get(level, 6)


def _fmt_amount(cents: str) -> str:
    try:
        return f"{int(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return str(cents)


def _repo_from_ctx(registry: Any) -> "BillsRepository":
    """Build a :class:`BillsRepository` from a Today ``ctx.registry``.

    ``registry`` is the durable :class:`RepositoryRegistry` (or ``None``,
    handled by the caller). We construct the repo directly from
    ``registry.session_factory`` (the attribute documented on
    ``RepositoryRegistry``) — the bills workstream does not edit
    ``registry.py``.
    """
    from backend.persistence.repositories.bills_repo import BillsRepository

    return BillsRepository(registry.session_factory)


class BillsTodayProvider:
    """Today ``coming_up`` contributor: upcoming bills (≤ 14 days, sorted).

    The provider tolerates ``ctx.registry is None`` (returns ``[]``) per the
    frozen :class:`TodaySectionProvider` contract. Bills feed ONLY
    ``coming_up`` — ``needs_you`` is left to the attention items this
    workstream mirrors (no double-counting).
    """

    BILL_WINDOW_DAYS = 14

    async def name(self) -> str:
        return "bills"

    async def needs_you(self, ctx: Any) -> list[dict[str, Any]]:
        return []

    async def coming_up(self, ctx: Any) -> list[dict[str, Any]]:
        if ctx is None or getattr(ctx, "registry", None) is None:
            return []
        try:
            repo = _repo_from_ctx(ctx.registry)
            rows = await repo.upcoming(days=self.BILL_WINDOW_DAYS, now=ctx.now)
        except Exception:  # noqa: BLE001 — degrade to no bills (house contract)
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            due = _ensure_utc(r.due_at)
            out.append(
                {
                    "id": r.id,
                    "kind": "bill",
                    "title": f"{r.payee} ({_fmt_amount(r.amount_cents)} {r.currency})",
                    "deadline_at": due.isoformat(),
                    "amount_cents": r.amount_cents,
                    "currency": r.currency,
                    "status": r.status,
                }
            )
        out.sort(key=lambda it: it["deadline_at"])
        return out

    async def handled(self, ctx: Any) -> list[dict[str, Any]]:
        if ctx is None or getattr(ctx, "registry", None) is None:
            return []
        try:
            repo = _repo_from_ctx(ctx.registry)
            rows = await repo.list_bills(status="confirmed_paid")
        except Exception:  # noqa: BLE001
            return []
        since = ctx.now - timedelta(hours=24)
        out: list[dict[str, Any]] = []
        for r in rows:
            confirmed = _ensure_utc(r.confirmed_at)
            if confirmed is not None and confirmed >= since:
                out.append(
                    {
                        "id": r.id,
                        "kind": "bill",
                        "title": f"{r.payee} ({_fmt_amount(r.amount_cents)} {r.currency})",
                        "state": "confirmed_paid",
                        "at": confirmed.isoformat(),
                    }
                )
        return out


def register_bills_today_provider() -> BillsTodayProvider:
    """Register :class:`BillsTodayProvider` with the Today aggregator.

    The integrator calls this at startup (``today`` wiring); the bills
    package never edits ``backend/api/endpoints/today.py``. Idempotent —
    the aggregator dedupes by provider identity.
    """
    from backend.api.endpoints import today

    provider = BillsTodayProvider()
    today.register_today_provider(provider)
    return provider


__all__ = [
    "BillService",
    "BillsTodayProvider",
    "ESCALATION_SOON_DAYS",
    "ESCALATION_SOON_HOURS",
    "bill_escalation_level",
    "register_bills_today_provider",
]
