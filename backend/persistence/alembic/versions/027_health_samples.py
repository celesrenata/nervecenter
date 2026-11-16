"""Apple Health samples (NS-ADR-100).

Adds one new table, ``health_samples``. No existing table is altered.
Reversibility: ``downgrade()`` drops the table; only ingested samples are lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "027_health_samples"
down_revision = "026_agent_runner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "health_samples",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_health_samples_dedupe_key"),
    )
    op.create_index("ix_health_samples_id", "health_samples", ["id"])
    op.create_index("ix_health_samples_kind_start", "health_samples", ["kind", "start_at"])


def downgrade() -> None:
    op.drop_index("ix_health_samples_kind_start", table_name="health_samples")
    op.drop_index("ix_health_samples_id", table_name="health_samples")
    op.drop_table("health_samples")
