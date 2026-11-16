"""Infrastructure connectors and change plans (NS-ADR-120).

Adds ``infra_connectors`` and ``infra_plans``. No existing table is altered.
Reversibility: ``downgrade()`` drops both tables (connector credentials stay in
``secrets`` until the connectors are deleted through the API).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "036_infra"
down_revision = "035_agent_overrides_keys"
branch_labels = None
depends_on = None


def _ts(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _jsonb(name: str, default: str) -> sa.Column:
    # The ORM supplies the default ({} or []); no server default, so the
    # migration also renders on SQLite (like the other migrations).
    del default
    return sa.Column(name, postgresql.JSONB(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "infra_connectors",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        _jsonb("config", "'{}'::jsonb"),
        sa.Column(
            "host_key_status", sa.String(length=16), nullable=False, server_default="unpinned"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_infra_connectors_name", "infra_connectors", ["name"], unique=True)
    op.create_table(
        "infra_plans",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.String(), nullable=False, server_default=""),
        _jsonb("steps", "'[]'::jsonb"),
        _jsonb("verify", "'[]'::jsonb"),
        sa.Column("rollback", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="proposed"),
        sa.Column(
            "act_mode", sa.String(length=16), nullable=False, server_default="opencode"
        ),
        sa.Column("decided_by", sa.String(length=200), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        _jsonb("results", "'[]'::jsonb"),
        sa.Column("handoff_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_infra_plans_status", "infra_plans", ["status"])


def downgrade() -> None:
    op.drop_index("ix_infra_plans_status", table_name="infra_plans")
    op.drop_table("infra_plans")
    op.drop_index("ix_infra_connectors_name", table_name="infra_connectors")
    op.drop_table("infra_connectors")
