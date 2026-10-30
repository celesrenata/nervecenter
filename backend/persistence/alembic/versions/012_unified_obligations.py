"""Unified obligation plane (P2-A — Personal Reliability).

Creates the single table backing the durable obligation plane:

- ``obligations`` — one actionable item the user is responsible for.
  Carries stable identity (``id`` + ``source_account`` + ``owner_id`` +
  unique ``dedup_key``), provenance (JSONB ``evidence`` —
  ``[{source_kind, source_id, snippet, confidence}]``, same shape as bills),
  a due date or structured trigger condition, confidence + per-field
  ``uncertainty``, a ``status`` lifecycle, a ``next_action`` +
  ``suggested_action``, ``priority`` + ``consequence``,
  ``last_verified_at`` (freshness), snooze/dismiss/complete timestamps,
  ``relationships`` (``[{kind, target_id}]``), and ``source_record_ref``
  (``{plane, id}`` — the obligation *references* the concrete plane record,
  it does not replace bills/calendar/routines/wellness).

Contract: ``docs/contracts/obligation.md`` (frozen).

Hard rules at the shape level:

- ``dedup_key`` is UNIQUE — two records with the same key are the same
  obligation; the repository upserts (merges evidence) instead of creating
  a second row.
- ``origin`` is ``explicit`` or ``inferred``; inferred obligations never
  auto-execute (enforced by the service, not the DB).
- Terminal states ``done`` / ``dismissed`` are written only by the
  repository; no code path flips them implicitly.
- No Postgres ``ENUM``/``CHECK`` types (breaks the SQLite scratch test DBs);
  enum-like values are plain :class:`sa.String`.

Conventions match :mod:`008_bills` / :mod:`009_routines`: identifier columns
are ``sa.String()`` (string UUID ids — no database UUID columns), JSONB uses
``postgresql.JSONB(astext_type=sa.Text())`` so the migration stays portable
to the SQLite scratch databases, timestamps use ``sa.DateTime(timezone=True)``
with a ``now()`` server default, and index names follow
``ix_<table>_<column>``.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "012_unified_obligations"
down_revision = "011_relay_seen_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "obligations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "schema_version",
            sa.String(length=16),
            nullable=False,
            server_default="v0",
        ),
        sa.Column("obligation_type", sa.String(length=32), nullable=False),
        sa.Column(
            "origin",
            sa.String(length=16),
            nullable=False,
            server_default="inferred",
        ),
        sa.Column(
            "source_account",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "owner_id",
            sa.String(length=64),
            nullable=False,
            server_default="default",
        ),
        sa.Column("dedup_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "trigger",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "confidence", sa.Float(), nullable=False, server_default="1.0"
        ),
        sa.Column(
            "uncertainty",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column("next_action", sa.String(), nullable=True),
        sa.Column(
            "suggested_action",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "priority", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("consequence", sa.String(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "relationships",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "source_record_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_obligations_dedup_key", "obligations", ["dedup_key"], unique=True
    )
    op.create_index("ix_obligations_status", "obligations", ["status"])
    op.create_index("ix_obligations_due_at", "obligations", ["due_at"])


def downgrade() -> None:
    op.drop_index("ix_obligations_due_at", table_name="obligations")
    op.drop_index("ix_obligations_status", table_name="obligations")
    op.drop_index("ix_obligations_dedup_key", table_name="obligations")
    op.drop_table("obligations")
