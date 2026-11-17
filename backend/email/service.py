"""In-memory Email service (prompt2 Phase P0).

This is the P0 slice of the Personal Operations Plane: it receives
*normalized* email events from the macOS Personal Relay, reconstructs
threads, and produces
:class:`~backend.api.models.AttentionItem` entries so an unread thread
surfaces in the Now screen (prompt2 §75 acceptance: "a new email thread
appears inside NerveCenter and can produce an AttentionItem").

Design notes
------------
- **NerveCenter never touches a mailbox.** The Personal Relay daemon is the
  ingestion mechanism and it is *out of scope for this deliverable* (the
  §46/§47-style read-only mailbox bridge, explicit permissions, fail closed).
  This service only ever sees normalized email events posted via
  ``POST /api/email/ingest`` — no live EWS/IMAP access in-process.
- **No persistence yet.** P0 deliberately stores threads in process memory so
  the API, the Email screen, and the tests all work without ``NERVE_DB_URL``
  — same convention as the P0 attention store. Public method signatures are
  designed to survive a later swap to a real table.
- **Idempotent ingest.** Email ``message_id``s (Message-ID headers) are
  unique: re-posting the same ``message_id`` is a no-op, which matters
  because the relay replays queued events when connectivity returns (§48)
  and must preserve ordering.
- **Deterministic, explainable.** Attention generation reuses the P0
  :class:`~backend.attention.service.AttentionService` scorer, so "why is
  this thread on my Now screen" is always answerable (§21/§22).
- **Drafts, not sends.** Reply drafting is local and human-in-the-loop
  (prompt2 §13: "AI drafts, human sends"); nothing is ever sent from here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from backend.api.models import (
    AttentionState,
    EmailDirection,
    EmailMessage,
    EmailThread,
)
from backend.attention.service import AttentionService, get_attention_service
from backend.email.body import decode_body, looks_like_html, looks_like_mime

# A thread stops producing "new activity" attention once every received
# email has been acknowledged; re-escalation happens only on new inbounds.
_REPLY_REQUEST_PATTERN = re.compile(
    r"\?\s*$|can you|could you|will you|are you|when (?:can|are)|let me know",
    re.IGNORECASE,
)
# A thread key must survive "Re: …"/"Fw: …" prefixes on the subject line.
_SUBJECT_PREFIX_PATTERN = re.compile(r"^(re|fwd|fw)[:\s]+(.*)$", re.IGNORECASE)


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


def _canonical_subject(subject: str) -> str:
    """Normalize a subject line for thread keys: strip reply/forward
    prefixes and collapse whitespace/case, so a "Re:" reply lands on the
    same thread as the original."""
    subj = subject.strip()
    while True:
        m = _SUBJECT_PREFIX_PATTERN.match(subj)
        if not m:
            break
        subj = m.group(2).strip()
    return " ".join(subj.lower().split())


def _thread_key(counterpart: str, subject: str) -> str:
    """One thread per (counterpart, subject) — thread reconstruction (§75).

    The *counterpart* is the other side of the conversation (the sender for
    a received email, the recipient for an outgoing one), so a sent reply
    under the same recipient address and a subject line joins the received
    thread it answers.
    """
    peer = counterpart.strip().lower()
    subj = _canonical_subject(subject)
    return f"thr-{peer}|{subj or '(no subject)'}"


class EmailService:
    """In-memory email ingestion, threading, and attention generation (P0)."""

    def __init__(self, attention: AttentionService | None = None) -> None:
        self._attention = attention or get_attention_service()
        self._messages: dict[str, EmailMessage] = {}
        self._threads: dict[str, EmailThread] = {}

    # -- ingestion -----------------------------------------------------------
    def ingest(
        self, payload: dict[str, Any], *, generate_attention: bool = True
    ) -> dict[str, Any]:
        """Ingest one normalized email (as posted by the Personal Relay).

        Returns ``{"message": EmailMessage, "thread": EmailThread,
        "new_attention": AttentionItem | None, "duplicate": bool}``.
        Re-ingesting an already-seen ``message_id`` returns
        ``duplicate=True`` and changes nothing (§48 replay safety).
        """
        message_id = payload["message_id"]
        if message_id in self._messages:
            thread = self._threads[self._messages[message_id].thread_id]
            return {"message": self._messages[message_id], "thread": thread, "new_attention": None, "duplicate": True}

        direction = EmailDirection(payload.get("direction", EmailDirection.RECEIVED.value))
        timestamp = _ensure_utc(payload["timestamp"]) or _now()
        sender = str(payload["from_address"]).strip()
        recipient = str(payload.get("to_address", "")).strip()
        subject = str(payload.get("subject", ""))
        sender_name = payload.get("sender_name")

        # The thread is keyed on the *counterpart*: the other side of the
        # conversation (the sender for a received email, the recipient for
        # an outgoing one) plus the normalized subject line.
        counterpart = recipient if direction is EmailDirection.SENT else sender
        body_text = str(payload.get("body", ""))
        body_html = payload.get("body_html")
        if body_html is None and (
            payload.get("content_type")
            or payload.get("transfer_encoding")
            or looks_like_html(body_text)
            or looks_like_mime(body_text)
        ):
            decoded = decode_body(
                body_text,
                content_type=payload.get("content_type"),
                transfer_encoding=payload.get("transfer_encoding"),
                charset=payload.get("charset"),
            )
            body_text, body_html = decoded.text, decoded.html

        message = EmailMessage(
            message_id=message_id,
            thread_id=_thread_key(counterpart, subject),
            direction=direction,
            from_address=sender,
            to_address=recipient,
            subject=subject,
            body=body_text,
            body_html=body_html,
            timestamp=timestamp,
            source=str(payload.get("source", "personal-relay")),
        )
        self._messages[message_id] = message

        thread = self._threads.get(message.thread_id)
        if thread is None:
            thread = EmailThread(
                thread_id=message.thread_id,
                # The counterpart is always the "other side"; a thread opened
                # by an outgoing email has no known sender yet (the reply
                # will supply one).
                from_address=sender if direction is EmailDirection.RECEIVED else recipient,
                to_address=recipient if direction is EmailDirection.RECEIVED else sender,
                sender_name=sender_name,
                subject=subject,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._threads[thread.thread_id] = thread

        # Thread update: counts + unread bookkeeping.
        if direction is EmailDirection.RECEIVED:
            thread.unread += 1
        else:
            # Sending counts as engaging the thread; clear the unread badge.
            thread.unread = 0
        thread.message_count += 1
        thread.last_body = message.body
        thread.last_direction = direction
        thread.last_timestamp = timestamp
        thread.updated_at = _now()
        if sender_name and thread.sender_name != sender_name:
            thread.sender_name = sender_name

        # Attention generation (§75): a fresh received email that the user
        # has not yet engaged with (unread == 1) — or one carrying a reply
        # request — (re)creates the thread's AttentionItem. A source keeps
        # exactly ONE live item: if the thread already has an unresolved
        # item we *refresh* it in place instead of stacking duplicates.
        new_attention = None
        if generate_attention and direction is EmailDirection.RECEIVED:
            should_escalate = (
                thread.unread == 1
                or bool(_REPLY_REQUEST_PATTERN.search(message.body or ""))
            )
            if should_escalate:
                payload = self._attention_payload(thread, message)
                existing = self._attention.get(thread.attention_id) if thread.attention_id else None
                still_live = (
                    existing is not None
                    and existing.status in (AttentionState.NOW, AttentionState.TODAY, AttentionState.SOON, AttentionState.WAITING_ON)
                )
                if still_live and thread.attention_id is not None:
                    new_attention = self._attention.refresh(thread.attention_id, payload)
                else:
                    new_attention = self._attention.create(payload)
                    thread.attention_id = new_attention.attention_id

        return {
            "message": message,
            "thread": thread,
            "new_attention": new_attention,
            "duplicate": False,
        }

    def _attention_payload(self, thread: EmailThread, message: EmailMessage) -> dict[str, Any]:
        who = thread.sender_name or thread.from_address
        has_question = bool(_REPLY_REQUEST_PATTERN.search(message.body or ""))
        urgency = 0.9 if has_question else 0.6
        payload: dict[str, Any] = {
            "reason": f"Reply to {who} — “{thread.subject}”" if has_question else f"New email from {who} — “{thread.subject}”",
            "urgency": urgency,
            "importance": 0.6,
            "entities": [who],
            "sources": ["email"],
            "suggested_action": f"Reply to {who}",
            "status": AttentionState.NOW.value,
            "why": [f"unread inbound email from {who}"],
        }
        if has_question:
            payload["why"].append("direct request: the last email asks a question")
        return payload

    # -- queries -------------------------------------------------------------
    def list_threads(self) -> list[EmailThread]:
        """Threads, most recently active first."""
        return sorted(
            self._threads.values(),
            key=lambda t: (t.last_timestamp or datetime.min.replace(tzinfo=UTC)),
            reverse=True,
        )

    def get_thread(self, thread_id: str) -> EmailThread | None:
        return self._threads.get(thread_id)

    def get_messages(self, thread_id: str) -> list[EmailMessage]:
        """All emails in a thread, chronological (thread reconstruction)."""
        msgs = [m for m in self._messages.values() if m.thread_id == thread_id]
        return sorted(msgs, key=lambda m: m.timestamp)

    # -- engagement ----------------------------------------------------------
    def acknowledge(self, thread_id: str) -> EmailThread | None:
        """Mark the thread read: clear the unread count and resolve its
        AttentionItem (the human acted on it)."""
        thread = self._threads.get(thread_id)
        if thread is None:
            return None
        thread.unread = 0
        thread.updated_at = _now()
        if thread.attention_id:
            self._attention.resolve(thread.attention_id)
        return thread

    # -- reply drafting (§13: AI drafts, human sends) ------------------------
    def draft_reply(self, thread_id: str) -> dict[str, Any]:
        """Produce a suggested reply for the thread's latest inbound email.

        Deterministic and transparent: this P0 implementation is a
        rule-based drafter (echo of the ask + acknowledgment). An LLM-backed
        drafter can replace this method later without changing the contract.
        Returns ``{"thread_id", "to", "draft", "basis"}``.
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            return {"thread_id": thread_id, "to": None, "draft": None, "basis": "thread not found"}
        inbound = [m for m in self.get_messages(thread_id) if m.direction is EmailDirection.RECEIVED]
        if not inbound:
            return {
                "thread_id": thread_id,
                "to": thread.sender_name or thread.from_address,
                "draft": None,
                "basis": "no inbound emails to reply to",
            }
        last = inbound[-1]
        who = thread.sender_name or thread.from_address
        body = (last.body or "").strip()
        if body.endswith("?"):
            draft = f"Sure — let me check and get back to you."
        else:
            first = body.split(".")[0] if body else ""
            draft = f"Thanks {who}, noted. " + (f"({first}) " if first else "") + "I'll follow up soon."
        return {
            "thread_id": thread_id,
            "to": who,
            "draft": draft,
            "basis": f"rule-based draft from latest inbound ({last.message_id})",
        }

    # -- seeding / tests -----------------------------------------------------
    def seed_demo(self) -> None:
        """Dev-mode seeder (NERVE_SEED_EMAILS=1): a small, realistic
        thread so the Email screen has content before the relay posts
        real traffic."""
        base = _now()
        self.ingest({
            "message_id": "email-seed-1",
            "direction": "RECEIVED",
            "from_address": "alice.n@example.com",
            "to_address": "me@example.com",
            "sender_name": "Alice Nguyen",
            "subject": "Q4 planning — heads up",
            "body": "Are you still free to review the deck Friday?",
            "timestamp": base,
            "source": "seed",
        })
        self.ingest({
            "message_id": "email-seed-2",
            "direction": "RECEIVED",
            "from_address": "alice.n@example.com",
            "to_address": "me@example.com",
            "sender_name": "Alice Nguyen",
            "subject": "Q4 planning — heads up",
            "body": "No rush — Monday works too if Friday is gone.",
            "timestamp": base,
            "source": "seed",
        })


# -- process-wide singleton (mirrors the P0 attention service) ---------------
_service: EmailService | None = None


def get_email_service() -> EmailService:
    global _service
    if _service is None:
        _service = EmailService()
    return _service


def reset_email_service() -> None:
    """Test hook — replace the singleton with a fresh, empty service."""
    global _service
    _service = EmailService()
