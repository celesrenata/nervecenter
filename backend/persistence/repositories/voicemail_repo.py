"""Durable storage for voicemails and their threads.

Used by :class:`backend.voicemail.service.VoicemailService` as a write-through
store: the in-memory service stays the read model, this repository is what
survives an API restart.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, select

from ..models.voicemail import VoicemailRow, VoicemailThreadRow

_VM_FIELDS = (
    "thread_id", "caller_phone", "caller_name", "transcript", "duration_seconds",
    "received_at", "read", "needs_callback", "callback_reason", "source",
)
_THREAD_FIELDS = (
    "caller_phone", "caller_name", "voicemail_count", "unread", "last_transcript",
    "last_duration_seconds", "last_received_at", "needs_callback", "callback_reason",
    "attention_id", "created_at", "updated_at",
)


class VoicemailRepository:
    """Durable access to :class:`VoicemailRow` and :class:`VoicemailThreadRow`."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def load_all(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Every voicemail and thread as plain dicts (startup rehydration)."""
        async with self._sessions() as session:
            vms = (await session.execute(select(VoicemailRow))).scalars().all()
            threads = (await session.execute(select(VoicemailThreadRow))).scalars().all()
            return (
                [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in vms],
                [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in threads],
            )

    async def apply(
        self,
        *,
        voicemails: Iterable[dict[str, Any]] = (),
        threads: Iterable[dict[str, Any]] = (),
        deleted_voicemail_ids: Iterable[str] = (),
        deleted_thread_ids: Iterable[str] = (),
    ) -> None:
        """Upsert and delete in one transaction (all or nothing)."""
        async with self._sessions() as session:
            for vm in voicemails:
                row = await session.get(VoicemailRow, vm["voicemail_id"])
                if row is None:
                    row = VoicemailRow(voicemail_id=vm["voicemail_id"])
                    session.add(row)
                for name in _VM_FIELDS:
                    setattr(row, name, vm[name])
            for th in threads:
                row = await session.get(VoicemailThreadRow, th["thread_id"])
                if row is None:
                    row = VoicemailThreadRow(thread_id=th["thread_id"])
                    session.add(row)
                for name in _THREAD_FIELDS:
                    setattr(row, name, th[name])
            ids = list(deleted_voicemail_ids)
            if ids:
                await session.execute(
                    delete(VoicemailRow).where(VoicemailRow.voicemail_id.in_(ids))
                )
            tids = list(deleted_thread_ids)
            if tids:
                await session.execute(
                    delete(VoicemailThreadRow).where(VoicemailThreadRow.thread_id.in_(tids))
                )
            await session.commit()


__all__ = ["VoicemailRepository"]
