"""NerveShield per-account content campaigns (G8 item 6, NS-ADR-069).

Adds two tables: ``mail_campaign_members`` (account-keyed MinHash signature,
campaign id, opaque body ref, 30-day expiry) and ``mail_campaign_bands`` (HMAC
LSH band keys for candidate lookup). No subject, body, address or raw token.

Reversibility: fully reversible. Both tables are new; ``downgrade()`` drops
them (indexes first). No existing table or column is altered. The data is
derived and expires within 30 days anyway.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "022_mail_campaigns"
down_revision = "021_mail_thread_ledger"
branch_labels = None
depends_on = None

_JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "mail_campaign_members",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(length=32),
            nullable=False,
            server_default="mail-campaigns@1",
        ),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("source_key", sa.String(length=200), nullable=False),
        sa.Column("content_ref", sa.String(length=200), nullable=True),
        sa.Column("campaign_id", sa.String(length=32), nullable=False),
        sa.Column("signature", _JSON, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_campaign_member_src",
        "mail_campaign_members",
        ["account_scope", "source_key"],
        unique=True,
    )
    op.create_index(
        "ix_mail_campaign_member_campaign",
        "mail_campaign_members",
        ["account_scope", "campaign_id"],
    )
    op.create_index("ix_mail_campaign_member_expiry", "mail_campaign_members", ["expires_at"])
    op.create_table(
        "mail_campaign_bands",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("band_key", sa.String(length=32), nullable=False),
        sa.Column("member_id", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_campaign_band_key", "mail_campaign_bands", ["account_scope", "band_key"]
    )
    op.create_index("ix_mail_campaign_band_member", "mail_campaign_bands", ["member_id"])
    op.create_index("ix_mail_campaign_band_expiry", "mail_campaign_bands", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_mail_campaign_band_expiry", table_name="mail_campaign_bands")
    op.drop_index("ix_mail_campaign_band_member", table_name="mail_campaign_bands")
    op.drop_index("ix_mail_campaign_band_key", table_name="mail_campaign_bands")
    op.drop_table("mail_campaign_bands")
    op.drop_index("ix_mail_campaign_member_expiry", table_name="mail_campaign_members")
    op.drop_index("ix_mail_campaign_member_campaign", table_name="mail_campaign_members")
    op.drop_index("ix_mail_campaign_member_src", table_name="mail_campaign_members")
    op.drop_table("mail_campaign_members")
