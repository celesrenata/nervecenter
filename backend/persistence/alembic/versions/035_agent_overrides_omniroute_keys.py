"""Built-in agent overrides and the OmniRoute key audit (NS-ADR-116, NS-ADR-118).

Adds two new tables, ``agent_overrides`` (owner edits on top of a built-in agent
manifest) and ``omniroute_key_audit`` (who changed which OmniRoute key and the
outcome; never a key value). No existing table is altered.

Reversibility: ``downgrade()`` drops both tables; built-in overrides and the key
audit rows are lost. Stored keys are in ``secrets`` and are not touched.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "035_agent_overrides_keys"
down_revision = "034_agent_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_overrides",
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
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("prompt_addition", sa.String(), nullable=True),
        sa.Column("skills", postgresql.JSONB(), nullable=True),
        sa.Column("inference", postgresql.JSONB(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_overrides_agent_id", "agent_overrides", ["agent_id"], unique=True)
    op.create_table(
        "omniroute_key_audit",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("key_kind", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_omniroute_key_audit_at", "omniroute_key_audit", ["at"])


def downgrade() -> None:
    op.drop_index("ix_omniroute_key_audit_at", table_name="omniroute_key_audit")
    op.drop_table("omniroute_key_audit")
    op.drop_index("ix_agent_overrides_agent_id", table_name="agent_overrides")
    op.drop_table("agent_overrides")
