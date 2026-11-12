"""NerveShield pre-promotion trust gate holds (Phase 7, Task 7.4).

Adds exactly one table, ``mail_security_held_promotions``: legacy actionable
writes (attention items / commitments) held by the OFF-by-default
``REVIEW_GATED`` gate under their exact deterministic target id, with status
HELD / RELEASED / REVIEW_CANDIDATE and the deciding verdict for audit.

Reversibility: fully reversible. The table is new; ``downgrade()`` drops it
(indexes first). No existing table or column is altered. With the gate OFF
nothing writes here. Rolling back while holds exist discards them: HELD and
REVIEW_CANDIDATE items never reached the actionable tables, so nothing
actionable is lost or created; re-ingest would re-detect them.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "019_mail_held_promotions"
down_revision = "018_mail_security_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_security_held_promotions",
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
            server_default="mail-promotion-gate@1",
        ),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("content_ref", sa.String(length=200), nullable=False),
        sa.Column("communication_id", sa.String(length=200), nullable=False),
        sa.Column("target_kind", sa.String(length=24), nullable=False),
        sa.Column("target_id", sa.String(length=80), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="HELD"),
        sa.Column("decided_disposition", sa.String(length=32), nullable=True),
        sa.Column("decided_assessment_id", sa.String(length=160), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_security_held_target",
        "mail_security_held_promotions",
        ["target_kind", "target_id"],
        unique=True,
    )
    op.create_index(
        "ix_mail_security_held_scope_status",
        "mail_security_held_promotions",
        ["account_scope", "status"],
        unique=False,
    )
    op.create_index(
        "ix_mail_security_held_content_ref",
        "mail_security_held_promotions",
        ["content_ref"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mail_security_held_content_ref", table_name="mail_security_held_promotions")
    op.drop_index("ix_mail_security_held_scope_status", table_name="mail_security_held_promotions")
    op.drop_index("ix_mail_security_held_target", table_name="mail_security_held_promotions")
    op.drop_table("mail_security_held_promotions")
