"""NerveShield personal expectedness counts (8.3 follow-on, NS-ADR-075).

Adds one table, ``mail_expectedness_counts``: per account, per HMAC sender
key, per request type, a count. No address, subject, body or message id.

Reversibility: fully reversible. The table is new; ``downgrade()`` drops it
(index first). Dropping it only forgets learned counts.
"""

import sqlalchemy as sa
from alembic import op

revision = "024_mail_expectedness"
down_revision = "023_mail_semantic_obs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_expectedness_counts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("sender_key", sa.String(length=32), nullable=False),
        sa.Column("request_type", sa.String(length=40), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_expect_key",
        "mail_expectedness_counts",
        ["account_scope", "sender_key", "request_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_mail_expect_key", table_name="mail_expectedness_counts")
    op.drop_table("mail_expectedness_counts")
