"""OpenAI usage ledger and escalation grants (NS-ADR-107).

Adds two new tables; no existing table is altered.
Reversibility: ``downgrade()`` drops both; usage history and grants are lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "033_openai_usage"
down_revision = "032_coding_handoffs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "openai_calls",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("caller", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_after_s", sa.Float(), nullable=True),
        sa.Column("wait_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_openai_calls_id", "openai_calls", ["id"])
    op.create_index("ix_openai_calls_created", "openai_calls", ["created_at"])
    op.create_table(
        "escalation_grants",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_kind", "subject_id", name="uq_escalation_grants_subject"),
    )
    op.create_index("ix_escalation_grants_id", "escalation_grants", ["id"])


def downgrade() -> None:
    op.drop_index("ix_escalation_grants_id", table_name="escalation_grants")
    op.drop_table("escalation_grants")
    op.drop_index("ix_openai_calls_created", table_name="openai_calls")
    op.drop_index("ix_openai_calls_id", table_name="openai_calls")
    op.drop_table("openai_calls")
