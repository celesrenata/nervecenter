"""In-memory unified communications registry (gaps4 R101 — AC-R01/AC-R02).

This is the NerveCenter-side *canonical personal graph* (prompt2 §5–§7,
§32): one process-wide singleton holding :class:`~backend.communications.models.Person`,
:class:`~backend.communications.models.ContactIdentity`,
:class:`~backend.communications.models.Communication`, and the cross-channel
:class:`~backend.communications.models.Conversation` objects, mirroring the
house style of the P0–P4 personal planes (``attention``, ``contacts``,
``messages``, ``email``, ``voicemail``): in-process store, singleton getter,
``reset_*`` test hook, no persistence, no DB.

Design notes
------------
- **Value-first, conservative resolution (§6 / AC-R02).** The identity index
  is keyed by the *canonical value only* (not the kind): an email address,
  a phone/relay identifier, a voicemail caller, or a calendar/contact value
  that normalizes to the same canonical value links to the *same* person,
  idempotently. Display-name similarity alone may **not** merge: an
  uncategorized match (same display name, different canonical value) yields
  a separate person flagged ``PROPOSED`` with a ``proposed_merges`` cross
  link. Only :meth:`CommunicationsService.merge_people` — the explicit
  ``POST /api/people/{person_id}/merge`` action — joins two persons.
- **JSON-safe by construction.** Identity records carry only the normalized
  value, the trimmed raw value, an optional display name, provenance, and
  timestamps — never raw message content, credentials, or tokens. URL-ish
  garbage (``?token=…`` fragments) is cut off email-style values before
  storage. Communication records carry only a ``content_ref`` pointer; raw
  content lives encrypted outside the knowledge graph (§7).
- **Idempotent upserts.** Re-ingesting a known canonical value returns the
  existing person (``created=False``, no duplicate identity). Re-ingesting
  a known ``communication_id`` updates the existing record.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
    ContactIdentity,
    ContactIdentityKind,
    Conversation,
    IdentityMatchStatus,
    Person,
    SensitivityLabel,
)

_NON_DIGITS = re.compile(r"[^\d+]+")


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime | str | None, *, default: datetime | None = None) -> datetime:
    """Coerce a datetime/string to an aware UTC datetime (house style)."""
    if value is None:
        if default is not None:
            return default
        return _now()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def normalize_identity_value(kind: ContactIdentityKind | str, value: Any) -> str:
    """Canonical form of an identity value (value-first resolution, §6).

    - ``email``: case-folded; URL-ish fragments (``?…`` / ``#…``) are cut —
      an email address never carries them, so an embedded query token is
      never stored in an identity record.
    - ``phone`` / ``voicemail``: digits only, ``+`` kept as an E.164
      international prefix (``00`` prefix folded to ``+``).
    - ``imessage``: handle — case-folded when it is an email handle,
      digit-normalized when it is a phone identifier.
    - ``contact``: case-folded, whitespace-collapsed display value.

    Raises ``ValueError`` on an empty value — an identity must carry a
    value to be a link.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("identity value must be non-empty")

    # ``str()`` of a StrEnum member is its value (py3.11+ StrEnum); plain
    # strings pass through unchanged, so this handles both call styles.
    kind_str = str(kind).strip().lower()
    if kind_str == ContactIdentityKind.EMAIL.value:
        canonical = raw.split("?", 1)[0].split("#", 1)[0].strip().lower()
    elif kind_str in (ContactIdentityKind.PHONE.value, ContactIdentityKind.VOICEMAIL.value):
        digits = _NON_DIGITS.sub("", raw)
        if digits.startswith("+"):
            canonical = "+" + digits[1:]
        elif digits.startswith("00"):
            canonical = "+" + digits[2:]
        else:
            canonical = digits
    elif kind_str == ContactIdentityKind.IMESSAGE.value:
        if "@" in raw:
            canonical = raw.split("?", 1)[0].split("#", 1)[0].strip().lower()
        else:
            digits = _NON_DIGITS.sub("", raw)
            if digits.startswith("00"):
                canonical = "+" + digits[2:]
            else:
                canonical = digits
    else:  # contact / calendar values
        canonical = " ".join(raw.lower().split())
    if not canonical:
        raise ValueError("identity value must be non-empty after normalization")
    return canonical


def _clean_raw_value(kind: ContactIdentityKind, value: Any) -> str:
    """The trimmed raw value kept on an identity record.

    The value as first seen, trimmed — but, like the canonical value, URL-ish
    fragments (``?…`` / ``#…``) are cut from email-style values (``email``,
    and email-handle ``imessage``) before storage, so an embedded query token
    never lands in an identity record (§6: records carry no raw secrets).
    """
    raw = str(value or "").strip()
    kind_str = str(kind).strip().lower()
    if kind_str in (ContactIdentityKind.EMAIL.value, ContactIdentityKind.IMESSAGE.value) and "@" in raw:
        raw = raw.split("?", 1)[0].split("#", 1)[0].strip()
    return raw


def _slug(value: str) -> str:
    """Stable, URL-safe slug for derived person ids."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug[:48] or "unknown"


def _derive_person_id(display_name: str | None, value: str, taken: set[str]) -> str:
    base = _slug(display_name or value)
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


#: Channel → identity kind used when a communication must be attributed to a
#: person by its counterpart value (email From, relay peer, voicemail caller).
_CHANNEL_PEER_KIND: dict[CommunicationChannel, ContactIdentityKind] = {
    CommunicationChannel.EMAIL: ContactIdentityKind.EMAIL,
    CommunicationChannel.IMESSAGE: ContactIdentityKind.IMESSAGE,
    CommunicationChannel.SMS: ContactIdentityKind.PHONE,
    CommunicationChannel.RCS: ContactIdentityKind.PHONE,
    CommunicationChannel.VOICEMAIL: ContactIdentityKind.VOICEMAIL,
}


class CommunicationsService:
    """In-memory Person/ContactIdentity/Communication/Conversation registry."""

    def __init__(self) -> None:
        self._people: dict[str, Person] = {}
        self._identity_index: dict[str, str] = {}
        self._communications: dict[str, Communication] = {}
        self._conversations: dict[str, Conversation] = {}

    # -- identity ingestion (AC-R01: idempotent upsert; AC-R02: resolution) --
    def resolve_identity(
        self, kind: ContactIdentityKind | str, value: Any
    ) -> tuple[Person | None, IdentityMatchStatus, list[str]]:
        """Map one raw identity value to its canonical person, conservatively.

        Returns ``(person, status, proposed_candidates)``:

        - ``RESOLVED`` — an equal canonical value is already linked; the
          same person is returned (value-first, §6).
        - ``PROPOSED`` — no equal value, but a person with the same display
          name exists; those candidates are returned *separately* and are
          never joined automatically.
        - ``UNLINKED`` — nothing matches; the value is new.
        """
        canonical = normalize_identity_value(kind, value)
        person_id = self._identity_index.get(canonical)
        if person_id is not None:
            person = self._people.get(person_id)
            if person is not None:
                return person, IdentityMatchStatus.RESOLVED, []

        proposed: list[str] = []
        display = " ".join(str(value or "").strip().lower().split())
        for pid, candidate in self._people.items():
            if candidate.display_name and display and candidate.display_name.lower() == display:
                proposed.append(pid)
        if proposed:
            return None, IdentityMatchStatus.PROPOSED, proposed
        return None, IdentityMatchStatus.UNLINKED, []

    def ingest_identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Link one normalized identity value to its canonical person.

        Returns ``{"person", "identity", "created", "status",
        "proposed_candidates"}``. Re-ingesting the same canonical value is
        an idempotent upsert: the same person is returned, no duplicate
        identity is created, ``created=False`` (AC-R01). Same-name /
        different-value results create a separate person flagged
        ``PROPOSED`` and are never merged automatically (AC-R02).

        ``payload`` keys: ``kind``, ``value`` (required); ``person_id``,
        ``display_name``, ``source``, ``timestamp`` (optional). Unknown
        keys are ignored — the record never stores what it does not need
        (no raw content, no credentials).
        """
        kind = payload["kind"]
        if not isinstance(kind, ContactIdentityKind):
            kind = ContactIdentityKind(str(kind).lower())
        value = payload["value"]
        canonical = normalize_identity_value(kind, value)
        timestamp = _ensure_utc(payload.get("timestamp"), default=_now())
        display_name = str(payload.get("display_name") or "").strip() or None
        source = str(payload.get("source") or "personal-relay")

        # Value-first: an equal canonical value already links a person.
        linked_person_id = self._identity_index.get(canonical)
        if linked_person_id is not None:
            existing = self._people[linked_person_id]
            identity = next(i for i in existing.identities if i.value == canonical)
            identity.last_seen_at = timestamp
            if display_name and not existing.display_name:
                existing.display_name = display_name
            existing.updated_at = timestamp
            return {
                "person": existing,
                "identity": identity,
                "created": False,
                "status": IdentityMatchStatus.RESOLVED,
                "proposed_candidates": [],
            }

        explicit_person_id = str(payload.get("person_id") or "").strip()

        if explicit_person_id:
            person = self._people.get(explicit_person_id)
            if person is None:
                raise KeyError(explicit_person_id)
            if display_name and not person.display_name:
                person.display_name = display_name
            status = IdentityMatchStatus.RESOLVED
            proposed_candidates: list[str] = []
            created_person = False
        else:
            # Conservative: same display name with a *different* canonical
            # value is an uncertain match — a separate person, not a merge.
            proposed_candidates = []
            display = (display_name or "").lower()
            if display:
                for pid, candidate in self._people.items():
                    if candidate.display_name and candidate.display_name.lower() == display:
                        proposed_candidates.append(pid)
            person = Person(
                person_id=_derive_person_id(display_name, canonical, set(self._people)),
                display_name=display_name,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._people[person.person_id] = person
            for candidate_id in proposed_candidates:
                candidate = self._people[candidate_id]
                candidate.proposed_merges.append(person.person_id)
                candidate.updated_at = timestamp
            person.proposed_merges.extend(proposed_candidates)
            status = (
                IdentityMatchStatus.PROPOSED
                if proposed_candidates
                else IdentityMatchStatus.UNLINKED
            )
            created_person = True

        identity = ContactIdentity(
            kind=kind,
            value=canonical,
            raw_value=_clean_raw_value(kind, value),
            display_name=display_name,
            source=source,
            first_seen_at=timestamp,
            last_seen_at=timestamp,
        )
        person.identities.append(identity)
        self._identity_index[canonical] = person.person_id
        person.updated_at = timestamp
        return {
            "person": person,
            "identity": identity,
            "created": created_person,
            "status": status,
            "proposed_candidates": proposed_candidates,
        }

    # -- explicit merge (AC-R02) -------------------------------------------------
    def merge_people(self, target_person_id: str, other_person_id: str) -> Person:
        """Explicitly merge ``other`` into ``target`` (§6: merges require an
        explicit action — this is the service half of
        ``POST /api/people/{person_id}/merge``).

        Identities are re-linked onto the target (equal canonical values
        dedupe to the target's record), communications are re-attributed,
        per-channel conversations are folded together, and
        ``proposed_merges`` cross-links are dropped. The source person is
        removed; the target is returned.
        """
        target = self._people.get(target_person_id)
        if target is None:
            raise KeyError(target_person_id)
        other = self._people.get(other_person_id)
        if other is None:
            raise KeyError(other_person_id)
        if target_person_id == other_person_id:
            raise ValueError("cannot merge a person into itself")

        target.display_name = target.display_name or other.display_name
        # VIP state is preserved on the surviving person (R101 step 5: the
        # existing VIP contacts flag delegates to the canonical person).
        target.is_vip = target.is_vip or other.is_vip

        existing_values = {i.value for i in target.identities}
        for identity in other.identities:
            if identity.value in existing_values:
                kept = next(i for i in target.identities if i.value == identity.value)
                kept.last_seen_at = max(kept.last_seen_at, identity.last_seen_at)
            else:
                target.identities.append(identity)
                existing_values.add(identity.value)
            self._identity_index[identity.value] = target_person_id

        for communication in self._communications.values():
            if communication.person_id == other_person_id:
                communication.person_id = target_person_id
                self._fold_conversation(communication)

        # Snapshot: folding below may delete/insert conversations.
        for conversation in list(self._conversations.values()):
            if conversation.person_id == other_person_id:
                conversation.person_id = target_person_id
                target_conversation = self._conversation_for(
                    target_person_id, conversation.channel
                )
                if target_conversation.conversation_id != conversation.conversation_id:
                    for thread_id in conversation.thread_ids:
                        if thread_id not in target_conversation.thread_ids:
                            target_conversation.thread_ids.append(thread_id)
                    for comm_id in conversation.communication_ids:
                        if comm_id not in target_conversation.communication_ids:
                            target_conversation.communication_ids.append(comm_id)
                    target_conversation.communication_count = len(
                        target_conversation.communication_ids
                    )
                    target_conversation.started_at = min(
                        target_conversation.started_at, conversation.started_at
                    )
                    target_conversation.last_activity_at = max(
                        target_conversation.last_activity_at,
                        conversation.last_activity_at,
                    )
                    del self._conversations[conversation.conversation_id]

        for pid in (target_person_id, other_person_id):
            self._people[pid].proposed_merges = [
                p for p in self._people[pid].proposed_merges if p not in (target_person_id, other_person_id)
            ]
        del self._people[other_person_id]
        target.updated_at = _now()
        return target

    # -- communication ingestion (AC-R01 §7 shape) --------------------------------
    def ingest_communication(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Upsert one normalized communication and fold it into its
        conversation (per canonical person + channel, §32).

        ``payload`` keys: ``channel``, ``direction``, ``sender``,
        ``timestamp``, ``content_ref`` (required); ``communication_id``,
        ``person_id``, ``recipients``, ``thread_id``, ``attachments``,
        ``classification``, ``sensitivity``, ``source`` (optional). When
        ``person_id`` is omitted the registry resolves the counterpart
        value (sender for incoming, first recipient for outgoing) against
        the identity index — uncertain matches leave the communication
        unlinked rather than guessing.
        """
        channel = payload["channel"]
        if not isinstance(channel, CommunicationChannel):
            channel = CommunicationChannel(str(channel).lower())
        direction = payload["direction"]
        if not isinstance(direction, CommunicationDirection):
            direction = CommunicationDirection(str(direction).lower())
        sender = str(payload["sender"]).strip()
        recipients = [str(r).strip() for r in (payload.get("recipients") or []) if str(r).strip()]
        timestamp = _ensure_utc(payload.get("timestamp"), default=_now())
        communication_id = str(payload.get("communication_id") or "").strip() or f"comm-{uuid4().hex[:12]}"

        person_id = self._attribute_person(
            person_id=payload.get("person_id"),
            channel=channel,
            direction=direction,
            sender=sender,
            recipients=recipients,
        )

        existing = self._communications.get(communication_id)
        if existing is not None:
            created = False
            existing.channel = channel
            existing.direction = direction
            existing.sender = sender
            existing.recipients = recipients
            existing.timestamp = timestamp
            if payload.get("thread_id"):
                existing.thread_id = str(payload["thread_id"]).strip()
            existing.content_ref = str(payload["content_ref"]).strip()
            existing.attachments = [str(a).strip() for a in (payload.get("attachments") or []) if str(a).strip()]
            if payload.get("classification") is not None:
                existing.classification = dict(payload["classification"])
            if payload.get("sensitivity") is not None:
                existing.sensitivity = SensitivityLabel(str(payload["sensitivity"]))
            if payload.get("source"):
                existing.source = str(payload["source"])
            if person_id is not None:
                existing.person_id = person_id
        else:
            created = True
            existing = Communication(
                communication_id=communication_id,
                person_id=person_id,
                channel=channel,
                direction=direction,
                sender=sender,
                recipients=recipients,
                timestamp=timestamp,
                thread_id=(str(payload["thread_id"]).strip() if payload.get("thread_id") else None),
                content_ref=str(payload["content_ref"]).strip(),
                attachments=[str(a).strip() for a in (payload.get("attachments") or []) if str(a).strip()],
                classification=dict(payload.get("classification") or {}),
                sensitivity=SensitivityLabel(str(payload.get("sensitivity") or SensitivityLabel.PERSONAL.value)),
                source=str(payload.get("source") or "personal-relay"),
            )
            self._communications[communication_id] = existing

        conversation = self._fold_conversation(existing)
        return {
            "communication": existing,
            "conversation": conversation,
            "created": created,
        }

    def _attribute_person(
        self,
        *,
        person_id: Any,
        channel: CommunicationChannel,
        direction: CommunicationDirection,
        sender: str,
        recipients: list[str],
    ) -> str | None:
        if person_id:
            pid = str(person_id)
            if pid not in self._people:
                raise KeyError(pid)
            return pid
        counterpart = (
            sender
            if direction is CommunicationDirection.INCOMING
            else (recipients[0] if recipients else sender)
        )
        if not counterpart:
            return None
        kind = _CHANNEL_PEER_KIND[channel]
        person, status, _proposed = self.resolve_identity(kind, counterpart)
        if status is not IdentityMatchStatus.RESOLVED:
            return None  # PROPOSED / UNLINKED — never guess.
        return person.person_id if person is not None else None

    def _conversation_for(self, person_id: str | None, channel: CommunicationChannel) -> Conversation:
        key_person = person_id or "unknown"
        conversation_id = f"conv-{key_person}-{channel.value}"
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            conversation = Conversation(
                conversation_id=conversation_id,
                person_id=person_id,
                channel=channel,
                started_at=_now(),
                last_activity_at=_now(),
            )
            self._conversations[conversation_id] = conversation
        return conversation

    def _fold_conversation(self, communication: Communication) -> Conversation:
        conversation = self._conversation_for(communication.person_id, communication.channel)
        if communication.communication_id not in conversation.communication_ids:
            conversation.communication_ids.append(communication.communication_id)
        if communication.thread_id and communication.thread_id not in conversation.thread_ids:
            conversation.thread_ids.append(communication.thread_id)
        conversation.communication_count = len(conversation.communication_ids)
        conversation.last_activity_at = max(
            conversation.last_activity_at, communication.timestamp
        )
        if communication.timestamp < conversation.started_at:
            conversation.started_at = communication.timestamp
        if communication.person_id is not None:
            conversation.person_id = communication.person_id
        return conversation

    # -- queries -----------------------------------------------------------------
    def list_people(self) -> list[Person]:
        return sorted(
            self._people.values(),
            key=lambda p: ((p.display_name or p.person_id).lower(), p.person_id),
        )

    def get_person(self, person_id: str) -> Person | None:
        return self._people.get(person_id)

    def list_communications(
        self, person_id: str | None = None, channel: CommunicationChannel | None = None
    ) -> list[Communication]:
        result = [
            c
            for c in self._communications.values()
            if (person_id is None or c.person_id == person_id)
            and (channel is None or c.channel is channel)
        ]
        return sorted(result, key=lambda c: (c.timestamp, c.communication_id))

    def get_communication(self, communication_id: str) -> Communication | None:
        return self._communications.get(communication_id)

    def list_conversations(self, channel: CommunicationChannel | None = None) -> list[Conversation]:
        result = [c for c in self._conversations.values() if channel is None or c.channel is channel]
        return sorted(result, key=lambda c: (c.last_activity_at, c.conversation_id))

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)

    def get_conversation_communications(self, conversation_id: str) -> list[Communication]:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            return []
        return [
            c
            for c in sorted(self._communications.values(), key=lambda x: (x.timestamp, x.communication_id))
            if c.communication_id in set(conversation.communication_ids)
        ]

    def reset(self) -> None:
        """Clear every store (used by the singleton reset hook)."""
        self._people.clear()
        self._identity_index.clear()
        self._communications.clear()
        self._conversations.clear()


# -- process-wide singleton (mirrors the P0–P4 personal planes) ----------------
_service: CommunicationsService | None = None


def get_communications_service() -> CommunicationsService:
    global _service
    if _service is None:
        _service = CommunicationsService()
    return _service


def reset_communications_service() -> None:
    """Test hook — replace the singleton with a fresh, empty service."""
    global _service
    _service = CommunicationsService()
