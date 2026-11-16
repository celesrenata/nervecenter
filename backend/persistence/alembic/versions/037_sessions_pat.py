"""Durable NerveCenter sessions and personal API tokens (NS-ADR-123, NS-ADR-124).

Adds ``auth_refresh_sessions`` and ``personal_api_tokens``. No existing table is
altered. Reversibility: ``downgrade()`` drops both tables (users sign in again;
personal tokens stop working and are re-issued).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "037_sessions_pat"
down_revision = "036_infra"
branch_labels = None
depends_on = None


def _ts(name: str, *, nullable: bool = False, server_now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if server_now else None,
    )


def upgrade() -> None:
    op.create_table(
        "auth_refresh_sessions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("family_id", sa.String(length=64), nullable=False),
        sa.Column("sub", sa.String(length=255), nullable=False),
        sa.Column("identity", postgresql.JSONB(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="sso"),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        _ts("issued_at", server_now=True),
        _ts("last_used_at", nullable=True),
        _ts("idle_expires_at"),
        _ts("absolute_expires_at"),
        _ts("rotated_at", nullable=True),
        sa.Column("replaced_by", sa.String(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _ts("revoked_at", nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auth_refresh_sessions_token_hash",
        "auth_refresh_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_auth_refresh_sessions_family_id", "auth_refresh_sessions", ["family_id"]
    )
    op.create_index("ix_auth_refresh_sessions_sub", "auth_refresh_sessions", ["sub"])
    op.create_table(
        "personal_api_tokens",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_sub", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        _ts("created_at", server_now=True),
        _ts("expires_at", nullable=True),
        _ts("last_used_at", nullable=True),
        _ts("revoked_at", nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_personal_api_tokens_token_hash",
        "personal_api_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_personal_api_tokens_owner_sub", "personal_api_tokens", ["owner_sub"]
    )


def downgrade() -> None:
    op.drop_index("ix_personal_api_tokens_owner_sub", table_name="personal_api_tokens")
    op.drop_index("ix_personal_api_tokens_token_hash", table_name="personal_api_tokens")
    op.drop_table("personal_api_tokens")
    op.drop_index("ix_auth_refresh_sessions_sub", table_name="auth_refresh_sessions")
    op.drop_index("ix_auth_refresh_sessions_family_id", table_name="auth_refresh_sessions")
    op.drop_index("ix_auth_refresh_sessions_token_hash", table_name="auth_refresh_sessions")
    op.drop_table("auth_refresh_sessions")
