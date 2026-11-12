"""Durable conversation ledger (NS-ADR-067).

Scope-bound: every read and write names the account scope, and fingerprints
are salted per account, so one account's ledger can never answer for
another's. Stores fingerprints, registrable domains, status and signal codes
and the opaque body ``content_ref`` only.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.mail_security.thread_ledger import (
    Entry,
    Observed,
    Result,
    assess,
    fingerprint,
)
from backend.persistence.models.mail_security import MailThreadLedgerRow

_OUT = "OUT"
_IN = "IN"


def _entry(row: MailThreadLedgerRow) -> Entry:
    return Entry(
        thread_fp=row.thread_fp,
        outgoing=row.direction == _OUT,
        sender_domain=row.sender_domain,
        reply_to_domain=row.reply_to_domain,
        participant_domains=tuple(row.participant_domains or ()),
    )


class MailThreadLedgerRepository:
    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def record(
        self,
        account_scope: str,
        salt: bytes,
        msg: Observed,
        *,
        content_ref: str | None = None,
        assess_inbound: bool = True,
    ) -> Result | None:
        """Assess (inbound only) and record one message; idempotent.

        Returns the SHADOW result for a newly recorded inbound message, or
        ``None`` for outgoing mail, a replay, a seed write
        (``assess_inbound=False``) or a lost insert race.
        """
        own = fingerprint(salt, msg.message_id)
        parents = [fingerprint(salt, p) for p in msg.parent_ids]
        async with self._sessions() as s:
            exists = (
                await s.execute(
                    select(MailThreadLedgerRow.id).where(
                        MailThreadLedgerRow.account_scope == account_scope,
                        MailThreadLedgerRow.message_fp == own,
                    )
                )
            ).first()
            if exists is not None:
                return None
            thread_fp: str | None = None
            if parents:
                found = {
                    r.message_fp: r.thread_fp
                    for r in (
                        await s.execute(
                            select(MailThreadLedgerRow).where(
                                MailThreadLedgerRow.account_scope == account_scope,
                                MailThreadLedgerRow.message_fp.in_(parents),
                            )
                        )
                    ).scalars()
                }
                # References are root-first: the first known parent names the thread.
                thread_fp = next((found[p] for p in parents if p in found), None)
            result: Result | None = None
            if not msg.outgoing and assess_inbound:
                thread = None
                if thread_fp is not None:
                    thread = [
                        _entry(r)
                        for r in (
                            await s.execute(
                                select(MailThreadLedgerRow).where(
                                    MailThreadLedgerRow.account_scope == account_scope,
                                    MailThreadLedgerRow.thread_fp == thread_fp,
                                )
                            )
                        ).scalars()
                    ]
                total = (
                    await s.execute(
                        select(func.count(MailThreadLedgerRow.id)).where(
                            MailThreadLedgerRow.account_scope == account_scope
                        )
                    )
                ).scalar_one()
                result = assess(msg, thread, ledger_empty=not total)
            s.add(
                MailThreadLedgerRow(
                    account_scope=account_scope,
                    message_fp=own,
                    thread_fp=thread_fp or own,
                    direction=_OUT if msg.outgoing else _IN,
                    sender_domain=msg.sender_domain,
                    reply_to_domain=msg.reply_to_domain,
                    participant_domains=list(msg.participant_domains),
                    status=result.status.value if result else None,
                    signals=[sig.value for sig in result.signals] if result else [],
                    content_ref=content_ref,
                )
            )
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return None
            return result

    async def flagged_by_ref(self, account_scope: str, statuses: list[str]) -> dict[str, str]:
        """``content_ref -> status`` for inbound rows with a flagged status."""
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(MailThreadLedgerRow.content_ref, MailThreadLedgerRow.status).where(
                        MailThreadLedgerRow.account_scope == account_scope,
                        MailThreadLedgerRow.status.in_(statuses),
                        MailThreadLedgerRow.content_ref.is_not(None),
                    )
                )
            ).all()
        return {str(ref): str(st) for ref, st in rows}

    async def status_counts(self, account_scope: str) -> dict[str, int]:
        """Rows per direction/status (ops evidence; codes only)."""
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(
                        MailThreadLedgerRow.direction,
                        MailThreadLedgerRow.status,
                        func.count(MailThreadLedgerRow.id),
                    )
                    .where(MailThreadLedgerRow.account_scope == account_scope)
                    .group_by(MailThreadLedgerRow.direction, MailThreadLedgerRow.status)
                )
            ).all()
        return {f"{d}:{st or '-'}": int(n) for d, st, n in rows}


__all__ = ["MailThreadLedgerRepository"]
