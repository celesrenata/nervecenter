"""Durable per-account expectedness counts (NS-ADR-075). Counts only."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from backend.persistence.models.mail_security import MailExpectednessCountRow


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
