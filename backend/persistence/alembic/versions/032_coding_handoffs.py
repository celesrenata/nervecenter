"""Coding handoffs to central opencode or the owner's Mac (NS-ADR-106).

Adds one new table; no existing table is altered.
Reversibility: ``downgrade()`` drops it; the handoff queue and results are lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "032_coding_handoffs"
down_revision = "031_project_contexts"
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        "coding_handoffs",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("runner", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("project_slug", sa.String(length=64), nullable=True),
        sa.Column("repo", sa.String(length=200), nullable=True),
        sa.Column("requested_by", sa.String(length=64), nullable=False, server_default="owner"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("push_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        _ts("claimed_at", nullable=True),
        _ts("finished_at", nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("session_ref", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_coding_handoffs_id", "coding_handoffs", ["id"])
    op.create_index(
        "ix_coding_handoffs_queue", "coding_handoffs", ["runner", "status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_coding_handoffs_queue", table_name="coding_handoffs")
    op.drop_index("ix_coding_handoffs_id", table_name="coding_handoffs")
    op.drop_table("coding_handoffs")
