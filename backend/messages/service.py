"""In-memory Messages service (prompt2 Phase P1).

This is the P1 slice of the Personal Operations Plane: it receives
*normalized* messages from the macOS Personal Relay (``nerve-personal-relay``,
prompt2 §46/§47), reconstructs threads, resolves people across channels, and
produces :class:`~backend.api.models.AttentionItem` entries so an unread
conversation surfaces in the Now screen (prompt2 §76 acceptance: "a new Mac
Messages conversation appears inside NerveCenter and can produce an
AttentionItem").

Design notes
-----------
- **NerveCenter never touches source message storage.** The relay is the
  ingestion mechanism (§47: read-only, explicit permissions, fail closed).
  This service only ever sees normalized events posted via
  ``POST /api/messages/ingest``.
- **No persistence yet.** P1 deliberately stores conversations in process
  memory so the API, the Messages screen, and the tests all work without
  ``NERVE_DB_URL`` — same convention as the P0 attention store. Public method
  signatures are designed to survive a later swap to a real table.
- **Idempotent ingest.** Message ids are unique: re-posting the same
  ``message_id`` is a no-op, which matters because the relay replays queued
  events when connectivity returns (§48) and must preserve ordering.
- **Deterministic, explainable.** Attention generation reuses the P0
  :class:`~backend.attention.service.AttentionService` scorer, so "why is this
  conversation on my Now screen" is always answerable (§21/§22).
- **Drafts, not sends.** Reply drafting is local and human-in-the-loop
  (prompt2 §13: "AI drafts, human sends"); nothing is ever sent from here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from backend.api.models import (
    AttentionState,
    Conversation,
    Message,
    MessageDirection,
)
from backend.attention.service import AttentionService, get_attention_service

# A conversation stops producing "new activity" attention once every inbound
# message has been acknowledged; re-escalation happens only on new inbounds.
_REPLY_REQUEST_PATTERN = re.compile(
    r"\?\s*$|can you|could you|will you|are you|when (?:can|are)|let me know",
    re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime | str | None) -> datetime | None:
    """Coerce a datetime (or ISO-8601 string, as the relay posts) to UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _conversation_key(peer_identifier: str, channel: str) -> str:
    """One thread per (peer, channel) — thread reconstruction (§76)."""
    peer = peer_identifier.strip().lower()
    channel = channel.strip().lower() or "imessage"
    return f"conv-{peer}|{channel}"


