"""Auth infrastructure ORM models (S101 / spec sso-integration AC-S03..AC-S05).

These tables back the signed-JWT auth surface in :mod:`backend.api.auth`:

- :class:`AuthSession` — one row per issued NerveCenter token (access /
  refresh / device).  Persisting sessions means an API restart never
  invalidates a still-valid JWT, and ``logout`` can revoke a session.
- :class:`AuthRefreshSession` — one row per opaque refresh token (stored only
  as a SHA-256 hash); rows of one login share a ``family_id`` (NS-ADR-123).
- :class:`PersonalApiToken` — a long-lived ``nct_`` API token owned by a
  person, stored only as a SHA-256 hash (NS-ADR-124).
- :class:`AuthDevice` — a paired device (prompot.md §71).  The stable
  ``device_id`` is chosen by the device; pairing mints a device credential
  JWT bound to it.
- :class:`PairingChallenge` — a short-lived (≤5 min) pairing challenge
  issued to an authenticated session and confirmed by a new device.
- :class:`ApprovalRecord` — a device-bound approval (prompot.md §72) bound
  to ``device_id`` + ``nonce`` + ``expires_at`` and to an exact
  ``planned_change_id``.  ``change_fingerprint`` is the SHA-256 of the
  bound change's content at approval time; any modification of the bound
  change invalidates the approval.

Naming follows the project convention: plural snake_case tables,
``ix_<table>_<column>`` indexes, ``<table>_<kind>_<columns>`` constraints.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Jsonb


def _uuid4_str() -> str:
    return str(uuid.uuid4())


class AuthSession(Base):
    """A persistent session for one issued NerveCenter-issued token.

    The primary key doubles as the JWT ``jti`` claim.  A session row is
    written at token issuance (login, pairing, refresh) and checked at
    validation time: a missing or revoked row means the token must be
    rejected even though its signature is valid.  Because the row lives in
    PostgreSQL, restarting the API does not invalidate a valid JWT.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        default=_uuid4_str,
        index=True,
    )
    #: JWT ``sub`` claim (username for humans, ``device:<device_id>`` for devices).
    sub: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    #: JWT ``typ`` claim: ``access`` | ``refresh`` | ``device``.
    typ: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    #: Where the session came from: ``local_admin`` | ``sso`` | ``pairing``.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="sso")
    #: Device binding (present for device credentials).
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_auth_sessions_sub_expires_at", "sub", "expires_at"),
        Index("ix_auth_sessions_device_id", "device_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuthSession id={self.id!r} sub={self.sub!r} typ={self.typ!r} "
            f"revoked={self.revoked!r}>"
        )


class AuthRefreshSession(Base):
    """One opaque refresh token of a NerveCenter login (NS-ADR-123).

    Only the SHA-256 ``token_hash`` is stored.  Every rotation inserts a new
    row in the same ``family_id`` and marks the old row ``rotated_at``; a
    rotated token presented after the grace window revokes the whole family.
    """

    __tablename__ = "auth_refresh_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid4_str)
    family_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sub: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    #: Verified identity captured at login (``sub`` / ``username`` / ``email``).
    identity: Mapped[dict | None] = mapped_column(Jsonb, nullable=True)
    #: ``sso`` | ``local-admin``.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="sso")
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idle_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    absolute_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    replaced_by: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<AuthRefreshSession id={self.id!r} family_id={self.family_id!r} "
            f"revoked={self.revoked!r}>"
        )


class PersonalApiToken(Base):
    """A personal ``nct_`` API token (NS-ADR-124); only its hash is stored."""

    __tablename__ = "personal_api_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid4_str)
    owner_sub: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    #: First characters of the token, for display only.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<PersonalApiToken id={self.id!r} name={self.name!r}>"


class AuthDevice(Base):
    """A paired device (prompot.md §71: challenge → credential → Keychain).

    ``device_id`` is the stable identifier the device reports during
    pairing confirmation; it is unique and is embedded in the device
    credential JWT so the token can only be used by that device.
    """

    __tablename__ = "auth_devices"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=_uuid4_str, index=True
    )
    device_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="ios")
    #: ``active`` | ``revoked``.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: Optional device attestation payload (JSONB).
    attestation: Mapped[dict | None] = mapped_column(Jsonb, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Set when the device is revoked (break-glass / admin action).
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<AuthDevice device_id={self.device_id!r} status={self.status!r}>"


class PairingChallenge(Base):
    """A short-lived device pairing challenge (prompot.md §71).

    ``expires_at`` is bounded to ≤5 minutes from creation.  Confirming the
    challenge with the matching ``nonce`` mints the device credential and
    flips ``status`` to ``confirmed``; the challenge is single-use.
    """

    __tablename__ = "pairing_challenges"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=_uuid4_str, index=True
    )
    #: Opaque handle returned to the client (``challenge_id``).
    challenge_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    nonce: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    #: JWT ``sub`` of the authenticated user who requested the challenge.
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    #: ``pending`` | ``confirmed`` | ``expired`` | ``failed``.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_pairing_challenges_status_expires_at", "status", "expires_at"),)

    def __repr__(self) -> str:
        return (
            f"<PairingChallenge challenge_id={self.challenge_id!r} "
            f"status={self.status!r}>"
        )


class ApprovalRecord(Base):
    """A device-bound approval on an exact :class:`PlannedChange` (§72).

    The approval is bound to ``device_id`` + ``nonce`` + ``expires_at`` and
    to an exact ``planned_change_id``.  ``change_fingerprint`` is the
    SHA-256 digest of the bound change's content captured at approval time;
    any modification of the bound change invalidates the approval
    (verified by :func:`backend.api.auth.verify_approval`).
    """

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=_uuid4_str, index=True
    )
    planned_change_id: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
    )
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    #: JWT ``sub`` of the approver (the device's subject, ``device:<device_id>``).
    approver_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Approval nonce (bound to the approval; must match on verify/decide).
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: SHA-256 of the bound planned change's content at approval time.
    change_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``pending`` | ``approved`` | ``denied`` | ``invalidated``.
    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    invalidation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rationale: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_approvals_planned_change_id", "planned_change_id"),
        Index("ix_approvals_decision_expires_at", "decision", "expires_at"),
        Index("ix_approvals_device_id", "device_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<ApprovalRecord id={self.id!r} "
            f"planned_change_id={self.planned_change_id!r} "
            f"decision={self.decision!r}>"
        )
