"""NerveShield threat-intelligence store (Phase 2, Task 2.2).

Adds exactly two tables (design.md §5.3 / §9 ``mail_intel_indicators``):

- ``mail_intel_indicators`` — global, time-limited, versioned indicators.
  Canonical value or SHA-256 hash only; explicit source, confidence *type*,
  license, provenance, feed version, first/last seen, ``expires_at``, scope,
  ``enabled``, promotion version and rollback flags. No account column
  (indicators are global threat intel, not per-mailbox) and no free text.
- ``mail_intel_overrides`` — account-scoped (``account_scope`` + index),
  expiring user overrides scoped by sender + indicator and carrying an opaque
  approval reference (R-006.4, T-30, R-023).

Reversibility: fully reversible. Both tables are brand new; ``downgrade()``
drops them (indexes first) and cannot lose pre-existing rows. No existing table
or column is altered, so older code remains read-compatible. Rolling back this
migration discards stored intel/overrides; intel is re-ingestible from its feed
and overrides must be re-approved (fail-closed: losing an override makes a
match visible again, never hides one).
"""
import sqlalchemy as sa
from alembic import op

revision = "017_mail_intel"
down_revision = "016_mail_security"
branch_labels = None
depends_on = None


def _base_columns() -> list[sa.Column]:
    return [
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
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
    ]


def upgrade() -> None:
    op.create_table(
        "mail_intel_indicators",
        *_base_columns(),
        sa.Column("indicator_type", sa.String(length=24), nullable=False),
        sa.Column("canonical_indicator", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("confidence_type", sa.String(length=24), nullable=False),
        sa.Column("license", sa.String(length=64), nullable=False),
        sa.Column("provenance", sa.String(length=200), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="GLOBAL"),
        sa.Column("feed_version", sa.String(length=64), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("promotion_version", sa.Integer(), nullable=False),
        sa.Column("superseded_in_version", sa.Integer(), nullable=True),
        sa.Column("rolled_back", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_intel_indicators_lookup",
        "mail_intel_indicators",
        ["indicator_type", "canonical_indicator"],
        unique=False,
    )
    op.create_index(
        "ix_mail_intel_indicators_version",
        "mail_intel_indicators",
        ["promotion_version"],
        unique=False,
    )
    op.create_index(
        "ix_mail_intel_indicators_expires_at",
        "mail_intel_indicators",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "mail_intel_overrides",
        *_base_columns(),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("sender_key", sa.String(length=200), nullable=False),
        sa.Column("indicator_type", sa.String(length=24), nullable=False),
        sa.Column("canonical_indicator", sa.String(length=255), nullable=False),
        sa.Column("approval_ref", sa.String(length=200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_intel_overrides_account_scope",
        "mail_intel_overrides",
        ["account_scope"],
        unique=False,
    )
    op.create_index(
        "ix_mail_intel_overrides_scope_lookup",
        "mail_intel_overrides",
        ["account_scope", "sender_key", "indicator_type", "canonical_indicator"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mail_intel_overrides_scope_lookup", table_name="mail_intel_overrides")
    op.drop_index("ix_mail_intel_overrides_account_scope", table_name="mail_intel_overrides")
    op.drop_table("mail_intel_overrides")
    op.drop_index("ix_mail_intel_indicators_expires_at", table_name="mail_intel_indicators")
    op.drop_index("ix_mail_intel_indicators_version", table_name="mail_intel_indicators")
    op.drop_index("ix_mail_intel_indicators_lookup", table_name="mail_intel_indicators")
    op.drop_table("mail_intel_indicators")