class MessagesService:
    """In-memory message ingestion, threading, and attention generation (P1)."""

    def __init__(self, attention: AttentionService | None = None) -> None:
        self._attention = attention or get_attention_service()
        self._messages: dict[str, Message] = {}
        self._conversations: dict[str, Conversation] = {}
        # Cross-channel person resolution (§76): raw identifier (lowercased) →
        # display name. One person can own many identifiers (phone + handle).
        self._person_bindings: dict[str, str] = {}

    # -- person resolution --------------------------------------------------
    def register_person(
        self,
        identifier: str,
        name: str,
        channels: list[str] | None = None,
    ) -> list[str]:
        """Cross-channel person resolution (§76): bind one person name to one
        (or more) raw identifier(s) — phone numbers, iMessage handles, other
        channel ids. Every existing conversation keyed on that identifier
        picks the name up immediately, and future messages from it resolve
        without the relay having to send the name.

        Returns the conversation ids updated by this call so callers (and
        tests) can observe the resolution effect.
        """
        key = identifier.strip().lower()
        self._person_bindings[key] = name
        updated: list[str] = []
        for conv in self._conversations.values():
            if conv.peer_identifier.strip().lower() == key and conv.peer_name != name:
                conv.peer_name = name
                conv.updated_at = _now()
                updated.append(conv.conversation_id)
        return updated

    # -- ingestion -----------------------------------------------------------
    def ingest(self, payload: dict[str, Any], *, generate_attention: bool = True) -> dict[str, Any]:
        """Ingest one normalized message (as posted by the Personal Relay).

        Returns ``{"message": Message, "conversation": Conversation,
        "new_attention": AttentionItem | None, "duplicate": bool}``.
        Re-ingesting an already-seen ``message_id`` returns ``duplicate=True``
        and changes nothing (§48 replay safety).
        """
        message_id = payload["message_id"]
        if message_id in self._messages:
            conv = self._conversations[self._messages[message_id].conversation_id]
            return {"message": self._messages[message_id], "conversation": conv, "new_attention": None, "duplicate": True}

        direction = MessageDirection(payload.get("direction", MessageDirection.INBOUND.value))
        timestamp = _ensure_utc(payload["timestamp"]) or _now()
        channel = str(payload.get("channel", "imessage"))
        peer = str(payload["peer_identifier"]).strip()
        peer_name = payload.get("peer_name") or self._resolve_peer_name(peer, channel)

        message = Message(
            message_id=message_id,
            conversation_id=_conversation_key(peer, channel),
            direction=direction,
            peer_identifier=peer,
            peer_name=peer_name,
            body=str(payload.get("body", "")),
            channel=channel,
            timestamp=timestamp,
            source=str(payload.get("source", "personal-relay")),
        )
        self._messages[message_id] = message

        conversation = self._conversations.get(message.conversation_id)
        if conversation is None:
            conversation = Conversation(
                conversation_id=message.conversation_id,
                peer_identifier=peer,
                peer_name=peer_name,
                channel=channel,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._conversations[conversation.conversation_id] = conversation

        # Thread update: counts + unread bookkeeping.
        if direction is MessageDirection.INBOUND:
            conversation.unread += 1
        else:
            # Replying counts as engaging the thread; clear the unread badge.
            conversation.unread = 0
        conversation.message_count += 1
        conversation.last_body = message.body
        conversation.last_direction = direction
        conversation.last_timestamp = timestamp
        conversation.updated_at = _now()
        if peer_name and conversation.peer_name != peer_name:
            conversation.peer_name = peer_name

        # Attention generation (§76): a fresh inbound that the user has not yet
        # engaged with (unread == 1) — or one carrying a reply request —
        # (re)creates the conversation's AttentionItem. A source keeps exactly
        # ONE live item: if the thread already has an unresolved item we
        # *refresh* it in place instead of stacking duplicates.
        new_attention = None
        # ``generate_attention=False``: restoring history at startup must not
        # re-raise attention for messages the user already saw.
        if direction is MessageDirection.INBOUND and generate_attention:
            should_escalate = (
                conversation.unread == 1
                or bool(_REPLY_REQUEST_PATTERN.search(message.body or ""))
            )
            if should_escalate:
                payload = self._attention_payload(conversation, message)
                existing = self._attention.get(conversation.attention_id) if conversation.attention_id else None
                still_live = (
                    existing is not None
                    and existing.status in (AttentionState.NOW, AttentionState.TODAY, AttentionState.SOON, AttentionState.WAITING_ON)
                )
                if still_live and conversation.attention_id is not None:
                    new_attention = self._attention.refresh(conversation.attention_id, payload)
                else:
                    new_attention = self._attention.create(payload)
                    conversation.attention_id = new_attention.attention_id

        return {
            "message": message,
            "conversation": conversation,
            "new_attention": new_attention,
            "duplicate": False,
        }

    def _resolve_peer_name(self, identifier: str, channel: str) -> str | None:
        """Cross-channel person resolution for an arriving message (§76):
        match the raw identifier against known person bindings."""
        name = self._person_bindings.get(identifier.strip().lower())
        if name:
            return name
        # Fall back to whatever this (peer, channel) thread already knows.
        conv = self._conversations.get(_conversation_key(identifier, channel))
        return conv.peer_name if conv else None

    def _attention_payload(self, conv: Conversation, message: Message) -> dict[str, Any]:
        who = conv.peer_name or conv.peer_identifier
        has_question = bool(_REPLY_REQUEST_PATTERN.search(message.body or ""))
        urgency = 0.9 if has_question else 0.6
        payload: dict[str, Any] = {
            "reason": f"Reply to {who} ({conv.channel})" if has_question else f"New message from {who} ({conv.channel})",
            "urgency": urgency,
            "importance": 0.6,
            "entities": [who],
            "sources": [f"messages:{conv.channel}"],
            "suggested_action": f"Reply to {who}",
            "status": AttentionState.NOW.value,
            "why": [f"unread inbound from {who} via {conv.channel}"],
        }
        if has_question:
            payload["why"].append("direct request: the last message asks a question")
        return payload

    # -- queries -------------------------------------------------------------
    def list_conversations(self, channel: str | None = None) -> list[Conversation]:
        """Conversations, most recently active first."""
        convs = [
            c for c in self._conversations.values()
            if channel is None or c.channel == channel.strip().lower()
        ]
        return sorted(convs, key=lambda c: (c.last_timestamp or datetime.min.replace(tzinfo=UTC)), reverse=True)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)

    def get_messages(self, conversation_id: str) -> list[Message]:
        """All messages in a thread, chronological (thread reconstruction)."""
        msgs = [m for m in self._messages.values() if m.conversation_id == conversation_id]
        return sorted(msgs, key=lambda m: m.timestamp)

    # -- engagement ----------------------------------------------------------
    def acknowledge(self, conversation_id: str) -> Conversation | None:
        """Mark the thread read: clear the unread count and resolve its
        AttentionItem (the human acted on it)."""
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        conv.unread = 0
        conv.updated_at = _now()
        if conv.attention_id:
            self._attention.resolve(conv.attention_id)
        return conv

    # -- reply drafting (§13: AI drafts, human sends) ------------------------
    def draft_reply(self, conversation_id: str) -> dict[str, Any]:
        """Produce a suggested reply for the thread's latest inbound message.

        Deterministic and transparent: this P1 implementation is a
        rule-based drafter (echo of the ask + acknowledgment). An LLM-backed
        drafter can replace this method later without changing the contract.
        Returns ``{"conversation_id", "to", "draft", "basis"}``.
        """
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return {"conversation_id": conversation_id, "to": None, "draft": None, "basis": "conversation not found"}
        inbound = [m for m in self.get_messages(conversation_id) if m.direction is MessageDirection.INBOUND]
        if not inbound:
            return {
                "conversation_id": conversation_id,
                "to": conv.peer_name or conv.peer_identifier,
                "draft": None,
                "basis": "no inbound messages to reply to",
            }
        last = inbound[-1]
        who = conv.peer_name or conv.peer_identifier
        body = (last.body or "").strip()
        if body.endswith("?"):
            draft = f"Sure — let me check and get back to you."
        else:
            first = body.split(".")[0] if body else ""
            draft = f"Thanks {who}, noted. " + (f"({first}) " if first else "") + "I'll follow up soon."
        return {
            "conversation_id": conversation_id,
            "to": who,
            "draft": draft,
            "basis": f"rule-based draft from latest inbound ({last.message_id})",
        }

    # -- seeding / tests -----------------------------------------------------
    def seed_demo(self) -> None:
        """Dev-mode seeder (NERVE_SEED_MESSAGES=1): a small, realistic
        conversation so the Messages screen has content before the relay
        posts real traffic."""
        base = _now()
        self.ingest({
            "message_id": "msg-seed-1",
            "direction": "INBOUND",
            "peer_identifier": "+14155550134",
            "peer_name": "Alice Nguyen",
            "body": "Are you still free to review the deck Friday?",
            "channel": "imessage",
            "timestamp": base,
            "source": "seed",
        })
        self.ingest({
            "message_id": "msg-seed-2",
            "direction": "INBOUND",
            "peer_identifier": "+14155550134",
            "peer_name": "Alice Nguyen",
            "body": "No rush — Monday works too if Friday is gone.",
            "channel": "imessage",
            "timestamp": base,
            "source": "seed",
        })


# -- process-wide singleton (mirrors the P0 attention service) ---------------
_service: MessagesService | None = None


def get_messages_service() -> MessagesService:
    global _service
    if _service is None:
        _service = MessagesService()
    return _service


def reset_messages_service() -> None:
    """Test hook — replace the singleton with a fresh, empty service."""
    global _service
    _service = MessagesService()
