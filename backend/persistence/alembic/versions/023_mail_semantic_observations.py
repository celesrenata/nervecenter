"""NerveShield SHADOW semantic observations (G8 item 3, NS-ADR-073).

Adds one table, ``mail_semantic_observations``: per message revision, the
observer status, prompt version, route digest, attempts, latency and an
enum-code-only observation. No gist, subject, body or address.

Reversibility: fully reversible. The table is new; ``downgrade()`` drops it
(indexes first). No existing table or column is altered.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "023_mail_semantic_obs"
down_revision = "022_mail_campaigns"
branch_labels = None
depends_on = None

_JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "mail_semantic_observations",
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
            server_default="mail-observation@1",
        ),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("source_key", sa.String(length=200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=48), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("route_digest", sa.String(length=80), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("observation", _JSON, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_semantic_obs_src",
        "mail_semantic_observations",
        ["account_scope", "source_key", "revision"],
        unique=True,
    )
    op.create_index(
        "ix_mail_semantic_obs_status",
        "mail_semantic_observations",
        ["account_scope", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_mail_semantic_obs_status", table_name="mail_semantic_observations")
    op.drop_index("ix_mail_semantic_obs_src", table_name="mail_semantic_observations")
    op.drop_table("mail_semantic_observations")
