"""Personal plane durable tables (Personal-Ops Recovery spec §6 freeze).

Adds the fourteen tables backing the durable personal plane, the foundation
that the connector control plane and the mail-ingest supervisor are built on:

- ``people``             — persons the user actually cares about.
- ``person_identities``  — channel identities resolving to a person.
- ``communications``     — ref-only communication records (raw content stays
  in the encrypted content store; only ``content_ref`` is kept).
- ``conversations``      — rolling per-person, per-channel threads.
- ``commitments``        — tracked commitments owed to a person.
- ``followups``          — loose follow-ups, optionally person-linked.
- ``attention_items``    — prioritized items needing the user's attention.
- ``snoozes``            — snooze events against attention items.
- ``notification_log``   — one-notification-per-(item, level) dedup ledger.
- ``preferences``        — durable user preferences (unique key / JSONB value).
- ``connections``        — configured personal-plane connections (non-secret
  config only).
- ``secrets``            — encrypted secret payloads (ciphertext only).
- ``ingest_cursors``     — durable per-(connection, kind) ingest cursors.
- ``import_jobs``        — tracked bulk-import jobs.

Conventions match :mod:`001_initial` / :mod:`002_auth` / :mod:`005_nc_platform`:
identifier columns are ``sa.String()`` (string UUID ids — no database UUID
columns), JSONB columns use ``postgresql.JSONB(astext_type=sa.Text())`` so
the migration stays portable to the SQLite scratch databases used by the
test suite, timestamps use ``server_default=sa.text("now()")``, and indexes
follow the ``ix_<table>_<column>`` naming. Enum-like values are stored as
plain strings (no Postgres ENUM/CHECK types, which would break the SQLite
scratch DBs).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "006_personal_plane"
down_revision = "005_nc_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # people (no FK dependencies; precedes person_identities and friends).
    op.create_table(
        "people",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("vip", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("default_sensitivity", sa.String(length=32), nullable=True),
        sa.Column(
            "notes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "boundaries",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # person_identities.
    op.create_table(
        "person_identities",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("person_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("handle", sa.String(length=512), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_person_identities_person_id", "person_identities", ["person_id"], unique=False
    )

    # communications.
    op.create_table(
        "communications",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("person_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("content_ref", sa.String(length=512), nullable=False),
        sa.Column("sensitivity", sa.String(length=32), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "flags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_communications_person_id", "communications", ["person_id"], unique=False
    )
    op.create_index(
        "ix_communications_received_at",
        "communications",
        ["received_at"],
        unique=False,
    )

    # conversations.
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("person_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversations_person_id", "conversations", ["person_id"], unique=False
    )

    # commitments.
    op.create_table(
        "commitments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("person_id", sa.String(), nullable=False),
        sa.Column("text", sa.String(length=2048), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("source_communication_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_communication_id"],
            ["communications.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_commitments_person_id", "commitments", ["person_id"], unique=False
    )
    op.create_index(
        "ix_commitments_state", "commitments", ["state"], unique=False
    )

    # followups.
    op.create_table(
        "followups",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("person_id", sa.String(), nullable=True),
        sa.Column("text", sa.String(length=2048), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column(
            "linked_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_followups_person_id", "followups", ["person_id"], unique=False
    )
    op.create_index(
        "ix_followups_state", "followups", ["state"], unique=False
    )

    # attention_items.
    op.create_table(
        "attention_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("body", sa.String(length=4096), nullable=True),
        sa.Column("importance", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("urgency", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "source_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("recommended_action", sa.String(length=1024), nullable=True),
        sa.Column(
            "escalation_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_condition", sa.String(length=1024), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_attention_items_state", "attention_items", ["state"], unique=False
    )
    op.create_index(
        "ix_attention_items_deadline_at",
        "attention_items",
        ["deadline_at"],
        unique=False,
    )

    # snoozes.
    op.create_table(
        "snoozes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("attention_item_id", sa.String(), nullable=False),
        sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=1024), nullable=True),
        sa.ForeignKeyConstraint(
            ["attention_item_id"], ["attention_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_snoozes_attention_item_id",
        "snoozes",
        ["attention_item_id"],
        unique=False,
    )

    # notification_log.
    op.create_table(
        "notification_log",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("attention_item_id", sa.String(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["attention_item_id"], ["attention_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_log_item_level",
        "notification_log",
        ["attention_item_id", "level"],
        unique=True,
    )
    op.create_index(
        "ix_notification_log_attention_item_id",
        "notification_log",
        ["attention_item_id"],
        unique=False,
    )

    # preferences.
    op.create_table(
        "preferences",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("key", sa.String(length=256), nullable=False),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_preferences_key", "preferences", ["key"], unique=True
    )

    # connections.
    op.create_table(
        "connections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=256), nullable=True),
        sa.Column(
            "auth_status", sa.String(length=32), nullable=False, server_default="unconfigured"
        ),
        sa.Column(
            "operational_status",
            sa.String(length=32),
            nullable=False,
            server_default="never_synced",
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_message", sa.String(length=2048), nullable=True),
        sa.Column(
            "sync_cursor",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "import_progress",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_connections_enabled", "connections", ["enabled"], unique=False
    )
    op.create_index("ix_connections_kind", "connections", ["kind"], unique=False)

    # secrets.
    op.create_table(
        "secrets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_secrets_owner", "secrets", ["owner_kind", "owner_id"], unique=False
    )
    op.create_index("ix_secrets_kind", "secrets", ["kind"], unique=False)

    # ingest_cursors.
    op.create_table(
        "ingest_cursors",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "cursor_value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ingest_cursors_connection_kind",
        "ingest_cursors",
        ["connection_id", "kind"],
        unique=True,
    )
    op.create_index(
        "ix_ingest_cursors_connection_id",
        "ingest_cursors",
        ["connection_id"],
        unique=False,
    )

    # import_jobs.
    op.create_table(
        "import_jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column(
            "progress",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_import_jobs_connection_id", "import_jobs", ["connection_id"], unique=False
    )
    op.create_index(
        "ix_import_jobs_state", "import_jobs", ["state"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_import_jobs_state", table_name="import_jobs")
    op.drop_index("ix_import_jobs_connection_id", table_name="import_jobs")
    op.drop_table("import_jobs")

    op.drop_index(
        "ix_ingest_cursors_connection_id", table_name="ingest_cursors"
    )
    op.drop_index(
        "ix_ingest_cursors_connection_kind", table_name="ingest_cursors"
    )
    op.drop_table("ingest_cursors")

    op.drop_index("ix_secrets_kind", table_name="secrets")
    op.drop_index("ix_secrets_owner", table_name="secrets")
    op.drop_table("secrets")

    op.drop_index("ix_connections_kind", table_name="connections")
    op.drop_index("ix_connections_enabled", table_name="connections")
    op.drop_table("connections")

    op.drop_index("ix_preferences_key", table_name="preferences")
    op.drop_table("preferences")

    op.drop_index(
        "ix_notification_log_attention_item_id", table_name="notification_log"
    )
    op.drop_index("ix_notification_log_item_level", table_name="notification_log")
    op.drop_table("notification_log")

    op.drop_index("ix_snoozes_attention_item_id", table_name="snoozes")
    op.drop_table("snoozes")

    op.drop_index(
        "ix_attention_items_deadline_at", table_name="attention_items"
    )
    op.drop_index("ix_attention_items_state", table_name="attention_items")
    op.drop_table("attention_items")

    op.drop_index("ix_followups_state", table_name="followups")
    op.drop_index("ix_followups_person_id", table_name="followups")
    op.drop_table("followups")

    op.drop_index("ix_commitments_state", table_name="commitments")
    op.drop_index("ix_commitments_person_id", table_name="commitments")
    op.drop_table("commitments")

    op.drop_index("ix_conversations_person_id", table_name="conversations")
    op.drop_table("conversations")

    op.drop_index("ix_communications_received_at", table_name="communications")
    op.drop_index("ix_communications_person_id", table_name="communications")
    op.drop_table("communications")

    op.drop_index("ix_person_identities_person_id", table_name="person_identities")
    op.drop_table("person_identities")

    op.drop_table("people")
