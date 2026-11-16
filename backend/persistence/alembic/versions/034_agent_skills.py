"""Agent skills: pinned Skill Hub ids and per-run skill records (NS-ADR-111).

Adds ``agents.skills`` (pinned catalog ids), ``agent_runs.skills`` (the skills
injected into a run) and ``agent_runs.skills_status``. All additive.
Reversibility: ``downgrade()`` drops the three columns; pinned ids and per-run
skill records are lost.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "034_agent_skills"
down_revision = "033_openai_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "skills",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "skills",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column("agent_runs", sa.Column("skills_status", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "skills_status")
    op.drop_column("agent_runs", "skills")
    op.drop_column("agents", "skills")
