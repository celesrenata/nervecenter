"""Shared project context store (NS-ADR-104).

Adds two new tables; no existing table is altered.
Reversibility: ``downgrade()`` drops both; project summaries and notes are lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "031_project_contexts"
down_revision = "030_research"
branch_labels = None
depends_on = None


def _ts(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def upgrade() -> None:
    op.create_table(
        "project_contexts",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_project_contexts_slug"),
    )
    op.create_index("ix_project_contexts_id", "project_contexts", ["id"])
    op.create_table(
        "project_context_notes",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("author", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["project_contexts.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_project_context_notes_id", "project_context_notes", ["id"])
    op.create_index(
        "ix_project_context_notes_project", "project_context_notes", ["project_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_project_context_notes_project", table_name="project_context_notes")
    op.drop_index("ix_project_context_notes_id", table_name="project_context_notes")
    op.drop_table("project_context_notes")
    op.drop_index("ix_project_contexts_id", table_name="project_contexts")
    op.drop_table("project_contexts")
