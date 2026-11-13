"""NerveShield expectedness feedback (NS-ADR-078).

Adds one table, ``mail_expectedness_feedback``: append-only "this was
expected" records (keyed sender, request type, weight, expiry, opaque
reviewer ref, revoked_at for undo). No address, subject or body.

Reversibility: fully reversible. The table is new; ``downgrade()`` drops it
(indexes first). Dropping it only loses feedback.
"""

import sqlalchemy as sa
from alembic import op

revision = "025_mail_expect_feedback"
down_revision = "024_mail_expectedness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_expectedness_feedback",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("feedback_id", sa.String(length=64), nullable=False),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("assessment_id", sa.String(length=160), nullable=False),
        sa.Column("source_key", sa.String(length=200), nullable=False),
        sa.Column("sender_key", sa.String(length=32), nullable=False),
        sa.Column("request_type", sa.String(length=40), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewer_ref", sa.String(length=80), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_expect_fb_id", "mail_expectedness_feedback", ["feedback_id"], unique=True
    )
    op.create_index(
        "ix_mail_expect_fb_sender", "mail_expectedness_feedback", ["account_scope", "sender_key"]
    )
    op.create_index(
        "ix_mail_expect_fb_source", "mail_expectedness_feedback", ["account_scope", "source_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_mail_expect_fb_source", table_name="mail_expectedness_feedback")
    op.drop_index("ix_mail_expect_fb_sender", table_name="mail_expectedness_feedback")
    op.drop_index("ix_mail_expect_fb_id", table_name="mail_expectedness_feedback")
    op.drop_table("mail_expectedness_feedback")
