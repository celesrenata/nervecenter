"""Durable store for Task 7.6 confirmations (scope-enforced)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.mail_security.confirmations import Confirmation, RiskClass, Status
from backend.persistence.models.mail_security import MailSecurityConfirmationRow


class StepUpReused(Exception):
    """The step-up proof id was already used by another approval."""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def to_confirmation(row: Any) -> Confirmation:
    return Confirmation(
        confirmation_id=row.confirmation_id,
        account_scope=row.account_scope,
        proposal_id=row.proposal_id,
        proposal_hash="",
        execution_hash=row.execution_hash,
        action=row.action,
        risk=RiskClass(row.risk),
        target=row.target,
        requested_by=row.requested_by,
        created_at=_aware(row.created_at) if row.created_at else datetime.now(UTC),
        expires_at=_aware(row.expires_at),
        status=Status(row.status),
        approval_id=(row.approval or {}).get("approval_id"),
        step_up_proof_id=row.step_up_proof_id,
        sender_verified=bool(row.sender_verified),
        reject_reason=row.reject_reason,
        audit=tuple(row.audit or ()),
    )


class ConfirmationRepository:
    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def create(self, c: Confirmation, *, proposal: dict[str, Any]) -> None:
        async with self._sessions() as s:
            s.add(
                MailSecurityConfirmationRow(
                    confirmation_id=c.confirmation_id,
                    account_scope=c.account_scope,
                    proposal_id=c.proposal_id,
                    proposal=proposal,
                    execution_hash=c.execution_hash,
                    action=c.action,
                    risk=c.risk.value,
                    target=c.target,
                    requested_by=c.requested_by,
                    expires_at=c.expires_at,
                    status=c.status.value,
                    audit=list(c.audit),
                )
            )
            await s.commit()

    async def get(self, account_scope: str, confirmation_id: str) -> Any | None:
        async with self._sessions() as s:
            stmt = select(MailSecurityConfirmationRow).where(
                MailSecurityConfirmationRow.account_scope == account_scope,
                MailSecurityConfirmationRow.confirmation_id == confirmation_id,
            )
            return (await s.execute(stmt)).scalar_one_or_none()

    async def list(self, account_scope: str) -> list[Any]:
        async with self._sessions() as s:
            stmt = (
                select(MailSecurityConfirmationRow)
                .where(MailSecurityConfirmationRow.account_scope == account_scope)
                .order_by(MailSecurityConfirmationRow.created_at.desc())
                .limit(200)
            )
            return list((await s.execute(stmt)).scalars().all())

    async def save(self, c: Confirmation, *, approval: dict[str, Any] | None = None) -> None:
        """Persist a transition; a reused step-up proof raises StepUpReused."""
        async with self._sessions() as s:
            stmt = select(MailSecurityConfirmationRow).where(
                MailSecurityConfirmationRow.account_scope == c.account_scope,
                MailSecurityConfirmationRow.confirmation_id == c.confirmation_id,
            )
            row = (await s.execute(stmt)).scalar_one()
            row.status = c.status.value
            row.step_up_proof_id = c.step_up_proof_id
            row.sender_verified = c.sender_verified
            row.reject_reason = c.reject_reason
            row.audit = list(c.audit)
            if approval is not None:
                row.approval = approval
            try:
                await s.commit()
            except IntegrityError as exc:
                await s.rollback()
                raise StepUpReused from exc


__all__ = ["ConfirmationRepository", "StepUpReused", "to_confirmation"]
