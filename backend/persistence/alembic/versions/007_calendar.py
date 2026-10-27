"""Calendar plane durable tables (Personal-Ops Recovery §8 — workstream E).

Creates the two tables backing the durable calendar plane:

- ``calendar_accounts`` — a connected calendar account (Google / CalDAV /
  Apple relay): provider / label / non-secret ``account_ref`` / ``secret_ref``
  (the credential lives only in the connector secrets vault, workstream B) /
  the IANA ``timezone`` used to resolve floating times / non-secret JSONB
  ``config`` / durable JSONB ``sync_state`` (per-calendar sync tokens, etags) /
  ``enabled`` / the two-axis operational status (``operational_status`` +
  ``last_success_at`` / ``last_attempt_at`` / ``last_error_code`` /
  ``last_error_message``).
- ``calendar_events`` — a durable, read-only copy of a provider event keyed by
  ``(account_id, ical_uid)``: tz-aware ``start`` / ``end``, ``all_day``,
  ``title`` / ``location`` / ``description`` / the authored ``timezone`` /
  the provider change marker ``changed_at`` / ``appt`` / an explicit
  ``deleted`` marker (§8 — events are never silently dropped) / ``status`` /
  JSONB ``extra``.

Numbering note: the §6 freeze text lists the calendar tables as migration
``010`` in a 007–009 sequence (bills / routines / wellness) that has not
landed in this tree — the linear alembic chain head is
``006_personal_plane``. This revision therefore creates the tables as
``007_calendar`` directly on top of ``006_personal_plane``; the column set is
the frozen one.

Conventions match :mod:`006_personal_plane`: identifier columns are
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
revision = "007_calendar"
down_revision = "006_personal_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # calendar_accounts (no FK dependencies; precedes calendar_events).
    op.create_table(
        "calendar_accounts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("account_ref", sa.String(length=512), nullable=True),
        sa.Column("secret_ref", sa.String(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "sync_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "operational_status",
            sa.String(length=32),
            nullable=False,
            server_default="never_synced",
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_message", sa.String(length=512), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_calendar_accounts_provider",
        "calendar_accounts",
        ["provider"],
        unique=False,
    )
    op.create_index(
        "ix_calendar_accounts_enabled",
        "calendar_accounts",
        ["enabled"],
        unique=False,
    )

    # calendar_events.
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("ical_uid", sa.String(length=512), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("location", sa.String(length=512), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("changed_at", sa.String(length=128), nullable=True),
        sa.Column("appt", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="confirmed"
        ),
        sa.Column(
            "extra",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["calendar_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_calendar_events_account_id",
        "calendar_events",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_calendar_events_ical_uid",
        "calendar_events",
        ["ical_uid"],
        unique=False,
    )
    op.create_index(
        "ix_calendar_events_start",
        "calendar_events",
        ["start"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_calendar_events_start", table_name="calendar_events")
    op.drop_index("ix_calendar_events_ical_uid", table_name="calendar_events")
    op.drop_index("ix_calendar_events_account_id", table_name="calendar_events")
    op.drop_table("calendar_events")

    op.drop_index("ix_calendar_accounts_enabled", table_name="calendar_accounts")
    op.drop_index("ix_calendar_accounts_provider", table_name="calendar_accounts")
    op.drop_table("calendar_accounts")
