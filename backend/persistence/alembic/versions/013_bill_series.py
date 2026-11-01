"""bill series linking + payment-confirmation flags (P2-B)

Revises: 012
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "013_bill_series"
down_revision = "012_unified_obligations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bills",
        sa.Column(
            "series_key",
            sa.String(length=255),
            nullable=True,
            server_default=None,
        ),
    )
    op.create_index(
        "ix_bills_series_key",
        "bills",
        ["series_key"],
    )
    op.add_column(
        "bills",
        sa.Column(
            "needs_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "bills",
        sa.Column(
            "recurrence_of",
            sa.String(length=64),
            nullable=True,
            server_default=None,
        ),
    )
    op.create_index(
        "ix_bills_recurrence_of",
        "bills",
        ["recurrence_of"],
    )


def downgrade() -> None:
    op.drop_index("ix_bills_recurrence_of", table_name="bills")
    op.drop_column("bills", "recurrence_of")
    op.drop_column("bills", "needs_confirmation")
    op.drop_index("ix_bills_series_key", table_name="bills")
    op.drop_column("bills", "series_key")
