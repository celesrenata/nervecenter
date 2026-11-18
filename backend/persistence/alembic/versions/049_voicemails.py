"""Durable voicemails and threads.

Adds ``voicemail_threads`` and ``voicemails``. No existing table is altered.
Reversibility: ``downgrade()`` drops both tables; only voicemails are lost.
"""
import sqlalchemy as sa
from alembic import op

revision = "049_voicemails"
down_revision = "048_kube_run_cap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voicemail_threads",
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("caller_phone", sa.String(length=64), nullable=False),
        sa.Column("caller_name", sa.String(length=256), nullable=True),
        sa.Column("voicemail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unread", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_transcript", sa.Text(), nullable=True),
        sa.Column("last_duration_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("needs_callback", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("callback_reason", sa.String(length=64), nullable=True),
        sa.Column("attention_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("thread_id"),
    )
    op.create_table(
        "voicemails",
        sa.Column("voicemail_id", sa.String(length=256), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("caller_phone", sa.String(length=64), nullable=False),
        sa.Column("caller_name", sa.String(length=256), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=False, server_default=""),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("needs_callback", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("callback_reason", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="share-sheet"),
        sa.PrimaryKeyConstraint("voicemail_id"),
    )
    op.create_index(
        "ix_voicemails_thread_received", "voicemails", ["thread_id", "received_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_voicemails_thread_received", table_name="voicemails")
    op.drop_table("voicemails")
    op.drop_table("voicemail_threads")
