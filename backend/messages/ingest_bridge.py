"""Bridge that mirrors relay ingest into the in-memory messages service.

The relay control plane (workstream B) persists every message through the
durable communication-plane repositories. For *live reads* — the Messages
screen, attention generation, reply drafts — the legacy in-memory
:class:`~backend.messages.service.MessagesService` is still the source the
UI reads from. This bridge re-feeds each persisted relay message into that
in-memory service **without modifying any existing file**: it wraps
:mod:`backend.messages.service` behind a thin, dependency-injected seam so the
durable plane and the live plane stay in lockstep.

Design notes
------------
- ``MessagesIngestBridge`` is a pure composition: it holds a reference to a
  :class:`MessagesService` (resolved lazily via
  :func:`~backend.messages.service.get_messages_service` when not injected)
  and forwards each message with the normalized payload that
  :meth:`MessagesService.ingest` expects.
- It is intentionally stateless about the durable plane: it never reads the
  DB. The caller (the relay endpoint) has already durably stored the message
  and only needs the live mirror.
- The bridge never raises on the live plane: if the in-memory service is
  unavailable it logs and skips, so a durable write is never rolled back by a
  live-read mirror failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from backend.messages.service import MessagesService, get_messages_service

log = structlog.get_logger(__name__)


def _to_iso(value: Any) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class MessagesIngestBridge:
    """Re-feed persisted relay messages into the in-memory messages service."""

    def __init__(self, service: MessagesService | None = None) -> None:
        self._service = service  # lazy-resolved on first use when None

    @property
    def service(self) -> MessagesService | None:
        if self._service is None:
            try:
                self._service = get_messages_service()
            except Exception as exc:  # noqa: BLE001 - live mirror must not break ingest
                log.warning("ingest_bridge.service_unavailable", error=str(exc))
                return None
        return self._service

    def mirror_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Mirror one relay message into the live plane.

        ``message`` is the relay wire shape: ``{message_id, channel,
        direction, peer_identifier, peer_name, body, received_at,
        source, ...}``. Returns the in-memory service's ingest result or
        ``None`` when the live plane is unavailable (the durable write is the
        authoritative path and is unaffected).
        """
        svc = self.service
        if svc is None:
            return None
        payload = {
            "message_id": str(message.get("message_id", "")),
            "channel": str(message.get("channel", "imessage")),
            "direction": str(message.get("direction", "inbound")),
            "peer_identifier": str(message.get("peer_identifier", "")),
            "peer_name": message.get("peer_name"),
            "body": str(message.get("body", "")),
            "timestamp": _to_iso(message.get("received_at")),
            "source": str(message.get("source", "personal-relay")),
        }
        try:
            return svc.ingest(payload)
        except Exception as exc:  # noqa: BLE001 - live mirror must not break ingest
            log.warning("ingest_bridge.mirror_failed", error=str(exc))
            return None


__all__ = ["MessagesIngestBridge"]
