"""Domain models for the unified personal communications plane (gaps4 R101).

Implements prompt2 §5–§7 (New Core Entities / Unified Person Model /
Communication Object) and §32 (Cross-Channel Conversation Understanding) for
the canonical ``Person`` → ``ContactIdentity`` → ``Conversation`` →
``Communication`` graph:

- :class:`Person` is the cross-channel identity record (§6): one human, any
  number of :class:`ContactIdentity` links (email, phone, iMessage handle,
  voicemail caller, calendar/contact value). Identity resolution is
  conservative — equal *canonical values* may link, display-name similarity
  alone may not (§6: "Uncertain matches remain separate until confirmed").
- :class:`ContactIdentity` is one normalized (kind, value) link. Records only
  carry the normalized value — never raw credentials, tokens, or message
  content, so any identity record is JSON-safe and audit-loggable.
- :class:`Communication` carries the §7 shape: ``channel`` ∈
  email|imessage|sms|rcs|voicemail, ``direction``, ``sender``,
  ``recipients``, ``timestamp``, ``thread_id``, ``content_ref``,
  ``attachments``, ``classification``, ``sensitivity``, ``source``. Raw
  content lives encrypted outside the knowledge graph (§7); the record keeps
  only a ``content_ref`` pointer. ``sensitivity`` is one of the §36 labels and
  the default routing metadata is ``LOCAL_ONLY`` (§36/§37).
- :class:`Conversation` is the §32 cross-channel conversation: one per
  (canonical person, channel), linking the normalized communications of the
  same human on the same channel into a single evolving interaction.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

# -- allowed channels (prompt2 §7 / AC-R01) ----------------------------------


class CommunicationChannel(StrEnum):
    """Allowed normalized channels for a :class:`Communication` (§7)."""

    EMAIL = "email"
    IMESSAGE = "imessage"
    SMS = "sms"
    RCS = "rcs"
    VOICEMAIL = "voicemail"


# -- sensitivity labels (prompt2 §36 / AC-R01) -------------------------------


class SensitivityLabel(StrEnum):
    """§36 sensitivity labels. Raw communication defaults to ``LOCAL_ONLY``
    routing (§36: "Raw communication defaults to LOCAL_ONLY")."""

    PERSONAL = "PERSONAL"
    PRIVATE = "PRIVATE"
    FINANCIAL = "FINANCIAL"
    WORK = "WORK"
    PROJECT = "PROJECT"
    LOW_SENSITIVITY = "LOW-SENSITIVITY"


#: Default routing metadata for raw communication content (§36/§37).
DEFAULT_CONTENT_ROUTING = "LOCAL_ONLY"


# -- direction (prompt2 §7) ---------------------------------------------------


class CommunicationDirection(StrEnum):
    """Which side of the communication the content originated on (§7)."""

    INCOMING = "incoming"
    OUTGOING = "outgoing"


# -- identity kinds (prompt2 §6) ----------------------------------------------


class ContactIdentityKind(StrEnum):
    """Kinds of normalized identity values a :class:`ContactIdentity` carries.

    ``email`` — email From-address (§8 ingest)
    ``phone`` — phone / relay identifier (Messages, SMS, RCS peer)
    ``imessage`` — iMessage handle (email handle or phone identifier)
    ``voicemail`` — voicemail caller number (§14)
    ``contact`` — calendar/contact book value (§6)
    """

    EMAIL = "email"
    PHONE = "phone"
    IMESSAGE = "imessage"
    VOICEMAIL = "voicemail"
    CONTACT = "contact"


class ContactIdentity(BaseModel):
    """One normalized identity link for a :class:`Person` (prompt2 §6).

    ``value`` is the *normalized* value (case-folded email, E.164-style
    phone, ...); ``raw_value`` keeps the value as first seen, trimmed only.
    Nothing else about the source — no account tokens, no raw content — ever
    lands in this record, so identity records are JSON-safe by construction.
    """

    kind: ContactIdentityKind
    value: str
    raw_value: str | None = None
    display_name: str | None = None
    source: str = "personal-relay"
    first_seen_at: datetime
    last_seen_at: datetime


# -- uncertainty marker (prompt2 §6 — conservative resolution) ----------------


class IdentityMatchStatus(StrEnum):
    """How an ingested identity value resolved against the existing graph."""

    #: Value not seen before — a fresh link/person.
    UNLINKED = "UNLINKED"
    #: Exact canonical value match — linked (idempotent, never a duplicate).
    RESOLVED = "RESOLVED"
    #: Same display name, different canonical value — uncertain; stays
    #: separate until an explicit merge (§6).
    PROPOSED = "PROPOSED"


# -- Person (prompt2 §6) -------------------------------------------------------


class Person(BaseModel):
    """One canonical human across all channels (prompt2 §6 — unified model).

    ``identities`` is the merged set of normalized identity links; the same
    value re-ingested is an idempotent upsert (same person, no duplicate
    identities). ``proposed_merges`` records *uncertain* candidates — persons
    with the same display name but a different canonical value — which stay
    separate until :meth:`backend.communications.registry.merge_people` (the
    ``POST /api/people/{person_id}/merge`` explicit merge, AC-R02) joins them.
    ``is_vip`` carries the existing VIP-flag contract so the P4 contacts plane
    can delegate VIP state to the canonical person (gaps4 R101 step 5).
    """

    person_id: str
    display_name: str | None = None
    is_vip: bool = False
    identities: list[ContactIdentity] = Field(default_factory=list)
    #: person_ids of uncertain (same-name / different-value) candidates.
    proposed_merges: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# -- Communication (prompt2 §7) -------------------------------------------------


class Communication(BaseModel):
    """One normalized cross-channel communication (prompt2 §7 shape).

    The record never carries raw content: ``content_ref`` points at the
    (encrypted, out-of-graph) raw payload (§7: "Raw content may live encrypted
    outside the primary knowledge graph"). ``sensitivity`` is a §36 label,
    ``content_routing`` is the §36/§37 routing metadata (default
    ``LOCAL_ONLY``), and ``classification`` holds the deterministic
    detection verdict (commitment/request/deadline — filled by later tasks).
    """

    communication_id: str
    person_id: str | None = None
    channel: CommunicationChannel
    direction: CommunicationDirection
    sender: str
    recipients: list[str] = Field(default_factory=list)
    timestamp: datetime
    thread_id: str | None = None
    content_ref: str
    attachments: list[str] = Field(default_factory=list)
    classification: dict[str, Any] = Field(default_factory=dict)
    sensitivity: SensitivityLabel = SensitivityLabel.PERSONAL
    content_routing: str = DEFAULT_CONTENT_ROUTING
    source: str = "personal-relay"

    @field_validator("content_ref")
    @classmethod
    def _content_ref_must_not_be_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content_ref must be a non-empty pointer; raw content is never stored")
        return value


# -- Conversation (prompt2 §32) -------------------------------------------------


class Conversation(BaseModel):
    """A cross-channel conversation thread (prompt2 §32).

    One conversation per (canonical person, channel): the normalized
    communications of the same human on the same channel form one evolving
    interaction rather than unrelated inbox items. ``thread_ids`` lists the
    per-channel thread identifiers observed so far (deduped, order
    preserved) so the underlying channel threads remain recoverable.
    """

    conversation_id: str
    person_id: str | None = None
    channel: CommunicationChannel
    thread_ids: list[str] = Field(default_factory=list)
    communication_ids: list[str] = Field(default_factory=list)
    communication_count: int = 0
    started_at: datetime
    last_activity_at: datetime
