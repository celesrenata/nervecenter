"""Resources and agent links (NS-ADR-125).

Adds ``resources`` and ``agent_resource_links``. No existing table is altered.
Reversibility: ``downgrade()`` drops both tables (stored files stay in the object
store under ``resources/`` until removed through the API).
"""
import sqlalchemy as sa
from alembic import op

revision = "039_resources"
down_revision = "038_auth_time_defaults"
branch_labels = None
depends_on = None


def _ts(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def upgrade() -> None:
    op.create_table(
        "resources",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "content_type", sa.String(length=100), nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("object_key", sa.String(length=200), nullable=False),
        sa.Column("uploaded_by", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_resources_id", "resources", ["id"])
    op.create_table(
        "agent_resource_links",
        sa.Column("agent_id", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("agent_id", "resource_id"),
    )
    op.create_index(
        "ix_agent_resource_links_resource_id", "agent_resource_links", ["resource_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_resource_links_resource_id", table_name="agent_resource_links")
    op.drop_table("agent_resource_links")
    op.drop_index("ix_resources_id", table_name="resources")
    op.drop_table("resources")
