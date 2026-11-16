"""Long-term researcher: projects, plan steps, checkpoints (NS-ADR-103).

Adds three new tables; no existing table is altered.

Reversibility: ``downgrade()`` drops the three tables; research projects,
their results and checkpoints are lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "030_research"
down_revision = "029_finance"
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
        "research_projects",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="planned"),
        sa.Column("budget_hours", sa.Float(), nullable=False, server_default="4"),
        sa.Column("cadence_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("combo", sa.String(length=64), nullable=False, server_default="secure/planner"),
        sa.Column("spent_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        _ts("next_step_at", nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("report", sa.Text(), nullable=True),
        _ts("finished_at", nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_research_projects_id", "research_projects", ["id"])
    op.create_index("ix_research_projects_status", "research_projects", ["status"])
    op.create_table(
        "research_steps",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        _ts("finished_at", nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["research_projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "ordinal", name="uq_research_steps_order"),
    )
    op.create_index("ix_research_steps_id", "research_steps", ["id"])
    op.create_index("ix_research_steps_project_id", "research_steps", ["project_id"])
    op.create_table(
        "research_checkpoints",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("step_cursor", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["research_projects.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_research_checkpoints_id", "research_checkpoints", ["id"])
    op.create_index(
        "ix_research_checkpoints_project", "research_checkpoints", ["project_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_research_checkpoints_project", table_name="research_checkpoints")
    op.drop_index("ix_research_checkpoints_id", table_name="research_checkpoints")
    op.drop_table("research_checkpoints")
    op.drop_index("ix_research_steps_project_id", table_name="research_steps")
    op.drop_index("ix_research_steps_id", table_name="research_steps")
    op.drop_table("research_steps")
    op.drop_index("ix_research_projects_status", table_name="research_projects")
    op.drop_index("ix_research_projects_id", table_name="research_projects")
    op.drop_table("research_projects")
