"""NerveShield conversation ledger (G8 item 2, NS-ADR-067).

Adds exactly one table, ``mail_thread_ledger``: per-account HMAC fingerprints
of message ids and threads, registrable domains, and the SHADOW continuity
status / signal codes for inbound mail. No subject, body, local part or
address.

Reversibility: fully reversible. The table is new; ``downgrade()`` drops it
(indexes first). No existing table or column is altered. The ledger is
derived data: dropping it only loses conversation memory, which re-seeds.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "021_mail_thread_ledger"
down_revision = "020_mail_confirmations"
branch_labels = None
depends_on = None

_JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "mail_thread_ledger",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "schema_version",
            sa.String(length=32),
            nullable=False,
            server_default="mail-thread-ledger@1",
        ),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("message_fp", sa.String(length=32), nullable=False),
        sa.Column("thread_fp", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("sender_domain", sa.String(length=253), nullable=True),
        sa.Column("reply_to_domain", sa.String(length=253), nullable=True),
        sa.Column("participant_domains", _JSON, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("signals", _JSON, nullable=False),
        sa.Column("content_ref", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_thread_ledger_msg",
        "mail_thread_ledger",
        ["account_scope", "message_fp"],
        unique=True,
    )
    op.create_index(
        "ix_mail_thread_ledger_thread",
        "mail_thread_ledger",
        ["account_scope", "thread_fp"],
        unique=False,
    )
    op.create_index(
        "ix_mail_thread_ledger_status",
        "mail_thread_ledger",
        ["account_scope", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mail_thread_ledger_status", table_name="mail_thread_ledger")
    op.drop_index("ix_mail_thread_ledger_thread", table_name="mail_thread_ledger")
    op.drop_index("ix_mail_thread_ledger_msg", table_name="mail_thread_ledger")
    op.drop_table("mail_thread_ledger")
