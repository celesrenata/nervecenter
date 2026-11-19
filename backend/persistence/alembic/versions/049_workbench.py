"""Workbench: projects, sessions, model usage (counts only), IDE pods, audit log.
Additive only. Adds ``workbench_projects``, ``workbench_sessions``,
``workbench_model_usage``, ``workbench_ide`` and ``workbench_audit``.
No existing table is altered; the usage table has no prompt/completion text.
Reversibility: ``downgrade()`` drops only these five tables.
"""
import sqlalchemy as sa
from alembic import op

revision = "049_workbench"
down_revision = "049_voicemails"
branch_labels = None
depends_on = None


def _pg_only_project_checks() -> list[sa.CheckConstraint]:
    """Title length and slug regex use PostgreSQL functions (char_length, ~).

    Other dialects (the SQLite test databases) get the table without them; the
    API validates the same rules before every insert.
    """
    if op.get_bind().dialect.name != "postgresql":
        return []
    return [
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 120", name="ck_workbench_projects_title_len"
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'", name="ck_workbench_projects_slug"
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "workbench_projects",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("push_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("kind IN ('opencode','menagerie')", name="ck_workbench_projects_kind"),
        *_pg_only_project_checks(),
        sa.UniqueConstraint("slug", "kind", name="uq_workbench_projects_slug_kind"),
    )
    op.create_table(
        "workbench_sessions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("workbench_projects.id"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("small_model", sa.Text(), nullable=True),
    )
    op.create_index("ix_workbench_sessions_project_id", "workbench_sessions", ["project_id"])
    op.create_table(
        "workbench_model_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workbench_model_usage_session_id", "workbench_model_usage", ["session_id"])
    op.create_table(
        "workbench_ide",
        sa.Column(
            "project_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("workbench_projects.id"),
            primary_key=True,
        ),
        sa.Column("pod_name", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("last_traffic_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "workbench_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("project_slug", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('success','failure')", name="ck_workbench_audit_outcome"
        ),
    )


def downgrade() -> None:
    op.drop_table("workbench_audit")
    op.drop_table("workbench_ide")
    op.drop_index("ix_workbench_model_usage_session_id", table_name="workbench_model_usage")
    op.drop_table("workbench_model_usage")
    op.drop_index("ix_workbench_sessions_project_id", table_name="workbench_sessions")
    op.drop_table("workbench_sessions")
    op.drop_table("workbench_projects")
