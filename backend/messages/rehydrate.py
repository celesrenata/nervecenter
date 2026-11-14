"""Rebuild the in-memory Messages plane from durable relay rows at startup.

The Messages screen reads the in-process :class:`MessagesService`, which is
empty after every API restart. Relay messages are persisted durably (row +
encrypted body, NS-ADR-091), so on startup this replays the most recent ones
into that service. Only messages whose body is in the object store
(``content://relay/...``) can be shown; rows that predate durable bodies
(``relay://...``) carry no text and are skipped.

Replay goes through ``MessagesService.ingest``, so ordering, threads and
dedup work exactly as they do for live traffic. Attention generation is not
re-run for history: rehydration only restores what the screen shows.
Never raises; returns counts for the startup log.
"""
from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: How many recent relay messages to restore (newest first in the query,
#: replayed oldest first).
DEFAULT_LIMIT = 500


async def rehydrate_messages(
    registry: Any, object_store: Any, service: Any, *, limit: int = DEFAULT_LIMIT
) -> dict[str, int]:
    out = {"restored": 0, "no_body": 0, "failed": 0}
    if registry is None or object_store is None or service is None:
        return out
    from sqlalchemy import select

    from backend.persistence.models.personal import CommunicationRow, PersonIdentityRow

    try:
        async with registry.session_factory() as session:
            rows = (
                await session.execute(
                    select(CommunicationRow)
                    .where(CommunicationRow.flags["source"].as_string() == "personal-relay")
                    .order_by(CommunicationRow.received_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            person_ids = {r.person_id for r in rows}
            handles: dict[str, str] = {}
            if person_ids:
                for ident in (
                    await session.execute(
                        select(PersonIdentityRow).where(PersonIdentityRow.person_id.in_(person_ids))
                    )
                ).scalars():
                    handles.setdefault(ident.person_id, ident.handle)
    except Exception as exc:  # noqa: BLE001 - startup must not fail on this
        log.warning("messages.rehydrate_skipped", error_type=type(exc).__name__)
        return out

    for row in reversed(rows):  # oldest first, as the messages arrived
        ref = row.content_ref or ""
        if not ref.startswith("content://relay/"):
            out["no_body"] += 1
            continue
        try:
            body = (await object_store.get(ref.removeprefix("content://"))).decode("utf-8")
            flags = row.flags or {}
            service.ingest({
                "message_id": str(flags.get("message_id") or row.id),
                "channel": row.channel,
                "direction": "OUTBOUND" if row.direction == "outgoing" else "INBOUND",
                "peer_identifier": handles.get(row.person_id, "unknown"),
                "peer_name": None,
                "body": body,
                "timestamp": row.received_at,
                "source": "personal-relay",
            }, generate_attention=False)
            out["restored"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            out["failed"] += 1
            log.warning("messages.rehydrate_row_failed", error_type=type(exc).__name__)
    return out


__all__ = ["DEFAULT_LIMIT", "rehydrate_messages"]
