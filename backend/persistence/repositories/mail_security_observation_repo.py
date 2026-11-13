"""Durable SHADOW semantic observations (NS-ADR-073). Enum codes only."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.persistence.models.mail_security import MailSemanticObservationRow


class MailObservationRepository:
    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def exists(self, account_scope: str, source_key: str, revision: int) -> bool:
        async with self._sessions() as s:
            row = (
                await s.execute(
                    select(MailSemanticObservationRow.id).where(
                        MailSemanticObservationRow.account_scope == account_scope,
                        MailSemanticObservationRow.source_key == source_key,
                        MailSemanticObservationRow.revision == revision,
                    )
                )
            ).first()
        return row is not None

    async def record(
        self,
        account_scope: str,
        *,
        source_key: str,
        revision: int,
        status: str,
        prompt_version: str | None = None,
        route_digest: str | None = None,
        attempts: int = 0,
        latency_ms: int | None = None,
        observation: dict[str, Any] | None = None,
    ) -> bool:
        """Insert once per (scope, source, revision); a replay is a no-op."""
        async with self._sessions() as s:
            s.add(
                MailSemanticObservationRow(
                    account_scope=account_scope,
                    source_key=source_key,
                    revision=revision,
                    status=status[:48],
                    prompt_version=prompt_version,
                    route_digest=(route_digest or None) and route_digest[:80],
                    attempts=int(attempts),
                    latency_ms=latency_ms,
                    observation=observation or {},
                )
            )
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return False
        return True

    async def status_counts(self, account_scope: str | None = None) -> dict[str, int]:
        async with self._sessions() as s:
            stmt = select(
                MailSemanticObservationRow.status, func.count(MailSemanticObservationRow.id)
            ).group_by(MailSemanticObservationRow.status)
            if account_scope is not None:
                stmt = stmt.where(MailSemanticObservationRow.account_scope == account_scope)
            rows = (await s.execute(stmt)).all()
        return {str(st): int(n) for st, n in rows}


__all__ = ["MailObservationRepository"]
