"""Durable per-account expectedness counts (NS-ADR-075). Counts only."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from backend.persistence.models.mail_security import (
    MailExpectednessCountRow,
    MailExpectednessFeedbackRow,
)


class MailExpectednessRepository:
    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def counts(self, account_scope: str, sender_key: str) -> dict[str, float]:
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(
                        MailExpectednessCountRow.request_type, MailExpectednessCountRow.count
                    ).where(
                        MailExpectednessCountRow.account_scope == account_scope,
                        MailExpectednessCountRow.sender_key == sender_key,
                    )
                )
            ).all()
        return {str(t): float(n) for t, n in rows}

    async def add(self, account_scope: str, sender_key: str, request_types: Iterable[str]) -> None:
        for request_type in request_types:
            async with self._sessions() as s:
                res = await s.execute(
                    update(MailExpectednessCountRow)
                    .where(
                        MailExpectednessCountRow.account_scope == account_scope,
                        MailExpectednessCountRow.sender_key == sender_key,
                        MailExpectednessCountRow.request_type == request_type,
                    )
                    .values(count=MailExpectednessCountRow.count + 1)
                )
                if not res.rowcount:
                    s.add(
                        MailExpectednessCountRow(
                            account_scope=account_scope,
                            sender_key=sender_key,
                            request_type=request_type,
                            count=1,
                        )
                    )
                try:
                    await s.commit()
                except IntegrityError:  # a concurrent first insert: count it
                    await s.rollback()
                    await self.add(account_scope, sender_key, [request_type])

    # -- feedback (NS-ADR-078) -----------------------------------------------

    async def add_feedback(
        self,
        *,
        feedback_id: str,
        account_scope: str,
        assessment_id: str,
        source_key: str,
        sender_key: str,
        request_type: str,
        weight: int,
        expires_at: datetime,
        reviewer_ref: str,
    ) -> tuple[MailExpectednessFeedbackRow, bool]:
        """Append one feedback; an identical ``feedback_id`` replay returns the
        existing row (``created`` False)."""
        async with self._sessions() as s:
            existing = (
                await s.execute(
                    select(MailExpectednessFeedbackRow).where(
                        MailExpectednessFeedbackRow.feedback_id == feedback_id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing, False
            row = MailExpectednessFeedbackRow(
                feedback_id=feedback_id, account_scope=account_scope,
                assessment_id=assessment_id, source_key=source_key, sender_key=sender_key,
                request_type=request_type, weight=weight, expires_at=expires_at,
                reviewer_ref=reviewer_ref,
            )
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return await self.add_feedback(
                    feedback_id=feedback_id, account_scope=account_scope,
                    assessment_id=assessment_id, source_key=source_key,
                    sender_key=sender_key, request_type=request_type, weight=weight,
                    expires_at=expires_at, reviewer_ref=reviewer_ref,
                )
            await s.refresh(row)
            return row, True

    async def revoke_feedback(
        self, account_scopes: Iterable[str], feedback_id: str, *, now: datetime
    ) -> MailExpectednessFeedbackRow | None:
        """Undo: mark revoked (kept for audit). Scope-bound; ``None`` if absent."""
        async with self._sessions() as s:
            row = (
                await s.execute(
                    select(MailExpectednessFeedbackRow).where(
                        MailExpectednessFeedbackRow.feedback_id == feedback_id,
                        MailExpectednessFeedbackRow.account_scope.in_(sorted(account_scopes)),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.revoked_at is None:
                row.revoked_at = now
                await s.commit()
                await s.refresh(row)
            return row

    async def live_feedback_weights(
        self, account_scope: str, sender_key: str, *, now: datetime
    ) -> dict[str, float]:
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(
                        MailExpectednessFeedbackRow.request_type,
                        MailExpectednessFeedbackRow.weight,
                    ).where(
                        MailExpectednessFeedbackRow.account_scope == account_scope,
                        MailExpectednessFeedbackRow.sender_key == sender_key,
                        MailExpectednessFeedbackRow.revoked_at.is_(None),
                        MailExpectednessFeedbackRow.expires_at > now,
                    )
                )
            ).all()
        out: dict[str, float] = {}
        for t, w in rows:
            out[str(t)] = out.get(str(t), 0.0) + float(w)
        return out

    async def list_feedback(
        self, account_scopes: Iterable[str], *, limit: int = 100
    ) -> list[MailExpectednessFeedbackRow]:
        async with self._sessions() as s:
            return list(
                (
                    await s.execute(
                        select(MailExpectednessFeedbackRow)
                        .where(
                            MailExpectednessFeedbackRow.account_scope.in_(sorted(account_scopes))
                        )
                        .order_by(MailExpectednessFeedbackRow.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            )

    async def forget(self, account_scope: str) -> int:
        """Erase everything learned for one account."""
        async with self._sessions() as s:
            res = await s.execute(
                delete(MailExpectednessCountRow).where(
                    MailExpectednessCountRow.account_scope == account_scope
                )
            )
            await s.commit()
        return int(res.rowcount or 0)


__all__ = ["MailExpectednessRepository"]
