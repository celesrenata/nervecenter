"""Workbench audit log (Req 6.11, 6.12).

``AuditLog.record(actor, project, action, outcome)`` writes one
``workbench_audit`` row and one structured log line. The API accepts no payload
text, so entries cannot carry file contents, prompts or secrets. Each field is
capped so the serialized entry never exceeds 4 KiB.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.persistence.models.workbench import WorkbenchAudit

logger = logging.getLogger("nervecenter.workbench.audit")

MAX_ENTRY_BYTES = 4096
MAX_FIELD_BYTES = 512
OUTCOMES = frozenset({"success", "failure"})


def _clip(value: str, limit: int = MAX_FIELD_BYTES) -> str:
    """Truncate to at most ``limit`` UTF-8 bytes without splitting a character."""
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    return raw[:limit].decode("utf-8", errors="ignore")


def serialize_entry(
    at: datetime, actor: str, project: str | None, action: str, outcome: str
) -> str:
    """Serialize an entry as compact JSON, guaranteed to be at most 4 KiB."""
    entry = {
        "event": "workbench_audit",
        "at": at.isoformat(),
        "actor": actor,
        "project": project,
        "action": action,
        "outcome": outcome,
    }
    text = json.dumps(entry, separators=(",", ":"))
    # ensure_ascii escapes can inflate non-ASCII text; shrink fields until it fits.
    limit = MAX_FIELD_BYTES
    while len(text.encode("utf-8")) > MAX_ENTRY_BYTES and limit > 1:
        limit //= 2
        entry["actor"] = _clip(actor, limit)
        entry["project"] = _clip(project, limit) if project is not None else None
        entry["action"] = _clip(action, limit)
        text = json.dumps(entry, separators=(",", ":"))
    return text


class AuditLog:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self, actor: str, project: str | None, action: str, outcome: str
    ) -> None:
        """Write one audit row and one structured log line."""
        if outcome not in OUTCOMES:
            raise ValueError("outcome must be 'success' or 'failure'")
        actor = _clip(actor)
        project = _clip(project) if project is not None else None
        action = _clip(action)
        at = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    WorkbenchAudit(
                        at=at,
                        actor=actor,
                        project_slug=project,
                        action=action,
                        outcome=outcome,
                    )
                )
        logger.info(serialize_entry(at, actor, project, action, outcome))
