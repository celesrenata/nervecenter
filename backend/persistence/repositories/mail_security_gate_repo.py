"""Held-promotion store and release pass for the pre-promotion gate (Task 7.4).

* :class:`HeldPromotionRepository.hold` is get-or-create on
  ``(target_kind, target_id)``: a replayed ingest never stacks a second hold
  and never re-holds an already RELEASED or REVIEW_CANDIDATE item.
* :func:`release_pass` reads each open hold's newest verdict through the
  job's exact ``content_ref`` in the hold's own scope and either replays the
  original write with the same id (SAFE / LOW_PRIORITY), marks it a review
  candidate (REVIEW / RESTRICTED / INCOMPLETE), or leaves it HELD (no
  verdict). The target write happens before the status mark, and both target
  writers are get-or-create by id, so a crash between them is repaired by the
  next pass without a duplicate.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from backend.mail_security.trust_gate import (
    GateDecision,
    HoldStatus,
    TargetKind,
    decide,
    decode_payload,
)
from backend.persistence.models.mail_security import (
    MailSecurityAssessmentRow,
    MailSecurityHeldPromotionRow,
    MailSecurityJobRow,
)

DEFAULT_RELEASE_BATCH = 500
_OPEN = (HoldStatus.HELD.value, HoldStatus.REVIEW_CANDIDATE.value)


@dataclass(frozen=True)
class HoldWrite:
    row: MailSecurityHeldPromotionRow
    created: bool


class HeldPromotionRepository:
    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def hold(
        self,
        *,
        account_scope: str,
        content_ref: str,
        communication_id: str,
        target_kind: str,
        target_id: str,
        payload: dict[str, Any],
    ) -> HoldWrite:
        async with self._sessions() as session:
            stmt = select(MailSecurityHeldPromotionRow).where(
                MailSecurityHeldPromotionRow.target_kind == target_kind,
                MailSecurityHeldPromotionRow.target_id == target_id,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                return HoldWrite(existing, False)
            row = MailSecurityHeldPromotionRow(
                account_scope=account_scope,
                content_ref=content_ref,
                communication_id=communication_id,
                target_kind=target_kind,
                target_id=target_id,
                payload=payload,
                status=HoldStatus.HELD.value,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return HoldWrite(row, True)

    async def list_open(self, *, limit: int = DEFAULT_RELEASE_BATCH) -> list[Any]:
        async with self._sessions() as session:
            stmt = (
                select(MailSecurityHeldPromotionRow)
                .where(MailSecurityHeldPromotionRow.status.in_(_OPEN))
                .order_by(MailSecurityHeldPromotionRow.created_at, MailSecurityHeldPromotionRow.id)
                .limit(limit)
            )
            return list((await session.execute(stmt)).scalars().all())

    async def list_for_scope(self, account_scope: str) -> list[Any]:
        async with self._sessions() as session:
            stmt = select(MailSecurityHeldPromotionRow).where(
                MailSecurityHeldPromotionRow.account_scope == account_scope
            )
            return list((await session.execute(stmt)).scalars().all())

    async def mark(
        self,
        row_id: str,
        *,
        status: HoldStatus,
        disposition: str | None,
        assessment_id: str | None,
        at: datetime,
    ) -> None:
        async with self._sessions() as session:
            row = await session.get(MailSecurityHeldPromotionRow, row_id)
            if row is None:
                return
            row.status = status.value
            row.decided_disposition = disposition
            row.decided_assessment_id = assessment_id
            row.decided_at = at
            await session.commit()

    async def latest_verdict(self, account_scope: str, content_ref: str) -> tuple[str, str] | None:
        """``(disposition, assessment_id)`` of the newest assessment for the
        job scheduled for this exact ref in this scope, else ``None``."""
        async with self._sessions() as session:
            source_key = (
                await session.execute(
                    select(MailSecurityJobRow.source_key)
                    .where(
                        MailSecurityJobRow.account_scope == account_scope,
                        MailSecurityJobRow.meta["content_ref"].as_string() == content_ref,
                    )
                    .order_by(MailSecurityJobRow.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if source_key is None:
                return None
            row = (
                await session.execute(
                    select(
                        MailSecurityAssessmentRow.disposition,
                        MailSecurityAssessmentRow.assessment_id,
                    )
                    .where(
                        MailSecurityAssessmentRow.account_scope == account_scope,
                        MailSecurityAssessmentRow.source_id == source_key,
                    )
                    .order_by(
                        MailSecurityAssessmentRow.revision.desc(),
                        MailSecurityAssessmentRow.created_at.desc(),
                    )
                    .limit(1)
                )
            ).first()
            return None if row is None else (row[0], row[1])


async def _write_target(row: Any, *, attention: Any, commitments: Any) -> None:
    columns = decode_payload(row.payload or {})
    if row.target_kind == TargetKind.ATTENTION.value:
        await attention.create_attention_item(id=row.target_id, **columns)
    elif row.target_kind == TargetKind.COMMITMENT.value:
        await commitments.create_commitment(id=row.target_id, **columns)
    else:  # pragma: no cover - closed set enforced at hold time
        raise ValueError("unknown target kind")


async def release_pass(
    holds: HeldPromotionRepository,
    *,
    attention: Any,
    commitments: Any,
    now: datetime | None = None,
    limit: int = DEFAULT_RELEASE_BATCH,
) -> dict[str, int]:
    """One bounded pass over open holds. Returns counts by outcome code."""
    clock = now or datetime.now(UTC)
    counts: Counter[str] = Counter()
    for row in await holds.list_open(limit=limit):
        verdict = await holds.latest_verdict(row.account_scope, row.content_ref)
        disposition, assessment_id = verdict if verdict else (None, None)
        decision = decide(disposition)
        if decision is GateDecision.WAIT:
            counts["waiting"] += 1
            continue
        if decision is GateDecision.RELEASE:
            await _write_target(row, attention=attention, commitments=commitments)
            await holds.mark(
                row.id,
                status=HoldStatus.RELEASED,
                disposition=disposition,
                assessment_id=assessment_id,
                at=clock,
            )
            counts["released"] += 1
            continue
        if row.status != HoldStatus.REVIEW_CANDIDATE.value or (
            row.decided_assessment_id != assessment_id
        ):
            await holds.mark(
                row.id,
                status=HoldStatus.REVIEW_CANDIDATE,
                disposition=disposition,
                assessment_id=assessment_id,
                at=clock,
            )
            counts["review_candidate"] += 1
        else:
            counts["unchanged"] += 1
    return dict(counts)


__all__ = [
    "DEFAULT_RELEASE_BATCH",
    "HeldPromotionRepository",
    "HoldWrite",
    "release_pass",
]
