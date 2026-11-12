"""NerveShield explicit confirmations (Phase 7, Task 7.6).

Adds exactly one table, ``mail_security_confirmations``: action-specific,
expiring confirmations of email-origin proposals with the exact execution
hash, the issued approval record, a unique single-use step-up proof id, the
out-of-band sender attestation and a codes-only audit trail.

Reversibility: fully reversible. The table is new; ``downgrade()`` drops it
(indexes first). No existing table or column is altered. Nothing executes
from these rows in this phase, so rolling back loses only pending/decided
confirmations (they expire within an hour anyway).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "020_mail_confirmations"
down_revision = "019_mail_held_promotions"
branch_labels = None
depends_on = None

_JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "mail_security_confirmations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "schema_version",
            sa.String(length=32),
            nullable=False,
            server_default="mail-confirmation@1",
        ),
        sa.Column("confirmation_id", sa.String(length=64), nullable=False),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), nullable=False),
        sa.Column("proposal", _JSON, nullable=False),
        sa.Column("execution_hash", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("risk", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False),
        sa.Column("requested_by", sa.String(length=80), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("approval", _JSON, nullable=True),
        sa.Column("step_up_proof_id", sa.String(length=96), nullable=True),
        sa.Column("sender_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reject_reason", sa.String(length=32), nullable=True),
        sa.Column("audit", _JSON, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_security_conf_id",
        "mail_security_confirmations",
        ["confirmation_id"],
        unique=True,
    )
    op.create_index(
        "ix_mail_security_conf_scope",
        "mail_security_confirmations",
        ["account_scope", "status"],
        unique=False,
    )
    op.create_index(
        "ix_mail_security_conf_step_up",
        "mail_security_confirmations",
        ["step_up_proof_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_mail_security_conf_step_up", table_name="mail_security_confirmations")
    op.drop_index("ix_mail_security_conf_scope", table_name="mail_security_confirmations")
    op.drop_index("ix_mail_security_conf_id", table_name="mail_security_confirmations")
    op.drop_table("mail_security_confirmations")
