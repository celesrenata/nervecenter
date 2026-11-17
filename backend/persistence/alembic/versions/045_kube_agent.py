"""Kube agent: per-step plan approval and guard columns on infra_plans (NS-ADR-120).

Adds ``infra_plans.selected_steps`` (JSONB, null = every step), ``guard`` (null or ``kube``) and
``issue_key``. No existing column is altered. Reversibility: ``downgrade()`` drops them.
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


def downgrade() -> None:
    op.drop_column("infra_plans", "issue_key")
    op.drop_column("infra_plans", "guard")
    op.drop_column("infra_plans", "selected_steps")
