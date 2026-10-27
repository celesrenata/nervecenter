"""Durable per-connection dedup helpers (§4: "dedup by account and
message-id/uid").

The primary guarantee is the transport cursor itself (IMAP UID / Gmail
history-id strictly advance), which a restarted worker resumes from the
database — never from memory. The *secondary* guard is a bounded list of
already-ingested mail ids persisted **inside the same ``ingest_cursors``
row** under the ``seen`` key (``{"uid": 421, "seen": [...]}``), so a
replayed delivery or a source that re-emits an already imported message
cannot re-enter the communication graph.

Keeping the list in the transport cursor row (rather than a separate row)
respects the frozen ``ingest_cursors`` kind contract (§6: kinds are exactly
``imap_seq`` / ``gmail_history`` / ``relay_seq``); the transports ignore
unknown cursor keys, so the ``seen`` list never leaks into a poll. The list
is capped (most-recent win) so the JSONB stays bounded even for years of
history; entries beyond the cap are still protected by the transport
cursor.
"""

from __future__ import annotations

from typing import Any

#: Upper bound on retained seen-keys per (connection, kind).
MAX_SEEN_KEYS = 2000


def seen_keys(cursor: dict[str, Any] | None) -> list[str]:
    """The durable seen-list from a cursor row value (never mutates it)."""
    value = cursor or {}
    raw = value.get("seen") or []
    return [str(v) for v in raw]


def is_seen(seen: list[str], mail_id: str) -> bool:
    """Return ``True`` when ``mail_id`` was already ingested for this
    connection."""
    return mail_id in seen


def mark_seen(seen: list[str], mail_id: str) -> list[str]:
    """Record ``mail_id`` as ingested; return the capped, most-recent list.

    Idempotent: re-marking an existing id returns the list unchanged.
    """
    if mail_id in seen:
        return seen
    return seen[-(MAX_SEEN_KEYS - 1):] + [mail_id]


def cursor_with_seen(cursor: dict[str, Any], seen: list[str]) -> dict[str, Any]:
    """Return a new cursor dict with the durable ``seen`` list set (the
    input is not mutated)."""
    out = dict(cursor)
    out["seen"] = list(seen)
    return out
