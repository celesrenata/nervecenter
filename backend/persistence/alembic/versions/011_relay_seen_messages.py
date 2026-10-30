"""Relay durable-seen table (Personal-Ops Recovery §6 — workstream H).

Creates the single table backing the Apple Messages relay's **durable**
dedup:

- ``relay_seen_messages`` — one row per relay message that was durably
  ingested: ``connection_id`` (FK ``connections.id``) / ``device_id`` /
  ``message_id`` / the nullable durable ``content_ref`` / ``ingested_at``.
  The ``(connection_id, device_id, message_id)`` triple is unique, so a
  re-post of the same message is a no-op *across restarts* — the previous
  in-memory seen-set (bounded ``relay_seq`` cursor) was lost whenever the
  API restarted, which let a relay re-send its queue and re-import the same
  messages. The cursor stays as a secondary hint; the table is the
  authoritative gate.

Numbering note: the linear alembic chain head in this tree is
``009_routines`` (007_calendar → 008_bills → 010_wellness → 009_routines);
this revision numbers ``011`` directly on top of it, so the revision id is
stable regardless of any future chain rewire.

Conventions match :mod:`010_wellness`: identifier columns are
``sa.String()`` (string UUID ids — no database UUID columns), timestamps
use ``sa.DateTime(timezone=True)`` with a ``now()`` server default, and
index names follow the ``ix_<table>_<column>`` convention.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "011_relay_seen_messages"
down_revision = "009_routines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "relay_seen_messages",
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
        sa.Column(
            "connection_id",
            sa.String(),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.String(length=256), nullable=False),
        sa.Column("content_ref", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_relay_seen_messages_device_msg",
        "relay_seen_messages",
        ["connection_id", "device_id", "message_id"],
        unique=True,
    )
    op.create_index(
        "ix_relay_seen_messages_connection_id",
        "relay_seen_messages",
        ["connection_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_relay_seen_messages_connection_id", "relay_seen_messages")
    op.drop_index("ix_relay_seen_messages_device_msg", "relay_seen_messages")
    op.drop_table("relay_seen_messages")
