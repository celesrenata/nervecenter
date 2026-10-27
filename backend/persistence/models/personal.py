"""ORM models for the durable personal plane (Personal-Ops Recovery §6).

This module maps the frozen migration-006 table set from
``specs/personal-ops-recovery/spec.md`` §6 onto Postgres. It is the durable
backbone that the connector control plane (workstream B) and the mail-ingest
supervisor (workstream C) are built on, so the columns here are the contract.

Conventions (see :mod:`backend.persistence.models.base`):
plural snake_case table names, string UUID ids (``UUIDv4`` — no database UUID
columns), ``Jsonb`` for JSONB, ``DateTime(timezone=True)`` timestamps, and
explicit ``ix_<table>_<column>`` indexes. Enum-like values (channel, state,
urgency, …) are stored as :class:`~sqlalchemy.String` columns, matching the
rest of the platform — no Postgres ``ENUM``/``CHECK`` types, which would break
the SQLite scratch databases the test suite relies on.

Tables
------
- :class:`PersonRow`           — ``people``.
- :class:`PersonIdentityRow`   — ``person_identities``.
- :class:`CommunicationRow`    — ``communications``.
- :class:`ConversationRow`     — ``conversations``.
- :class:`CommitmentRow`       — ``commitments``.
- :class:`FollowupRow`         — ``followups``.
- :class:`AttentionItemRow`    — ``attention_items``.
- :class:`SnoozeRow`           — ``snoozes``.
- :class:`NotificationLogRow`  — ``notification_log``.
- :class:`PreferenceRow`       — ``preferences``.
- :class:`ConnectionRow`       — ``connections``.
- :class:`SecretRow`           — ``secrets`` (encrypted payload).
- :class:`IngestCursorRow`     — ``ingest_cursors``.
- :class:`ImportJobRow`        — ``import_jobs``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Jsonb, UUIDv4


def _created_at() -> Any:
    """Return a ``created_at`` column spec (timestamptz, server ``now()``)."""
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _updated_at() -> Any:
    """Return an ``updated_at`` column spec (timestamptz, server ``now()``)."""
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonRow(Base):
    """A person the user actually cares about.

    Table ``people`` columns
    ------------------------
    - ``id`` (UUID) — record row id.
    - ``created_at`` / ``updated_at`` (timestamptz) — provenance.
    - ``schema_version`` (text) — contract version, ``"v0"``.
    - ``display_name`` (text) — how the person is shown.
    - ``vip`` (bool) — high-priority person flag.
    - ``default_sensitivity`` (text, nullable) — default §36 sensitivity label
      applied to new communications with this person (e.g. ``PERSONAL``).
    - ``notes`` (JSONB) — free-form durable notes.
    - ``boundaries`` (JSONB) — standing boundaries/preferences for the person.
    """

    __tablename__ = "people"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    vip: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_sensitivity: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    notes: Mapped[dict[str, Any]] = mapped_column(Jsonb, default=dict, nullable=False)
    boundaries: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )


class PersonIdentityRow(Base):
    """A channel identity that resolves to a :class:`PersonRow`.

    Table ``person_identities`` columns
    -----------------------------------
    - ``person_id`` (FK ``people.id``) — the person this identity belongs to.
    - ``channel`` (text) — ``email`` / ``imessage`` / ``sms`` / ``voicemail`` /
      ``calendar`` / ``other``.
    - ``handle`` (text) — the raw identity value (address, phone, …).
    - ``resolved`` (bool) — whether the handle has been matched to a person.
    """

    __tablename__ = "person_identities"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    person_id: Mapped[str] = mapped_column(
        String, ForeignKey("people.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    handle: Mapped[str] = mapped_column(String(512), nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (Index("ix_person_identities_person_id", "person_id"),)


class CommunicationRow(Base):
    """One communication with a person — a ref-only record (no raw content).

    Table ``communications`` columns
    ---------------------------------
    - ``person_id`` (FK ``people.id``) — counterpart.
    - ``channel`` (text) — ``email`` / ``imessage`` / ``sms`` / ``voicemail`` /
      ``calendar`` / ``other``.
    - ``direction`` (text) — ``incoming`` / ``outgoing``.
    - ``subject`` (text, nullable) — subject line / first line.
    - ``content_ref`` (text, NOT NULL) — pointer to the encrypted content
      store (§7: raw content lives outside the graph, this keeps the ref).
    - ``sensitivity`` (text, nullable) — §36 sensitivity label.
    - ``received_at`` (timestamptz) — when it was received.
    - ``flags`` (JSONB) — free-form flags (unread, has_deadline, …).
    """

    __tablename__ = "communications"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    person_id: Mapped[str] = mapped_column(
        String, ForeignKey("people.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    sensitivity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    flags: Mapped[dict[str, Any]] = mapped_column(Jsonb, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_communications_person_id", "person_id"),
        Index("ix_communications_received_at", "received_at"),
    )


class ConversationRow(Base):
    """A rolling conversation thread between a person and the user.

    Table ``conversations`` columns
    -------------------------------
    - ``person_id`` (FK ``people.id``) — counterpart.
    - ``channel`` (text) — channel the conversation runs on.
    - ``last_message_at`` (timestamptz, nullable) — most recent message time.
    - ``unread_count`` (int) — messages not yet read.
    """

    __tablename__ = "conversations"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    person_id: Mapped[str] = mapped_column(
        String, ForeignKey("people.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    unread_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (Index("ix_conversations_person_id", "person_id"),)


class CommitmentRow(Base):
    """A commitment the user made to a person that must be tracked.

    Table ``commitments`` columns
    -----------------------------
    - ``person_id`` (FK ``people.id``) — the person the commitment is owed to.
    - ``text`` (text) — the commitment, in plain words.
    - ``due_at`` (timestamptz, nullable) — when it is due.
    - ``state`` (text) — ``open`` / ``done`` / ``dropped`` / ``expired``.
    - ``evidence`` (JSONB) — supporting evidence (refs, quotes, …).
    - ``source_communication_id`` (FK ``communications.id``, nullable) — the
      communication the commitment was detected from.
    """

    __tablename__ = "commitments"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    person_id: Mapped[str] = mapped_column(
        String, ForeignKey("people.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(String(2048), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    source_communication_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("communications.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_commitments_person_id", "person_id"),
        Index("ix_commitments_state", "state"),
    )


class FollowupRow(Base):
    """A loose follow-up to remember, optionally linked to a person.

    Table ``followups`` columns
    ---------------------------
    - ``person_id`` (FK ``people.id``, nullable) — related person, if any.
    - ``text`` (text) — what to follow up on.
    - ``due_at`` (timestamptz, nullable) — when to follow up.
    - ``state`` (text) — ``open`` / ``done`` / ``dropped`` / ``expired``.
    - ``linked_refs`` (JSONB array) — refs linking this follow-up to other
      records (communication ids, attention item ids, …).
    """

    __tablename__ = "followups"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    person_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("people.id", ondelete="SET NULL"), nullable=True
    )
    text: Mapped[str] = mapped_column(String(2048), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    linked_refs: Mapped[list[Any]] = mapped_column(
        Jsonb, default=list, nullable=False
    )

    __table_args__ = (
        Index("ix_followups_person_id", "person_id"),
        Index("ix_followups_state", "state"),
    )


class AttentionItemRow(Base):
    """A prioritized thing that needs the user's attention.

    Table ``attention_items`` columns
    ---------------------------------
    - ``title`` (text) — short headline.
    - ``body`` (text, nullable) — fuller description.
    - ``importance`` (int) — 1..10.
    - ``urgency`` (text) — ``low`` / ``normal`` / ``high`` / ``critical``.
    - ``deadline_at`` (timestamptz, nullable) — when it becomes overdue.
    - ``source_refs`` (JSONB array) — refs to the records that produced it.
    - ``recommended_action`` (text, nullable) — suggested next step.
    - ``escalation_policy`` (JSONB) — how/when to escalate.
    - ``state`` (text) — ``active`` / ``snoozed`` / ``resolved`` / ``dismissed``.
    - ``snoozed_until`` (timestamptz, nullable) — snooze expiry.
    - ``resolved_at`` (timestamptz, nullable) — when it was resolved.
    - ``resolution_condition`` (text, nullable) — what counts as resolved.
    """

    __tablename__ = "attention_items"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    importance: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    urgency: Mapped[str] = mapped_column(
        String(16), default="normal", nullable=False
    )
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_refs: Mapped[list[Any]] = mapped_column(
        Jsonb, default=list, nullable=False
    )
    recommended_action: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    escalation_policy: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )
    snoozed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_condition: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )

    __table_args__ = (
        Index("ix_attention_items_state", "state"),
        Index("ix_attention_items_deadline_at", "deadline_at"),
    )


class SnoozeRow(Base):
    """A single snooze event against an attention item.

    Table ``snoozes`` columns
    -------------------------
    - ``attention_item_id`` (FK ``attention_items.id``) — the item snoozed.
    - ``snooze_until`` (timestamptz) — when the snooze lifts.
    - ``reason`` (text, nullable) — why it was snoozed.
    """

    __tablename__ = "snoozes"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    attention_item_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("attention_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    snooze_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    __table_args__ = (
        Index("ix_snoozes_attention_item_id", "attention_item_id"),
    )


class NotificationLogRow(Base):
    """An audit record that a notification was sent for an attention item.

    Table ``notification_log`` columns
    ---------------------------------
    - ``attention_item_id`` (FK ``attention_items.id``) — the item notified.
    - ``level`` (text) — the notification level (e.g. ``normal`` / ``critical``).
    - ``sent_at`` (timestamptz) — when it was sent.
    - ``channel`` (text, nullable) — delivery channel.

    The unique ``(attention_item_id, level)`` index lets callers dedup to one
    notification per (item, level) — see ``attention_repo.already_notified``.
    """

    __tablename__ = "notification_log"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    attention_item_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("attention_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index(
            "ix_notification_log_item_level",
            "attention_item_id",
            "level",
            unique=True,
        ),
        Index("ix_notification_log_attention_item_id", "attention_item_id"),
    )


class PreferenceRow(Base):
    """A durable user preference, keyed by a unique key.

    Table ``preferences`` columns
    -----------------------------
    - ``key`` (text, UNIQUE) — the preference name.
    - ``value`` (JSONB) — the preference value.
    """

    __tablename__ = "preferences"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    key: Mapped[str] = mapped_column(String(256), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )

    __table_args__ = (
        Index("ix_preferences_key", "key", unique=True),
    )


class ConnectionRow(Base):
    """A configured personal-plane connection (email, iMessage relay, …).

    Table ``connections`` columns
    -----------------------------
    - ``kind`` (text) — connector kind id (e.g. ``gmail`` / ``imap`` /
      ``imessage`` / ``calendar``).
    - ``label`` (text, nullable) — display label.
    - ``auth_status`` (text) — ``unconfigured`` / ``pairing_pending`` /
      ``authenticated`` / ``auth_expired`` / ``auth_failed``.
    - ``operational_status`` (text) — ``never_synced`` / ``syncing`` /
      ``healthy`` / ``degraded`` / ``error``.
    - ``last_success_at`` / ``last_attempt_at`` (timestamptz, nullable) —
      sync bookkeeping.
    - ``last_error_code`` / ``last_error_message`` (text, nullable) — last
      failure detail for actionable diagnostics.
    - ``sync_cursor`` (JSONB) — durable resume position (see
      ``ingest_cursors`` for fine-grained per-kind cursors).
    - ``import_progress`` (JSONB) — bulk-import progress snapshot.
    - ``enabled`` (bool) — whether the connection is active.
    - ``config`` (JSONB) — **non-secret** connection configuration. Secrets
      are stored in :class:`SecretRow`, never here.
    """

    __tablename__ = "connections"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    auth_status: Mapped[str] = mapped_column(
        String(32), default="unconfigured", nullable=False
    )
    operational_status: Mapped[str] = mapped_column(
        String(32), default="never_synced", nullable=False
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    sync_cursor: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    import_progress: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )

    __table_args__ = (
        Index("ix_connections_enabled", "enabled"),
        Index("ix_connections_kind", "kind"),
    )


class SecretRow(Base):
    """An encrypted secret tied to an owner (connection, person, …).

    Table ``secrets`` columns
    -------------------------
    - ``kind`` (text) — secret kind (e.g. ``access_token`` / ``app_password``).
    - ``owner_kind`` (text) — owner type (``connection`` / ``person`` / …).
    - ``owner_id`` (text) — the owner's id.
    - ``payload`` (JSONB) — the **encrypted** payload bytes (codec output).
      The actual encryption hook (Fernet) is supplied by the connector
      vault (workstream B); see ``connections_repo.SecretCodec``.

    Plaintext is never stored here; only the codec's ciphertext.
    """

    __tablename__ = "secrets"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )

    __table_args__ = (
        Index("ix_secrets_owner", "owner_kind", "owner_id"),
        Index("ix_secrets_kind", "kind"),
    )


class IngestCursorRow(Base):
    """A durable per-connection, per-kind ingestion cursor.

    Table ``ingest_cursors`` columns
    --------------------------------
    - ``connection_id`` (FK ``connections.id``) — the owning connection.
    - ``kind`` (text) — ``imap_seq`` / ``gmail_history`` / ``relay_seq``.
    - ``cursor_value`` (JSONB) — the opaque resume position.
    - ``updated_at`` (timestamptz) — last cursor advance.

    Uniqueness by ``(connection_id, kind)``: one cursor per (connection,
    kind) pair.
    """

    __tablename__ = "ingest_cursors"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    connection_id: Mapped[str] = mapped_column(
        String, ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    cursor_value: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_ingest_cursors_connection_kind",
            "connection_id",
            "kind",
            unique=True,
        ),
        Index("ix_ingest_cursors_connection_id", "connection_id"),
    )


class ImportJobRow(Base):
    """A tracked bulk-import job for a connection.

    Table ``import_jobs`` columns
    -----------------------------
    - ``connection_id`` (FK ``connections.id``) — the owning connection.
    - ``kind`` (text) — import kind (e.g. ``mail_history`` / ``contacts``).
    - ``state`` (text) — ``pending`` / ``running`` / ``done`` / ``failed``.
    - ``progress`` (JSONB) — progress detail (current/total breakdowns).
    - ``total`` (int, nullable) — expected item count, if known.
    - ``started_at`` / ``finished_at`` (timestamptz, nullable) — lifecycle.
    - ``error_code`` (text, nullable) — failure code, if any.
    """

    __tablename__ = "import_jobs"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    connection_id: Mapped[str] = mapped_column(
        String, ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )
    progress: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_import_jobs_connection_id", "connection_id"),
        Index("ix_import_jobs_state", "state"),
    )


# ---------------------------------------------------------------------------
# Calendar plane (Personal-Ops Recovery §8 — workstream E).
#
# The freeze (§6 migration note) assigns ``calendar_accounts`` and
# ``calendar_events`` to the calendar migration (listed as 010 in the freeze
# text; in the current linear chain — head ``006_personal_plane`` — there are
# no intervening 007-009 revisions, so the tables are created by
# ``007_calendar``). Neither table exists in ``006_personal_plane.py`` nor in
# this module, so the "minimal extension" this wave is the two ORM classes
# below, matching the frozen column set:
#
# - ``calendar_accounts`` — account / provider / credential refs / timezone /
#   non-secret config / durable ``sync_state`` / status liveness.
# - ``calendar_events`` — per-account, per-``ical_uid`` durable event copy:
#   start / end / all-day / summary / location / description / timezone /
#   change marker / appt flag / explicit deleted marker (§8).
# ---------------------------------------------------------------------------


class CalendarAccountRow(Base):
    """A connected calendar account (Google / CalDAV / Apple relay).

    Table ``calendar_accounts`` columns
    -----------------------------------
    - ``id`` (UUID) — record row id.
    - ``provider`` (text) — ``google`` / ``caldav`` / ``apple``.
    - ``label`` — display label ("Personal", "Work", …).
    - ``account_ref`` — reference to the upstream account (email address,
      DAV url, relay account id). Non-secret.
    - ``secret_ref`` — reference to the credential held by the connector
      secrets vault (workstream B); ``None`` for relay-only providers.
      Never a credential value.
    - ``timezone`` — IANA zone used to resolve floating (wall-clock) times
      the provider does not tag (freeze §8 "Timezone correctness").
    - ``config`` (JSONB) — non-secret provider config (e.g. the Google
      calendar ids to subscribe, the CalDAV url, the relay ingest path).
    - ``sync_state`` (JSONB) — durable per-account sync cursor: last sync
      times, per-calendar sync tokens (Google), etags (CalDAV).
    - ``enabled`` — whether the sync runner picks the account up.
    - ``operational_status`` — ``never_synced`` / ``syncing`` / ``healthy`` /
      ``degraded`` / ``error`` (spec §3 two-axis status).
    - ``last_success_at`` / ``last_attempt_at`` / ``last_error_code`` /
      ``last_error_message`` (redacted) — sync liveness.
    - ``created_at`` / ``updated_at``.
    """

    __tablename__ = "calendar_accounts"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    config: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    sync_state: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    operational_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="never_synced"
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )

    __table_args__ = (
        Index("ix_calendar_accounts_provider", "provider"),
        Index("ix_calendar_accounts_enabled", "enabled"),
    )


class CalendarEventRow(Base):
    """A durable, read-only copy of a provider calendar event.

    Table ``calendar_events`` columns
    ---------------------------------
    - ``id`` (UUID) — record row id.
    - ``account_id`` (FK ``calendar_accounts.id``) — owning account.
    - ``ical_uid`` — the provider's stable event identity (RFC5545 ``UID``);
      upsert key together with ``account_id``. Recurring instances are
      materialized per occurrence as ``<uid>@<date>``.
    - ``start`` / ``end`` — tz-aware UTC datetimes (timezone correctness);
      ``start`` is nullable alongside ``all_day`` for day events.
    - ``all_day`` — day-granularity event.
    - ``title`` — ``SUMMARY``.
    - ``location`` / ``description``.
    - ``timezone`` — the IANA zone / VTIMEZONE name the times were *authored*
      in (kept for faithful re-rendering); ``start``/``end`` are stored
      normalized to UTC.
    - ``changed_at`` — the provider's change marker (Google ``updated``,
      CalDAV ``DTSTAMP``/``etag``) for incremental change detection.
    - ``appt`` — whether this is an appointment (invitations, meetings);
      the flag row 7 ("appointment appears on Today") keys on.
    - ``deleted`` — explicit provider deletion marker; the sync runner acts
      on it (removes the row and the in-memory copy) — events are never
      silently dropped.
    - ``status`` — provider status (``confirmed`` / ``tentative`` /
      ``cancelled``).
    - ``extra`` (JSONB) — provider-specific fields (participants,
      reminders, raw RRULE).
    - ``created_at`` / ``updated_at``.
    """

    __tablename__ = "calendar_events"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    account_id: Mapped[str] = mapped_column(
        ForeignKey("calendar_accounts.id"), nullable=False
    )
    ical_uid: Mapped[str] = mapped_column(String(512), nullable=False)
    start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    changed_at: Mapped[str | None] = mapped_column(String(128), nullable=True)
    appt: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="confirmed"
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )

    __table_args__ = (
        Index("ix_calendar_events_account_id", "account_id"),
        Index("ix_calendar_events_ical_uid", "ical_uid"),
        Index("ix_calendar_events_start", "start"),
    )
