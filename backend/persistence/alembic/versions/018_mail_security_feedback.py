"""NerveShield reviewer feedback ledger (Phase 7, Task 7.2).

Adds exactly one table, ``mail_security_feedback`` (design.md §9): append-only,
account-scoped reviewer feedback holding codes and opaque refs only (verdict,
ledger kind, human source, hashed reviewer ref, disposition seen, routed
proposals). No free text, no addresses, no content.

Reversibility: fully reversible. The table is new; ``downgrade()`` drops it
(indexes first). No existing table or column is altered. Rolling back discards
recorded feedback only; no other store depends on it (proposals are never
applied automatically).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "018_mail_security_feedback"
down_revision = "017_mail_intel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_security_feedback",
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
            server_default="mail-feedback@1",
        ),
        sa.Column("feedback_id", sa.String(length=80), nullable=False),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("assessment_id", sa.String(length=160), nullable=False),
        sa.Column("verdict", sa.String(length=24), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("reviewer_ref", sa.String(length=80), nullable=False),
        sa.Column("disposition_seen", sa.String(length=32), nullable=False),
        sa.Column(
            "proposals",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_security_feedback_scope_id",
        "mail_security_feedback",
        ["account_scope", "feedback_id"],
        unique=True,
    )
    op.create_index(
        "ix_mail_security_feedback_scope_assessment",
        "mail_security_feedback",
        ["account_scope", "assessment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mail_security_feedback_scope_assessment", table_name="mail_security_feedback"
    )
    op.drop_index("ix_mail_security_feedback_scope_id", table_name="mail_security_feedback")
    op.drop_table("mail_security_feedback")
