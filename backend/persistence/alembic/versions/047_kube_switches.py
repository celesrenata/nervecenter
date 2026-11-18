"""Kube agent switches: per-namespace on/off lists and the three feature switches.

Adds to ``kube_settings``: ``known_down_off`` and ``protected_off`` (namespaces on those lists
that the owner switched off, so they are checked like any other), and ``check_enabled``,
``waste_enabled``, ``auto_agent_enabled`` (all true). Reversibility: ``downgrade()`` drops them.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "047_kube_switches"
down_revision = "045_kube_agent"
branch_labels = None
depends_on = None

_SWITCHES = ("check_enabled", "waste_enabled", "auto_agent_enabled")


def upgrade() -> None:
    for name in ("known_down_off", "protected_off"):
        op.add_column("kube_settings", sa.Column(
            name, postgresql.JSONB(), nullable=False, server_default="[]"))
    for name in _SWITCHES:
        op.add_column("kube_settings", sa.Column(
            name, sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    for name in (*_SWITCHES, "protected_off", "known_down_off"):
        op.drop_column("kube_settings", name)
