"""Kube doctor runs per group: the daily run cap default goes from 8 to 24.

Only a row still at the old default (8) is changed; an owner-chosen value is kept.
Reversibility: ``downgrade()`` puts rows at 24 back to 8.
"""
import sqlalchemy as sa
from alembic import op

revision = "048_kube_run_cap"
down_revision = "047_kube_switches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE kube_settings SET daily_run_cap = 24 WHERE daily_run_cap = 8"))


def downgrade() -> None:
    op.execute(sa.text("UPDATE kube_settings SET daily_run_cap = 8 WHERE daily_run_cap = 24"))
