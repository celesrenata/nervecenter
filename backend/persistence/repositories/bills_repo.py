"""Bills-plane repository: bills + bill candidates (workstream G).

Durable access to :class:`~backend.persistence.models.bills.BillRow` and
:class:`~backend.persistence.models.bills.BillCandidateRow`. Built from the
session factory exposed by :class:`RepositoryRegistry`
(``registry.session_factory``) — this module never edits ``registry.py``.

Hard rules implemented here (spec §7):

- **Never assume a bill was paid.** ``mark_confirmed_paid`` is the ONLY
  writer of ``status = "confirmed_paid"`` and it exists to be called by an
  explicit user action (the ``POST /api/bills/{id}/confirm-paid`` endpoint).
  The stale sweep (``sweep_stale``) NEVER confirms — a passed due date keeps
  the bill ``pending``→``stale``.
- **Candidate → bill promotion is explicit.** ``accept_candidate`` is
  atomic (single transaction): the candidate flips ``accepted``, the bill is
  created, and the link is recorded. There is no automatic promotion.
- **Duplicate detection** is the same-payee + same-amount-within-tolerance
  + same-due-window query; it links ``candidate.duplicate_of`` instead of
  silently merging.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select

from ..models.bills import (
    BILL_RECURRENCES,
    BILL_STATUSES,
    CANDIDATE_STATUSES,
    BillCandidateRow,
    BillRow,
)


def _to_dict(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def _require(value: str, allowed: tuple[str, ...], what: str) -> None:
    if value not in allowed:
        raise ValueError(f"invalid {what}: {value!r}")


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, str):
        try:
            return _ensure_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _field_value(fields: dict[str, Any], name: str) -> Any:
    """Read a field from an ``extracted_fields`` map (``{value, confidence}``)."""
    entry = fields.get(name)
    if isinstance(entry, dict):
        return entry.get("value")
    return entry


class BillsRepository:
    """Durable access to :class:`BillRow` and :class:`BillCandidateRow`."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    # -- bills --------------------------------------------------------------

    async def create_bill(
        self,
        *,
        payee: str,
        amount_cents: str | int,
        due_at: datetime,
        currency: str = "USD",
        recurrence: str = "one-time",
        interval: dict[str, Any] | None = None,
        status: str = "pending",
        evidence: list[dict[str, Any]] | None = None,
        duplicate_of: str | None = None,
    ) -> BillRow:
        """Create a bill. ``status`` is caller-supplied but validated.

        A *new* bill is created ``pending`` (the default); writing
        ``confirmed_paid`` here is only valid when the caller is the
        explicit user confirm action.
        """
        _require(recurrence, BILL_RECURRENCES, "bill recurrence")
        _require(status, BILL_STATUSES, "bill status")
        async with self._sessions() as session:
            row = BillRow(
                payee=payee,
                amount_cents=str(int(amount_cents)),
                currency=currency,
                due_at=_ensure_utc(due_at) or due_at,
                recurrence=recurrence,
                interval=interval or {},
                status=status,
                evidence=evidence or [],
                duplicate_of=duplicate_of,
            )
            session.add(row)
            await session.commit()
            return row

    async def get_bill(self, bill_id: str) -> BillRow | None:
        async with self._sessions() as session:
            return await session.get(BillRow, bill_id)

    async def list_bills(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[BillRow]:
        async with self._sessions() as session:
            stmt = select(BillRow).order_by(BillRow.due_at.asc())
            if status is not None:
                stmt = stmt.where(BillRow.status == status)
            stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # Patchable columns for ``PATCH /api/bills/{id}`` (frozen whitelist).
    _PATCHABLE = (
        "payee",
        "amount_cents",
        "currency",
        "due_at",
        "recurrence",
        "interval",
        "status",
        "evidence",
        "duplicate_of",
    )

    async def update_bill(
        self, bill_id: str, **fields: Any
    ) -> BillRow | None:
        """PATCH a bill. Only whitelisted columns are accepted.

        Setting ``status = "confirmed_paid"`` via PATCH is allowed ONLY
        because the caller (the endpoint) is the explicit user action; the
        repository itself never flips status on its own.
        """
        if "status" in fields and fields["status"] is not None:
            _require(fields["status"], BILL_STATUSES, "bill status")
        if "recurrence" in fields and fields["recurrence"] is not None:
            _require(fields["recurrence"], BILL_RECURRENCES, "bill recurrence")
        unknown = set(fields) - set(self._PATCHABLE)
        if unknown:
            raise ValueError(f"bill field(s) not patchable: {sorted(unknown)}")
        async with self._sessions() as session:
            row = await session.get(BillRow, bill_id)
            if row is None:
                return None
            for key, value in fields.items():
                if value is None:
                    continue  # null = "leave as-is" (partial patch semantics)
                if key == "amount_cents":
                    value = str(int(value))
                setattr(row, key, value)
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return row

    async def mark_confirmed_paid(
        self, bill_id: str, at: datetime | None = None
    ) -> BillRow | None:
        """Explicit user action ONLY: set ``confirmed_paid`` + ``confirmed_at``.

        This is the sole writer of the paid state. Nothing else in the
        system (stale sweep, scans, escalation) ever writes the paid state —
        a passed due date leaves the bill ``pending`` (→ ``stale``). Returns
        ``None`` when the bill does not exist. Idempotent: re-confirming
        keeps the original ``confirmed_at``.
        """
        async with self._sessions() as session:
            row = await session.get(BillRow, bill_id)
            if row is None:
                return None
            if row.status != "confirmed_paid":
                row.status = "confirmed_paid"
                row.confirmed_at = _ensure_utc(at) or datetime.now(UTC)
                row.updated_at = datetime.now(UTC)
            await session.commit()
            return row

    async def mark_disputed(self, bill_id: str) -> BillRow | None:
        async with self._sessions() as session:
            row = await session.get(BillRow, bill_id)
            if row is None:
                return None
            row.status = "disputed"
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return row

    # -- upcoming / calendar windows -----------------------------------------

    async def upcoming(
        self, *, days: int = 14, now: datetime | None = None
    ) -> list[BillRow]:
        """Bills due within ``[now, now + days]`` (sorted by due_at).

        Only bills that are not ``confirmed_paid`` count as "upcoming
        obligations" — confirmed bills are done, not upcoming.
        """
        now = _ensure_utc(now) or datetime.now(UTC)
        async with self._sessions() as session:
            hi = now + timedelta(days=days)
            stmt = (
                select(BillRow)
                .where(
                    and_(
                        BillRow.status != "confirmed_paid",
                        BillRow.due_at >= now,
                        BillRow.due_at <= hi,
                    )
                )
                .order_by(BillRow.due_at.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def monthly_calendar(self, month: str) -> list[BillRow]:
        """Bills whose due date falls inside ``YYYY-MM`` (sorted by due_at).

        ``month`` is ``"YYYY-MM"``. The endpoint groups the rows by day into
        ``{date, payee, amount, status}``.
        """
        try:
            y, m = month.split("-")
            year, month_num = int(y), int(m)
            first = date(year, month_num, 1)
        except (ValueError, TypeError):
            raise ValueError(f"month must be YYYY-MM, got {month!r}")
        nxt = date(year + 1, 1, 1) if month_num == 12 else date(year, month_num + 1, 1)
        lo = datetime(first.year, first.month, first.day, tzinfo=UTC)
        hi = datetime(nxt.year, nxt.month, nxt.day, tzinfo=UTC)
        async with self._sessions() as session:
            stmt = (
                select(BillRow)
                .where(and_(BillRow.due_at >= lo, BillRow.due_at < hi))
                .order_by(BillRow.due_at.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # -- stale sweep (NEVER auto-confirms) ------------------------------------

    async def sweep_stale(self, now: datetime | None = None) -> list[BillRow]:
        """Flag overdue, unconfirmed bills ``stale`` — never ``confirmed_paid``.

        A bill whose due date has passed without an explicit confirmation is
        flagged ``stale`` so the UI/attention planes can say "this was due
        and we have no proof of payment". ``pending`` → ``stale``; rows
        already ``confirmed_paid`` or ``disputed`` are untouched. Returns
        the newly-flagged rows.
        """
        now = _ensure_utc(now) or datetime.now(UTC)
        async with self._sessions() as session:
            stmt = select(BillRow).where(
                and_(BillRow.status == "pending", BillRow.due_at < now)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            for row in rows:
                row.status = "stale"
                row.updated_at = datetime.now(UTC)
            if rows:
                await session.commit()
            return rows

    # -- candidates ----------------------------------------------------------

    async def create_candidate(
        self,
        *,
        source_kind: str,
        source_id: str,
        extracted_fields: dict[str, Any],
        evidence: list[dict[str, Any]],
        needs_review: list[str] | None = None,
        status: str = "proposed",
    ) -> BillCandidateRow:
        _require(status, CANDIDATE_STATUSES, "candidate status")
        async with self._sessions() as session:
            row = BillCandidateRow(
                source_kind=source_kind,
                source_id=source_id,
                status=status,
                extracted_fields=extracted_fields,
                evidence=evidence,
                needs_review=needs_review or [],
            )
            session.add(row)
            await session.commit()
            return row

    async def get_candidate(self, candidate_id: str) -> BillCandidateRow | None:
        async with self._sessions() as session:
            return await session.get(BillCandidateRow, candidate_id)

    async def list_candidates(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[BillCandidateRow]:
        async with self._sessions() as session:
            stmt = (
                select(BillCandidateRow).order_by(
                    BillCandidateRow.created_at.desc(), BillCandidateRow.id
                )
            )
            if status is not None:
                stmt = stmt.where(BillCandidateRow.status == status)
            stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def set_candidate_status(
        self, candidate_id: str, status: str
    ) -> BillCandidateRow | None:
        _require(status, CANDIDATE_STATUSES, "candidate status")
        async with self._sessions() as session:
            row = await session.get(BillCandidateRow, candidate_id)
            if row is None:
                return None
            row.status = status
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return row

    # -- explicit accept → bill promotion (atomic) ---------------------------

    async def accept_candidate(
        self, candidate_id: str
    ) -> tuple[BillCandidateRow, BillRow] | None:
        """Accept a candidate: promote it to a bill in ONE transaction.

        Returns ``(candidate, bill)``. ``None`` when the candidate does not
        exist, is not ``proposed`` (already accepted/rejected), or its
        extracted fields lack a usable payee + amount + due date (the user
        must review it first). Promotion is an EXPLICIT user action — never
        called automatically.
        """
        async with self._sessions() as session:
            candidate = await session.get(BillCandidateRow, candidate_id)
            if candidate is None or candidate.status != "proposed":
                return None
            fields = candidate.extracted_fields or {}
            payee = _field_value(fields, "payee")
            amount = _field_value(fields, "amount_cents")
            # The extractor emits the due date under ``due_date``; accept a
            # ``due_at`` spelling too (hand-authored candidates).
            due = _parse_iso(
                _field_value(fields, "due_date")
                if "due_date" in fields
                else _field_value(fields, "due_at")
            )
            if payee is None or amount is None or due is None:
                # Missing required fields: stays a needs-review proposal.
                return None
            try:
                amount_cents = str(int(amount))
            except (TypeError, ValueError):
                return None
            recurrence = str(_field_value(fields, "recurrence") or "one-time")
            _require(recurrence, BILL_RECURRENCES, "bill recurrence")
            interval_raw = _field_value(fields, "interval")
            bill = BillRow(
                payee=str(payee),
                amount_cents=amount_cents,
                currency=str(_field_value(fields, "currency") or "USD"),
                due_at=due,
                recurrence=recurrence,
                interval=dict(interval_raw) if isinstance(interval_raw, dict) else {},
                status="pending",
                evidence=list(candidate.evidence or []),
            )
            session.add(bill)
            await session.flush()  # assign bill.id
            candidate.status = "accepted"
            candidate.bill_id = bill.id
            candidate.updated_at = datetime.now(UTC)
            await session.commit()
            return candidate, bill

    # -- duplicate detection --------------------------------------------------

    async def find_duplicate_candidate(
        self,
        *,
        payee: str,
        amount_cents: str | int,
        due_at: datetime,
        tolerance_cents: int = 1,
        window_days: int = 3,
    ) -> BillCandidateRow | None:
        """The earliest PROPOSED candidate matching payee + amount ± tol +
        due date within ± ``window_days``.

        Same payee + same amount (within ± ``tolerance_cents``) + due date
        within the window → a duplicate. The caller links
        ``candidate.duplicate_of`` to the returned row.
        """
        amount = int(amount_cents)
        lo = amount - tolerance_cents
        hi = amount + tolerance_cents
        due = _ensure_utc(due_at)
        if due is None:
            return None
        due_lo = due - timedelta(days=window_days)
        due_hi = due + timedelta(days=window_days)
        async with self._sessions() as session:
            result = await session.execute(
                select(BillCandidateRow)
                .where(BillCandidateRow.status == "proposed")
                .order_by(BillCandidateRow.created_at.asc(), BillCandidateRow.id)
            )
            for row in result.scalars().all():
                fields = row.extracted_fields or {}
                rp = _field_value(fields, "payee")
                ra = _field_value(fields, "amount_cents")
                rd = _parse_iso(
                    _field_value(fields, "due_date")
                    if "due_date" in fields
                    else _field_value(fields, "due_at")
                )
                if rp is None or ra is None or rd is None:
                    continue
                if str(rp).strip().lower() != payee.strip().lower():
                    continue
                try:
                    ra_i = int(ra)
                except (TypeError, ValueError):
                    continue
                if not (lo <= ra_i <= hi):
                    continue
                if not (due_lo <= rd <= due_hi):
                    continue
                return row
            return None

    async def set_candidate_duplicate_of(
        self, candidate_id: str, duplicate_of: str | None
    ) -> BillCandidateRow | None:
        async with self._sessions() as session:
            row = await session.get(BillCandidateRow, candidate_id)
            if row is None:
                return None
            row.duplicate_of = duplicate_of
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return row

    async def candidate_exists_for_source(
        self, source_kind: str, source_id: str
    ) -> bool:
        """Idempotency guard: has this source already produced a candidate?"""
        async with self._sessions() as session:
            result = await session.execute(
                select(BillCandidateRow.id).where(
                    and_(
                        BillCandidateRow.source_kind == source_kind,
                        BillCandidateRow.source_id == source_id,
                    )
                )
            )
            return result.first() is not None

    @staticmethod
    def to_dict(row: Any) -> dict[str, Any]:
        return _to_dict(row)


__all__ = ["BillsRepository"]
