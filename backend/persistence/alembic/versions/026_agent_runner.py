"""Agent runner: schedules and run history (NS-ADR-099).

Adds two new tables, ``agent_schedules`` (one cadence row per agent) and
``agent_runs`` (append-only run history). No existing table is altered.

Reversibility: ``downgrade()`` drops both tables; only schedules and run
history are lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "026_agent_runner"
down_revision = "025_mail_expect_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_schedules",
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
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="on_demand"),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("daily_time", sa.String(length=5), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="background"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_schedules_id", "agent_schedules", ["id"])
    op.create_index("ix_agent_schedules_agent_id", "agent_schedules", ["agent_id"], unique=True)
    op.create_index("ix_agent_schedules_next_run_at", "agent_schedules", ["next_run_at"])
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False, server_default="schedule"),
        sa.Column("output", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("latency_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_id", "agent_runs", ["id"])
    op.create_index("ix_agent_runs_agent_id", "agent_runs", ["agent_id"])
    op.create_index("ix_agent_runs_agent_started", "agent_runs", ["agent_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_agent_started", table_name="agent_runs")
    op.drop_index("ix_agent_runs_agent_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_agent_schedules_next_run_at", table_name="agent_schedules")
    op.drop_index("ix_agent_schedules_agent_id", table_name="agent_schedules")
    op.drop_index("ix_agent_schedules_id", table_name="agent_schedules")
    op.drop_table("agent_schedules")
