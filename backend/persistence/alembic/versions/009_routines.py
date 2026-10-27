"""Routines and sleep durable tables (Personal-Ops Recovery §9 — workstream H).

Creates the three tables backing the Routine Engine:

- ``routines`` — a user routine of one of the seven frozen kinds
  (wake / workday_prep / bedtime / morning_briefing / appointment_prep /
  medication / check_in): JSONB ``schedule`` (wall-clock ``times`` + ISO
  ``weekdays`` + IANA ``timezone``), ``enabled``, ``last_completed_at``, the
  server-computed deterministic **clock-based** ``deterministic_fallback_alarm``
  JSONB (wake-up workflows must not depend on Apple Health or device wake),
  and the one-time gentle ``missed_flags`` JSONB (missed-window dedup — never
  a streak counter).
- ``routine_check_ins`` — completion records for a routine: ``at`` /
  ``completed`` / optional ``note``.
- ``sleep_records`` — explicit sleep records (Apple Health relay or manual
  entry): ``bed_at`` / ``wake_at`` (nullable) / ``source`` / the on-write
  ``duration_minutes`` / the durable ``consent_granted`` marker. Missing data
  stays unknown — the engine reports ``regularity: unknown`` for thin windows
  and never asserts asleep/awake from an absence of data.

Numbering note: the §6 freeze text lists the routine tables as migration
``008`` in a 007 (bills) / 008 (routines) / 009 (wellness) sequence that has
not all landed in this tree. The linear alembic chain head in this worktree
is ``007_calendar``, so this revision creates the tables as ``009_routines``
directly on top of ``007_calendar``; the column set is the frozen one. The
parent integrator rewires the chain order afterward.

Conventions match :mod:`007_calendar`: identifier columns are
``sa.String()`` (string UUID ids — no database UUID columns), JSONB columns
use ``postgresql.JSONB(astext_type=sa.Text())`` so the migration stays
portable to the SQLite scratch databases used by the test suite, timestamps
use ``sa.DateTime(timezone=True)`` with a ``now()`` server default, and index
names follow the ``ix_<table>_<column>`` convention.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "009_routines"
down_revision = "010_wellness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # routines (no FK dependencies; precedes routine_check_ins).
    op.create_table(
        "routines",
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
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column(
            "schedule",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "deterministic_fallback_alarm",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "missed_flags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_routines_kind", "routines", ["kind"], unique=False)
    op.create_index("ix_routines_enabled", "routines", ["enabled"], unique=False)

    op.create_table(
        "routine_check_ins",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
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
        sa.Column("routine_id", sa.String(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "completed", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["routine_id"], ["routines.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_routine_check_ins_routine_id",
        "routine_check_ins",
        ["routine_id"],
        unique=False,
    )
    op.create_index(
        "ix_routine_check_ins_at", "routine_check_ins", ["at"], unique=False
    )

    op.create_table(
        "sleep_records",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
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
        sa.Column("bed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "consent_granted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sleep_records_bed_at", "sleep_records", ["bed_at"], unique=False
    )
    op.create_index(
        "ix_sleep_records_source", "sleep_records", ["source"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_sleep_records_source", table_name="sleep_records")
    op.drop_index("ix_sleep_records_bed_at", table_name="sleep_records")
    op.drop_table("sleep_records")
    op.drop_index("ix_routine_check_ins_at", table_name="routine_check_ins")
    op.drop_index(
        "ix_routine_check_ins_routine_id", table_name="routine_check_ins"
    )
    op.drop_table("routine_check_ins")
    op.drop_index("ix_routines_enabled", table_name="routines")
    op.drop_index("ix_routines_kind", table_name="routines")
    op.drop_table("routines")
