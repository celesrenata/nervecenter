"""Wellness Journal durable table (Personal-Ops Recovery §10 — workstream I).

Creates the single table backing the private wellness plane:

- ``wellness_entries`` — one user check-in per day (the *date* of ``at``
  is the upsert key — re-saving the same day updates in place): tz-aware
  ``at`` / nullable ``mood`` (1..5) / ``sleep_minutes`` / JSONB
  ``sleep_consistency`` / nullable ``energy`` / ``stress`` (1..10) /
  ``journal_entry`` / JSONB ``observations`` (custom key-value).

Numbering note: the §6 freeze text lists wellness as migration ``009`` in a
007–010 sequence (bills / routines / wellness / calendar) that has not fully
landed in this tree — the linear alembic chain head in *this worktree* is
``007_calendar``. This revision therefore numbers ``010_wellness`` directly
on top of ``007_calendar`` as dispatched; the integrator rewires the chain
afterward (the revision id is stable regardless of the chain position).

Conventions match :mod:`006_personal_plane` / :mod:`007_calendar`:
identifier columns are ``sa.String()`` (string UUID ids — no database UUID
columns), JSONB columns use ``postgresql.JSONB(astext_type=sa.Text())`` so
the migration stays portable to the SQLite scratch databases used by the
test suite, timestamps use ``sa.DateTime(timezone=True)`` with a ``now()``
server default, and index names follow the ``ix_<table>_<column>``
convention.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "010_wellness"
down_revision = "008_bills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wellness_entries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mood", sa.Integer(), nullable=True),
        sa.Column("sleep_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "sleep_consistency",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("energy", sa.Integer(), nullable=True),
        sa.Column("stress", sa.Integer(), nullable=True),
        sa.Column("journal_entry", sa.Text(), nullable=True),
        sa.Column(
            "observations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_wellness_entries_at",
        "wellness_entries",
        ["at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_wellness_entries_at", table_name="wellness_entries")
    op.drop_table("wellness_entries")
