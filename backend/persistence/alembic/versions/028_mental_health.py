"""Mental-health daily forms (NS-ADR-101).

Adds one new table, ``mh_forms``. No existing table is altered.
Reversibility: ``downgrade()`` drops the table; only stored form answers are lost.
"""
import sqlalchemy as sa
from alembic import op

revision = "028_mental_health"
down_revision = "027_health_samples"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mh_forms",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("form_date", sa.Date(), nullable=False),
        sa.Column("instrument", sa.String(length=8), nullable=False),
        sa.Column("total_score", sa.Integer(), nullable=False),
        sa.Column("answers_enc", sa.Text(), nullable=False),
        sa.Column("comment_enc", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("form_date", "instrument", name="uq_mh_forms_date_instrument"),
    )
    op.create_index("ix_mh_forms_id", "mh_forms", ["id"])
    op.create_index("ix_mh_forms_form_date", "mh_forms", ["form_date"])


def downgrade() -> None:
    op.drop_index("ix_mh_forms_form_date", table_name="mh_forms")
    op.drop_index("ix_mh_forms_id", table_name="mh_forms")
    op.drop_table("mh_forms")
