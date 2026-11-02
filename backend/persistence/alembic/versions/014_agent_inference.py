"""Per-agent inference binding (INFER CONTROL CONTRACT §3).

Adds a nullable JSONB ``inference`` column to the ``agents`` table backing the
per-agent inference binding — ``{"provider": str, "model": str}`` (either key
optional) or ``NULL`` meaning "unbound → use the default". The binding is set
on ``POST /api/agents`` and overwritable via ``PATCH /api/agents/{id}`` (absent
leaves it unchanged), and read by the ask endpoint's resolution (contract §2).

Conventions match :mod:`004_incidents_agents`: JSONB uses
``postgresql.JSONB(astext_type=sa.Text())`` so the migration stays portable to
the SQLite scratch databases used by the test suite.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "014_agent_inference"
down_revision = "013_bill_series"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "inference",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default=None,
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "inference")
