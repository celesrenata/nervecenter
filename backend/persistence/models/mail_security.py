"""ORM models for the NerveShield durable security plane (Phase 1, Task 1.7).

This is the **first** persistence task for NerveShield. It maps the *minimum*
table set from ``.kiro/specs/nerve-shield/design.md`` §9 ("Data storage and
interfaces") onto the durable plane: the account-scoped security job, the
evidence manifest, and the assessment / finding metadata. Per design §9 we do
**not** create the intel / dataset / model / action tables in Phase 1 — those
arrive behind later gated migrations.

Hard constraints carried into these tables (steering + threat-model):

- **No raw content, ever.** No raw subject, body, attachment bytes, email
  addresses, authentication tokens or PII lives in any column here. Rows carry
  only metadata, safe upper-snake *codes*, hashes and **opaque reference
  strings** (the ``content_ref`` / ``evidence_ref`` pointers produced by the
  Task 1.1/1.2 contracts). A ``content_ref`` is an opaque pointer into the
  existing object store; it does **not** itself prove the referent is
  encrypted (Phase 0 found no SSE/client crypto in code), so Zone-P0 raw
  payloads never land in SQL — only their refs (design §10, T-14).
- **Account isolation is explicit.** The existing ``communications`` table has
  no account column (an R-023/T-15 gap). Every table here carries an explicit
  ``account_scope`` column with an index, modelled on
  ``calendar_events.account_id`` + ``ix_calendar_events_account_id``. The
  repository access-checks key on it so account A can never read account B's
  row even given A's exact source id / assessment id / content hash
  (R-023.4, T-15).
- **Idempotent logical identity.** The deterministic ids from Task 1.2
  (``outbox_job_key`` / ``assessment_id``) are the row identity, so replay
  after a crash resolves to the same row rather than duplicating work
  (R-018, T-17).

Conventions mirror :mod:`backend.persistence.models.personal`: plural
snake_case table names, string UUID ids (:data:`UUIDv4`), ``Jsonb`` for JSONB,
``DateTime(timezone=True)`` timestamps, enum-like values stored as capped
:class:`~sqlalchemy.String` columns (no Postgres ``ENUM``/``CHECK`` — the test
suite runs on SQLite scratch DBs), and explicit ``ix_<table>_<column>``
indexes.

Tables
------
- :class:`MailSecurityJobRow`         — ``mail_security_jobs``.
- :class:`MailEvidenceRow`            — ``mail_evidence``.
- :class:`MailSecurityAssessmentRow`  — ``mail_security_assessments``.
- :class:`MailSecurityFindingRow`     — ``mail_security_findings``.
- :class:`MailIntelIndicatorRow`      — ``mail_intel_indicators`` (Task 2.2).
- :class:`MailIntelOverrideRow`       — ``mail_intel_overrides`` (Task 2.2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
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


class MailSecurityJobRow(Base):
    """A durable, restartable account-scoped security analysis job.

    Table ``mail_security_jobs`` columns
    ------------------------------------
    - ``id`` (UUID) — record row id.
    - ``account_scope`` (text) — the cross-account isolation boundary
      (R-023). Carried explicitly and indexed, mirroring
      ``calendar_events.account_id``.
    - ``outbox_job_key`` (text) — the deterministic Task 1.2 outbox job key
      ``(account_scope, message_key, analysis_profile_version)``. Unique
      together with ``account_scope`` so a replay resolves to the same job
      rather than scheduling a duplicate (R-018.2, T-17).
    - ``source_key`` (text) — the account-scoped logical source key (Task 1.2
      ``logical_source_key``). Opaque; never a raw Message-ID.
    - ``analysis_profile_version`` (text) — the analysis-profile/version token
      that, with the source key, fixes the job's logical identity.
    - ``state`` (text) — processing state (e.g. ``DISCOVERED`` /
      ``EVIDENCE_READY`` / ``ASSESSED`` / ``DEAD_LETTER``). Stored as a capped
      string, matching the rest of the platform.
    - ``attempts`` (int) — delivery/processing attempts, for bounded retry.
    - ``lease_owner`` (text, nullable) — opaque worker lease holder id; no PII.
    - ``lease_expires_at`` (timestamptz, nullable) — when the lease lapses so a
      crashed worker's job can be recovered (T-17).
    - ``last_error_code`` (text, nullable) — a safe failure *code*, never a raw
      exception message / body fragment.
    - ``meta`` (JSONB) — bounded, safe structured metadata (no raw content).
    """

    __tablename__ = "mail_security_jobs"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    outbox_job_key: Mapped[str] = mapped_column(String(200), nullable=False)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    analysis_profile_version: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(32), default="DISCOVERED", nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )

    __table_args__ = (
        Index("ix_mail_security_jobs_account_scope", "account_scope"),
        Index("ix_mail_security_jobs_state", "state"),
        # The outbox job key is unique *within* an account scope. Scoping the
        # unique index by account_scope keeps the isolation boundary structural
        # (two accounts can hold byte-identical keys without colliding).
        Index(
            "ix_mail_security_jobs_scope_key",
            "account_scope",
            "outbox_job_key",
            unique=True,
        ),
    )


class MailEvidenceRow(Base):
    """An immutable per-revision evidence manifest (design §4.1/§9).

    Table ``mail_evidence`` columns
    -------------------------------
    - ``id`` (UUID) — record row id.
    - ``account_scope`` (text) — isolation boundary (R-023), indexed.
    - ``source_id`` (text) — opaque account-scoped source ref (never raw).
    - ``revision`` (int) — the immutable source revision; a rescan creates a
      *new* revision rather than overwriting lineage (design §9).
    - ``provider`` (text) — bounded provider id (e.g. ``gmail`` / ``imap``).
    - ``acquisition_mode`` (text) — how evidence was acquired.
    - ``evidence_fidelity`` (text) — how lossy the capture is, so a lossy
      summary is never mistaken for original signed MIME.
    - ``raw_content_ref`` (text, nullable) — **opaque** pointer to the raw
      payload held in the existing object store. This column holds the *ref*
      only, never the bytes; the ref does not itself guarantee encryption
      (design §10, Phase 0).
    - ``normalized_content_ref`` (text, nullable) — opaque pointer to the
      normalized/parsed evidence, same rule.
    - ``sha256`` (text, nullable) — content hash (64 hex), a fingerprint, not
      an access key.
    - ``missing_parts`` (JSONB) — list of safe codes for missing parts.
    - ``meta`` (JSONB) — bounded safe structured metadata.

    Uniqueness by ``(account_scope, source_id, revision)`` — the design §9
    key — so a replayed manifest for a fixed revision is idempotent.
    """

    __tablename__ = "mail_evidence"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    acquisition_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_fidelity: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_content_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    normalized_content_ref: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    missing_parts: Mapped[list[Any]] = mapped_column(
        Jsonb, default=list, nullable=False
    )
    meta: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )

    __table_args__ = (
        Index("ix_mail_evidence_account_scope", "account_scope"),
        Index(
            "ix_mail_evidence_scope_source_rev",
            "account_scope",
            "source_id",
            "revision",
            unique=True,
        ),
    )


class MailSecurityAssessmentRow(Base):
    """A metadata-only mail security assessment (design §9).

    Table ``mail_security_assessments`` columns
    -------------------------------------------
    - ``id`` (UUID) — record row id.
    - ``assessment_id`` (text) — the deterministic Task 1.2 assessment id
      (depends on source revision + analysis version). Unique together with
      ``account_scope`` so a replay is idempotent (R-018.2, I-11).
    - ``account_scope`` (text) — isolation boundary (R-023), indexed.
    - ``source_id`` (text) — opaque source ref.
    - ``revision`` (int) — the source revision the assessment pins.
    - ``analysis_profile_version`` (text) — analysis version.
    - ``model_version`` (text, nullable) — resolved model id, if any (metadata
      only; never model output text).
    - ``policy_version`` (text) — policy version that produced the disposition.
    - ``processing_state`` (text) — assessment state (e.g. ``ASSESSED`` /
      ``INCOMPLETE``).
    - ``evidence_fidelity`` (text) — fidelity of the evidence assessed.
    - ``mode`` (text) — deployment mode (default ``SHADOW``).
    - ``disposition`` (text) — the restrictive verdict code.
    - ``assessment`` (JSONB) — the full metadata-only
      :class:`~backend.mail_security.models.SecurityAssessment` payload
      (opaque refs + safe codes only, never raw content).

    Uniqueness by ``(account_scope, assessment_id)`` — the design §9 key.
    """

    __tablename__ = "mail_security_assessments"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    assessment_id: Mapped[str] = mapped_column(String(160), nullable=False)
    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    analysis_profile_version: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    model_version: Mapped[str | None] = mapped_column(String(160), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    processing_state: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_fidelity: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(32), default="SHADOW", nullable=False
    )
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    assessment: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )

    __table_args__ = (
        Index("ix_mail_security_assessments_account_scope", "account_scope"),
        Index("ix_mail_security_assessments_disposition", "disposition"),
        Index(
            "ix_mail_security_assessments_scope_asmt",
            "account_scope",
            "assessment_id",
            unique=True,
        ),
    )


class MailSecurityFindingRow(Base):
    """One typed deterministic finding attached to an assessment (design §9).

    Table ``mail_security_findings`` columns
    ----------------------------------------
    - ``id`` (UUID) — record row id.
    - ``account_scope`` (text) — isolation boundary (R-023), indexed. Carried
      on the finding too so a finding read can be account-filtered without a
      join back to the assessment.
    - ``assessment_id`` (text) — the owning assessment's deterministic id.
    - ``finding_key`` (text) — opaque per-finding key; unique within
      ``(account_scope, assessment_id)``.
    - ``rule_id`` (text) — the deterministic rule that fired.
    - ``category`` (text) — typed finding category code.
    - ``severity`` (text) — severity code.
    - ``status`` (text) — finding status (``OBSERVED`` / ``SUSPECTED`` / …).
    - ``confidence_semantics`` (text, nullable) — what the confidence value
      means (raw estimate vs calibrated); an *uncalibrated* estimate is never
      presented as a verified probability.
    - ``confidence_value`` (JSONB) — the (nullable) numeric confidence, held in
      JSONB so ``None`` round-trips without a nullable-float column quirk.
    - ``evidence_ref`` (text, nullable) — single opaque evidence pointer.
    - ``explanation_code`` (text) — a safe upper-snake explanation code, never
      a raw body fragment (design §9, I-05).
    - ``meta`` (JSONB) — bounded safe structured metadata (e.g. extra opaque
      evidence refs); never raw content.

    Uniqueness by ``(account_scope, assessment_id, finding_key)``.
    """

    __tablename__ = "mail_security_findings"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    assessment_id: Mapped[str] = mapped_column(String(160), nullable=False)
    finding_key: Mapped[str] = mapped_column(String(200), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence_semantics: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    confidence_value: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )
    evidence_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    explanation_code: Mapped[str] = mapped_column(String(80), nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        Jsonb, default=dict, nullable=False
    )

    __table_args__ = (
        Index("ix_mail_security_findings_account_scope", "account_scope"),
        Index(
            "ix_mail_security_findings_scope_assessment",
            "account_scope",
            "assessment_id",
        ),
        Index(
            "ix_mail_security_findings_scope_asmt_key",
            "account_scope",
            "assessment_id",
            "finding_key",
            unique=True,
        ),
    )


class MailIntelIndicatorRow(Base):
    """A time-limited, versioned threat-intelligence indicator (Task 2.2).

    Table ``mail_intel_indicators``. Indicators are **global** threat intel
    (not per-mailbox), so there is deliberately no ``account_scope`` column;
    account isolation applies to user overrides (:class:`MailIntelOverrideRow`).
    Only a canonical value (domain / IP literal) or a SHA-256 hash (URL / file)
    is stored. Nothing is derived from user mail and there is no free-text
    column (R-006, R-015.1, T-19).

    Rows are never deleted: expiry, disable, supersession and rollback only
    change *liveness* (R-006.3 — stale intel is invalidated without erasing
    historical audit evidence).

    - ``promotion_version`` — the store version in which the row was promoted.
    - ``superseded_in_version`` — set when a later promotion replaced the row
      for the same ``(indicator_type, canonical_indicator, source)`` key;
      cleared again if that later version is rolled back.
    - ``rolled_back`` — set when the row's promotion was rolled back.
    """

    __tablename__ = "mail_intel_indicators"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")
    indicator_type: Mapped[str] = mapped_column(String(24), nullable=False)
    canonical_indicator: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence_type: Mapped[str] = mapped_column(String(24), nullable=False)
    license: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), default="GLOBAL", nullable=False)
    feed_version: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    promotion_version: Mapped[int] = mapped_column(Integer, nullable=False)
    superseded_in_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    rolled_back: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_mail_intel_indicators_lookup",
            "indicator_type",
            "canonical_indicator",
        ),
        Index("ix_mail_intel_indicators_version", "promotion_version"),
        Index("ix_mail_intel_indicators_expires_at", "expires_at"),
    )


class MailIntelOverrideRow(Base):
    """An account-scoped, expiring user override of an intel match (Task 2.2).

    Table ``mail_intel_overrides``. Scoped by **account + sender + indicator +
    expiry** and requires an opaque ``approval_ref`` (R-006.4, T-30, R-023).
    ``sender_key`` is an opaque hash, never an email address. The override only
    suppresses the match for that exact tuple; it is never a global allowlist.
    """

    __tablename__ = "mail_intel_overrides"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")
    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    sender_key: Mapped[str] = mapped_column(String(200), nullable=False)
    indicator_type: Mapped[str] = mapped_column(String(24), nullable=False)
    canonical_indicator: Mapped[str] = mapped_column(String(255), nullable=False)
    approval_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_mail_intel_overrides_account_scope", "account_scope"),
        Index(
            "ix_mail_intel_overrides_scope_lookup",
            "account_scope",
            "sender_key",
            "indicator_type",
            "canonical_indicator",
        ),
    )


class MailSecurityFeedbackRow(Base):
    """One append-only reviewer feedback record (Task 7.2, design §9).

    Table ``mail_security_feedback``. Codes and opaque refs only: the verdict,
    the mapped ledger kind, the human source, an opaque ``reviewer_ref`` (hash,
    never a user id or address), the disposition the reviewer saw, and the
    routed proposals (destination/scope/status). Proposals are recorded, never
    applied. ``account_scope`` is copied from the assessment row, never from
    the client. Unique by ``(account_scope, feedback_id)`` so a replay is
    idempotent.
    """

    __tablename__ = "mail_security_feedback"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    schema_version: Mapped[str] = mapped_column(String(32), default="mail-feedback@1")
    feedback_id: Mapped[str] = mapped_column(String(80), nullable=False)
    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    assessment_id: Mapped[str] = mapped_column(String(160), nullable=False)
    verdict: Mapped[str] = mapped_column(String(24), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_ref: Mapped[str] = mapped_column(String(80), nullable=False)
    disposition_seen: Mapped[str] = mapped_column(String(32), nullable=False)
    proposals: Mapped[list[dict[str, Any]]] = mapped_column(
        Jsonb, default=list, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_mail_security_feedback_scope_id",
            "account_scope",
            "feedback_id",
            unique=True,
        ),
        Index(
            "ix_mail_security_feedback_scope_assessment",
            "account_scope",
            "assessment_id",
        ),
    )


class MailSecurityHeldPromotionRow(Base):
    """One legacy actionable write held by the pre-promotion gate (Task 7.4).

    Table ``mail_security_held_promotions``. ``target_kind`` + ``target_id``
    are the exact deterministic id the ungated pipeline would have written
    (``attention`` / ``at-<..>``, ``commitment`` / ``ct-<..>``), unique so a
    replayed ingest never stacks a second hold. ``payload`` is the templated
    column set the legacy writer would have stored (same sensitivity as the
    target table; no body text). ``status`` is HELD / RELEASED /
    REVIEW_CANDIDATE; the deciding disposition and assessment id are kept for
    audit.
    """

    __tablename__ = "mail_security_held_promotions"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(32), default="mail-promotion-gate@1")
    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    content_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    communication_id: Mapped[str] = mapped_column(String(200), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    target_id: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(Jsonb, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="HELD")
    decided_disposition: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_assessment_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_mail_security_held_target",
            "target_kind",
            "target_id",
            unique=True,
        ),
        Index("ix_mail_security_held_scope_status", "account_scope", "status"),
        Index("ix_mail_security_held_content_ref", "content_ref"),
    )


class MailSecurityConfirmationRow(Base):
    """One explicit confirmation of an email-origin proposal action (Task 7.6).

    Table ``mail_security_confirmations``. Holds the validated proposal
    payload (enums, dates, bounded amounts, opaque refs only), the exact
    execution hash the approval binds to, the action and risk class, status,
    the issued approval record, the single-use step-up proof id (unique), the
    out-of-band sender attestation, a coded reject reason and a codes-only
    audit trail.
    """

    __tablename__ = "mail_security_confirmations"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    schema_version: Mapped[str] = mapped_column(String(32), default="mail-confirmation@1")
    confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal: Mapped[dict[str, Any]] = mapped_column(Jsonb, default=dict, nullable=False)
    execution_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    risk: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(80), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    approval: Mapped[dict[str, Any] | None] = mapped_column(Jsonb, nullable=True)
    step_up_proof_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    sender_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audit: Mapped[list[dict[str, Any]]] = mapped_column(Jsonb, default=list, nullable=False)

    __table_args__ = (
        Index("ix_mail_security_conf_id", "confirmation_id", unique=True),
        Index("ix_mail_security_conf_scope", "account_scope", "status"),
        Index("ix_mail_security_conf_step_up", "step_up_proof_id", unique=True),
    )


class MailThreadLedgerRow(Base):
    """One message in an account's conversation ledger (NS-ADR-067).

    Table ``mail_thread_ledger``. Holds only per-account HMAC fingerprints
    (``message_fp``, ``thread_fp``) and registrable domains: never a subject,
    body, local part or address. Outgoing rows (the owner's sent mail) carry
    their recipients' domains as ``participant_domains``. Inbound rows carry
    the SHADOW continuity ``status`` and signal codes plus the opaque body
    ``content_ref`` the review queue joins on.
    """

    __tablename__ = "mail_thread_ledger"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    schema_version: Mapped[str] = mapped_column(String(32), default="mail-thread-ledger@1")
    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    message_fp: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_fp: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    sender_domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    reply_to_domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    participant_domains: Mapped[list[str]] = mapped_column(Jsonb, default=list, nullable=False)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    signals: Mapped[list[str]] = mapped_column(Jsonb, default=list, nullable=False)
    content_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        Index("ix_mail_thread_ledger_msg", "account_scope", "message_fp", unique=True),
        Index("ix_mail_thread_ledger_thread", "account_scope", "thread_fp"),
        Index("ix_mail_thread_ledger_status", "account_scope", "status"),
    )


class MailCampaignMemberRow(Base):
    """One message's per-account content-campaign membership (NS-ADR-069).

    Table ``mail_campaign_members``. ``signature`` is the account-keyed MinHash
    (64 integers derived from content; never the content itself), kept until
    ``expires_at`` (30 days). ``content_ref`` is the opaque body ref.
    """

    __tablename__ = "mail_campaign_members"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), default="mail-campaigns@1")
    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    content_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    campaign_id: Mapped[str] = mapped_column(String(32), nullable=False)
    signature: Mapped[list[int]] = mapped_column(Jsonb, default=list, nullable=False)

    __table_args__ = (
        Index("ix_mail_campaign_member_src", "account_scope", "source_key", unique=True),
        Index("ix_mail_campaign_member_campaign", "account_scope", "campaign_id"),
        Index("ix_mail_campaign_member_expiry", "expires_at"),
    )


class MailCampaignBandRow(Base):
    """One LSH band key of a campaign member (NS-ADR-069). HMAC only."""

    __tablename__ = "mail_campaign_bands"

    id: Mapped[UUIDv4]
    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    band_key: Mapped[str] = mapped_column(String(32), nullable=False)
    member_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_mail_campaign_band_key", "account_scope", "band_key"),
        Index("ix_mail_campaign_band_member", "member_id"),
        Index("ix_mail_campaign_band_expiry", "expires_at"),
    )


class MailSemanticObservationRow(Base):
    """One SHADOW semantic-observer run for a message revision (NS-ADR-073).

    Table ``mail_semantic_observations``. ``observation`` holds enum codes
    only (category, claimed role, identity consistency, request types and
    urgencies, risk indicator codes). The model's free-text ``gist`` is never
    stored. ``status`` is ``COMPLETED`` or a typed observer/route error code.
    """

    __tablename__ = "mail_semantic_observations"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = _created_at()
    schema_version: Mapped[str] = mapped_column(String(32), default="mail-observation@1")
    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    route_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observation: Mapped[dict[str, Any]] = mapped_column(Jsonb, default=dict, nullable=False)

    __table_args__ = (
        Index(
            "ix_mail_semantic_obs_src", "account_scope", "source_key", "revision", unique=True
        ),
        Index("ix_mail_semantic_obs_status", "account_scope", "status"),
    )


class MailExpectednessCountRow(Base):
    """Per-account, per-keyed-sender request-type count (NS-ADR-075).

    Table ``mail_expectedness_counts``. ``sender_key`` is an account-salted
    HMAC of the registrable sender domain; nothing else about the sender,
    and nothing about any message, is stored.
    """

    __tablename__ = "mail_expectedness_counts"

    id: Mapped[UUIDv4]
    updated_at: Mapped[datetime] = _updated_at()
    account_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    sender_key: Mapped[str] = mapped_column(String(32), nullable=False)
    request_type: Mapped[str] = mapped_column(String(40), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index(
            "ix_mail_expect_key", "account_scope", "sender_key", "request_type", unique=True
        ),
    )


__all__ = [
    "MailCampaignBandRow",
    "MailExpectednessCountRow",
    "MailSemanticObservationRow",
    "MailCampaignMemberRow",
    "MailEvidenceRow",
    "MailIntelIndicatorRow",
    "MailIntelOverrideRow",
    "MailSecurityConfirmationRow",
    "MailThreadLedgerRow",
    "MailSecurityFeedbackRow",
    "MailSecurityHeldPromotionRow",
    "MailSecurityAssessmentRow",
    "MailSecurityFindingRow",
    "MailSecurityJobRow",
]
