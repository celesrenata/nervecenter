"""R102 — ingest-plane normalization into the unified communications registry.

Routes the existing messages / email / voicemail ingest payloads (prompt2
§75/§76/§77, AC-R03) through the R101 canonical graph without changing the
source service contracts. Every source payload is mapped to the §7
``Communication`` shape by a *pure* adapter; a :class:`CommunicationNormalizer`
then registers the normalized record with the
:class:`~backend.communications.registry.CommunicationsService` — the
``register_communication`` integration seam, backed by
:meth:`~backend.communications.registry.CommunicationsService.ingest_communication`.

Design notes
------------
- **Privacy by construction (§7 / R107).** Adapters never copy raw content
  (message body, email subject/body, voicemail transcript) into the
  normalized record. Only a deterministic :func:`content_ref` pointer
  (``message:<id>`` / ``email:<id>`` / ``voicemail:<id>``) is produced; the
  raw payload stays in the source plane, outside the knowledge graph.
- **Conservative attribution (§6 / AC-R02).** Person attribution happens in
  the registry, value-first: an equal canonical peer value links the existing
  person; an unresolved or uncertain value leaves the communication unlinked
  (``person_id=None``) rather than guessing. Identity provenance (channel,
  thread, source, direction) is preserved on every record.
- **Cross-channel conversation view (§32 / §62).** The registry folds one
  ``Conversation`` per (person, channel) — ``Conversation.channel`` is a
  single enum, so a true cross-channel conversation cannot be a registry
  ``Conversation`` (R101 contract). R102 therefore exposes a *derived*
  :class:`CrossChannelConversation` over all of a person's normalized
  communications, leaving the R101 per-channel fold untouched.
- **Idempotent replay (§48).** Registration is keyed by the source id
  (``message_id`` / ``voicemail_id`` / email ``message_id``): re-normalizing
  the same payload updates nothing and reports ``created=False``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.communications.registry import (
    CommunicationsService,
    get_communications_service,
)

__all__ = [
    "CommunicationNormalizer",
    "ContentRefError",
    "CrossChannelConversation",
    "NormalizedCommunication",
    "content_ref",
    "cross_channel_conversation",
    "normalize_batch",
    "normalize_email",
    "normalize_message",
    "normalize_voicemail",
]


class ContentRefError(ValueError):
    """Raised when a payload cannot yield a safe, deterministic content ref."""


def content_ref(kind: str, source_id: str) -> str:
    """Deterministic pointer for raw content that is never stored here.

    ``message:<id>`` / ``email:<id>`` / ``voicemail:<id>`` — stable across
    replays, so a re-ingested payload points at the same out-of-graph payload
    (§7: raw content may live encrypted outside the knowledge graph).
    """
    kind = str(kind or "").strip()
    source_id = str(source_id or "").strip()
    if not kind:
        raise ContentRefError("content ref kind must be non-empty")
    if not source_id:
        raise ContentRefError("content ref source id must be non-empty")
    return f"{kind}:{source_id}"


class NormalizedCommunication(BaseModel):
    """One source payload mapped to the §7 ``Communication`` shape.

    Carries only identity metadata, provenance, and a ``content_ref`` pointer
    — never raw message/email/voicemail content, so the record (and anything
    serialized from it) is JSON-safe and carries no plaintext payload.
    """

    communication_id: str
    channel: CommunicationChannel
    direction: CommunicationDirection
    #: Counterpart identity value — the raw peer address/handle/caller number
    #: as sent by the relay (an identity, not content). Maps to §7 ``sender``.
    sender: str
    recipients: list[str] = Field(default_factory=list)
    timestamp: datetime
    thread_id: str | None = None
    content_ref: str
    source: str = "personal-relay"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text(payload: dict[str, Any], field: str) -> str:
    """Trim a raw string field from a source payload (missing → empty)."""
    return str(payload.get(field) or "").strip()


def _thread_id(payload: dict[str, Any], field: str) -> str | None:
    return _text(payload, field) or None


def _ensure_utc(value: Any, *, field: str) -> datetime:
    """Coerce a datetime (or ISO-8601 string, as the relay posts) to UTC.

    A communication must carry its source timestamp (provenance), so a missing
    or unparseable value is an error rather than a silent default.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{field} is required for normalization")
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError(f"unparseable {field}: {value!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _channel(raw: Any, fallback: CommunicationChannel) -> CommunicationChannel:
    """Normalize a source channel tag to an allowed §7 channel.

    Unknown tags fall back (the Messages plane defaults to ``imessage``,
    §76) instead of failing the ingest.
    """
    try:
        return CommunicationChannel(str(raw or "").strip().lower() or fallback.value)
    except ValueError:
        return fallback


# ---------------------------------------------------------------------------
# pure adapters (source payload -> NormalizedCommunication)
# ---------------------------------------------------------------------------


def normalize_message(payload: dict[str, Any]) -> NormalizedCommunication:
    """Map a Messages ingest payload (prompt2 §76) to the §7 shape.

    - ``id=message_id``; channel normalized to ``imessage`` (an explicit
      allowed §7 channel tag such as ``sms`` is preserved).
    - ``INBOUND → incoming`` (peer is the ``sender``);
      ``OUTBOUND → outgoing`` (peer is the first ``recipient``; the relay
      payload carries no own-handle, so ``sender`` is the ``self`` placeholder).
    - ``thread_id=conversation_id``; peer identity from
      ``peer_identifier``/``peer_name`` (value first, name fallback).
    - The raw ``body`` is never stored; ``content_ref=message:<id>``.
    """
    message_id = _text(payload, "message_id")
    if not message_id:
        raise ContentRefError("message_id is required for normalization")

    direction_raw = _text(payload, "direction").upper() or "INBOUND"
    direction = (
        CommunicationDirection.OUTGOING
        if direction_raw == "OUTBOUND"
        else CommunicationDirection.INCOMING
    )

    peer = _text(payload, "peer_identifier") or _text(payload, "peer_name")
    if direction is CommunicationDirection.INCOMING:
        sender, recipients = peer or "unknown-peer", []
    else:
        sender, recipients = "self", [peer] if peer else []

    return NormalizedCommunication(
        communication_id=message_id,
        channel=_channel(payload.get("channel"), CommunicationChannel.IMESSAGE),
        direction=direction,
        sender=sender,
        recipients=recipients,
        timestamp=_ensure_utc(payload.get("timestamp"), field="timestamp"),
        thread_id=_thread_id(payload, "conversation_id"),
        content_ref=content_ref("message", message_id),
        source=_text(payload, "source") or "personal-relay",
    )


def normalize_email(payload: dict[str, Any]) -> NormalizedCommunication:
    """Map an Email ingest payload (prompt2 §75) to the §7 shape.

    - ``id=message_id``; channel ``email``.
    - ``RECEIVED → incoming`` (sender = ``from_address``);
      ``SENT → outgoing`` (recipient = ``to_address``; the counterpart the
      registry attributes the communication to).
    - ``thread_id`` preserved; the raw ``subject``/``body`` are excluded and
      ``content_ref=email:<id>`` is the only pointer to the payload.
    """
    message_id = _text(payload, "message_id")
    if not message_id:
        raise ContentRefError("message_id is required for normalization")

    direction_raw = _text(payload, "direction").upper() or "RECEIVED"
    direction = (
        CommunicationDirection.OUTGOING
        if direction_raw == "SENT"
        else CommunicationDirection.INCOMING
    )

    from_address = _text(payload, "from_address")
    to_address = _text(payload, "to_address")
    if direction is CommunicationDirection.INCOMING:
        # Value first: the From *address* is the identity; the display name
        # is only a fallback when the relay omitted the address.
        sender = from_address or _text(payload, "sender_name") or "unknown-sender"
        recipients = [to_address] if to_address else []
    else:
        # A SENT email originates from the user; the counterpart (attributed
        # by the registry) is the recipient, not the sender.
        sender = "self"
        recipients = [to_address] if to_address else []

    return NormalizedCommunication(
        communication_id=message_id,
        channel=CommunicationChannel.EMAIL,
        direction=direction,
        sender=sender,
        recipients=recipients,
        timestamp=_ensure_utc(payload.get("timestamp"), field="timestamp"),
        thread_id=_thread_id(payload, "thread_id"),
        content_ref=content_ref("email", message_id),
        source=_text(payload, "source") or "personal-relay",
    )


def normalize_voicemail(payload: dict[str, Any]) -> NormalizedCommunication:
    """Map a Voicemail ingest payload (prompt2 §77) to the §7 shape.

    - ``id=voicemail_id``; channel ``voicemail``; direction ``incoming``.
    - ``timestamp=received_at``; ``thread_id`` when present; caller identity
      from ``caller_phone`` (value first, ``caller_name`` fallback).
    - The raw ``transcript`` is excluded; ``content_ref=voicemail:<id>``.
    """
    voicemail_id = _text(payload, "voicemail_id")
    if not voicemail_id:
        raise ContentRefError("voicemail_id is required for normalization")

    caller = _text(payload, "caller_phone") or _text(payload, "caller_name")

    return NormalizedCommunication(
        communication_id=voicemail_id,
        channel=CommunicationChannel.VOICEMAIL,
        direction=CommunicationDirection.INCOMING,
        sender=caller or "unknown-caller",
        recipients=[],
        timestamp=_ensure_utc(payload.get("received_at"), field="received_at"),
        thread_id=_thread_id(payload, "thread_id"),
        content_ref=content_ref("voicemail", voicemail_id),
        source=_text(payload, "source") or "share-sheet",
    )


#: Adapter dispatch by ingest plane (source payloads from the P0–P2 planes).
_ADAPTERS = {
    "message": normalize_message,
    "email": normalize_email,
    "voicemail": normalize_voicemail,
}


# ---------------------------------------------------------------------------
# cross-channel conversation view (§32 / §62 — derived; R101 fold untouched)
# ---------------------------------------------------------------------------


class CrossChannelConversation(BaseModel):
    """§62 cross-channel conversation: one evolving interaction across
    channels for a single canonical person.

    Derived from the registry's normalized communications — it is a *view*,
    not a stored registry ``Conversation`` (whose ``channel`` is a single
    enum, R101). ``thread_ids`` keep the per-channel thread provenance so
    context is available from every channel (§62: "Context available:
    email + text + voicemail summary").
    """

    conversation_id: str
    person_id: str | None
    display_name: str | None = None
    channels: list[CommunicationChannel] = Field(default_factory=list)
    thread_ids: list[str] = Field(default_factory=list)
    communication_ids: list[str] = Field(default_factory=list)
    communication_count: int = 0
    started_at: datetime
    last_activity_at: datetime


def cross_channel_conversation(
    communications: list[Communication], display_name: str | None = None
) -> CrossChannelConversation | None:
    """Fold one canonical person's communications into a cross-channel view.

    ``communications`` must belong to a single person (callers filter by
    ``person_id``). Returns ``None`` for an empty set.
    """
    if not communications:
        return None
    ordered = sorted(communications, key=lambda c: (c.timestamp, c.communication_id))
    person_id = ordered[0].person_id
    channels = sorted({c.channel for c in ordered}, key=lambda ch: ch.value)
    thread_ids = sorted({c.thread_id for c in ordered if c.thread_id})
    return CrossChannelConversation(
        conversation_id=f"ccx-{person_id or 'unlinked'}",
        person_id=person_id,
        display_name=display_name,
        channels=channels,
        thread_ids=thread_ids,
        communication_ids=[c.communication_id for c in ordered],
        communication_count=len(ordered),
        started_at=ordered[0].timestamp,
        last_activity_at=ordered[-1].timestamp,
    )


# ---------------------------------------------------------------------------
# registry-backed normalizer (the register_communication integration seam)
# ---------------------------------------------------------------------------


class CommunicationNormalizer:
    """Pure adapters + registry registration for the ingest planes (R102).

    :meth:`register_communication` is the integration seam: it maps one
    source payload with the matching pure adapter and registers the result
    with the registry (conservative attribution, idempotent by
    ``communication_id``). :meth:`normalize_batch` registers a whole batch
    and derives the §62 cross-channel conversation view per attributed
    person.
    """

    def __init__(self, service: CommunicationsService | None = None) -> None:
        self._service = service or get_communications_service()

    # -- single-record seam ---------------------------------------------------
    def register_communication(
        self,
        source: str,
        payload: dict[str, Any],
        *,
        person_id: str | None = None,
    ) -> dict[str, Any]:
        """Normalize one source payload and register it with the registry.

        Returns ``{"spec", "result"}`` where ``result`` is the registry's
        ``{"communication", "conversation", "created"}``. Attribution is
        conservative: an unresolved / uncertain peer identity yields
        ``person_id=None`` (the communication is stored, unlinked).

        An explicit ``person_id`` forces attribution to an existing person
        (the registry raises ``KeyError`` for an unknown id).
        """
        adapter = _ADAPTERS.get(source)
        if adapter is None:
            raise ValueError(f"unknown ingest source: {source!r}")
        spec: NormalizedCommunication = adapter(payload)
        reg_payload: dict[str, Any] = spec.model_dump()
        if person_id:
            reg_payload["person_id"] = person_id
        result = self._service.ingest_communication(reg_payload)
        return {"spec": spec, "result": result}

    # -- batch + cross-channel view --------------------------------------------
    def normalize_batch(
        self,
        items: list[tuple[str, dict[str, Any]]],
        *,
        person_id: str | None = None,
    ) -> dict[str, Any]:
        """Register a batch of ``(source, payload)`` pairs.

        Replay-safe: re-normalizing the same source ids is idempotent
        (``created=False`` per item, no duplicate person or communication).
        ``cross_channel`` maps each attributed ``person_id`` to its §62
        :class:`CrossChannelConversation` view; unlinked communications are
        excluded from every view.
        """
        results: list[dict[str, Any]] = []
        for source, payload in items:
            results.append(self.register_communication(source, payload, person_id=person_id))

        views: dict[str, CrossChannelConversation] = {}
        seen: set[str] = set()
        for item in results:
            pid = item["result"]["communication"].person_id
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            person = self._service.get_person(pid)
            group = self._service.list_communications(person_id=pid)
            if not group:
                continue
            view = cross_channel_conversation(
                group, person.display_name if person is not None else None
            )
            if view is not None:
                views[pid] = view
        return {"results": results, "cross_channel": views}


def normalize_batch(
    items: list[tuple[str, dict[str, Any]]],
    *,
    person_id: str | None = None,
    service: CommunicationsService | None = None,
) -> dict[str, Any]:
    """Batch convenience over the process-wide registry singleton."""
    return CommunicationNormalizer(service).normalize_batch(items, person_id=person_id)
