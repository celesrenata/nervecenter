"""Inference provider + combo tables (providers feature).

Adds the durable backing stores the web flagged as missing:

- ``inference_providers`` — an editable, durable inference provider. The
  API key is stored in-band (``api_key``) so the live test-connection and
  model-recall endpoints authenticate against the real provider; it is
  *masked* on every read and only ever returned to an authenticated
  principal. ``models`` holds the model ids recalled from the live provider
  (or hand-entered).
- ``inference_combos`` — a named group binding one provider to one model.
  This is the unit agents bind to (``agents.combo_id``); re-pointing a combo
  re-binds every agent that references it, so a single edit cascades.
- ``agents.combo_id`` — nullable FK to ``inference_combos.id`` (a plain
  ``sa.String`` to stay portable to the SQLite scratch DBs; no hard FK
  constraint so a combo can be deleted without cascading deletes the ORM
  service owns).

Conventions match :mod:`004_incidents_agents` / :mod:`014_agent_inference`:
identifier columns are ``sa.String()``, JSONB uses
``postgresql.JSONB(astext_type=sa.Text())`` so the migration stays portable
to the SQLite scratch databases used by the test suite, indexes follow
``ix_<table>_<column>``.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "015_inference_providers"
down_revision = "014_agent_inference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inference_providers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(), nullable=True),
        sa.Column("api_key", sa.String(), nullable=True),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "models",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inference_providers_name", "inference_providers", ["name"], unique=True)

    op.create_table(
        "inference_combos",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("provider_id", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inference_combos_name", "inference_combos", ["name"], unique=True)
    op.create_index("ix_inference_combos_provider", "inference_combos", ["provider_id"], unique=False)

    op.add_column(
        "agents",
        sa.Column("combo_id", sa.String(), nullable=True),
    )
    op.create_index("ix_agents_combo", "agents", ["combo_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_agents_combo", table_name="agents")
    op.drop_column("agents", "combo_id")
    op.drop_index("ix_inference_combos_provider", table_name="inference_combos")
    op.drop_index("ix_inference_combos_name", table_name="inference_combos")
    op.drop_table("inference_combos")
    op.drop_index("ix_inference_providers_name", table_name="inference_providers")
    op.drop_table("inference_providers")
