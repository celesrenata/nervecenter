"""Kube agent: per-step plan approval, guard columns and kube tables (NS-ADR-120).

Adds ``infra_plans.selected_steps`` (JSONB, null = every step), ``guard`` (null or ``kube``) and
``issue_key``, plus ``kube_settings``, ``kube_issues``, ``kube_overrides`` and ``kube_reports``.
No existing column is altered. Reversibility: ``downgrade()`` drops them.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "045_kube_agent"
down_revision = "044_pentest_tcp_ports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("infra_plans", sa.Column("selected_steps", postgresql.JSONB(), nullable=True))
    op.add_column("infra_plans", sa.Column("guard", sa.String(length=16), nullable=True))
    op.add_column("infra_plans", sa.Column("issue_key", sa.String(length=300), nullable=True))


    op.create_table(
        "kube_settings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("protected_namespaces", postgresql.JSONB(), nullable=False),
        sa.Column("known_down", postgresql.JSONB(), nullable=False),
        sa.Column("override_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("retention_daily", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("retention_weekly", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("backup_nfs_server", sa.String(length=200), nullable=False,
                  server_default="192.168.42.8"),
        sa.Column("backup_nfs_base", sa.String(length=300), nullable=False,
                  server_default="/volume1/Kubernetes"),
        sa.Column("daily_run_cap", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("issue_cooldown_hours", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("prometheus_url", sa.String(length=300), nullable=True),
        sa.Column("diagnose_only_until_first_report", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_waste_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(length=200), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "kube_issues",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("key", sa.String(length=300), nullable=False),
        sa.Column("namespace", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("reason", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="warning"),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_agent_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="kube_issues_key_uq"),
    )
    op.create_index("ix_kube_issues_id", "kube_issues", ["id"])
    op.create_index("ix_kube_issues_state", "kube_issues", ["state"])
    op.create_table(
        "kube_overrides",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("issue_key", sa.String(length=300), nullable=False),
        sa.Column("namespace", sa.String(length=100), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kube_overrides_id", "kube_overrides", ["id"])
    op.create_index("ix_kube_overrides_issue_key", "kube_overrides", ["issue_key"])
    op.create_table(
        "kube_reports",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kube_reports_id", "kube_reports", ["id"])
    op.create_index("ix_kube_reports_kind", "kube_reports", ["kind"])
    op.create_index("ix_kube_reports_created_at", "kube_reports", ["created_at"])


def downgrade() -> None:
    for table in ("kube_reports", "kube_overrides", "kube_issues", "kube_settings"):
        op.drop_table(table)
    op.drop_column("infra_plans", "issue_key")
    op.drop_column("infra_plans", "guard")
    op.drop_column("infra_plans", "selected_steps")
