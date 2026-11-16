"""Financial helper tables (NS-ADR-102).

Adds ``debt_accounts``, ``statement_imports`` and ``ledger_entries``. No existing
table is altered. Reversibility: ``downgrade()`` drops the three tables; only the
imported ledger and manually entered debts are lost.
"""
import sqlalchemy as sa
from alembic import op

revision = "029_finance"
down_revision = "028_mental_health"
branch_labels = None
depends_on = None


def _ts(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def upgrade() -> None:
    op.create_table(
        "debt_accounts",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("institution", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("balance_cents", sa.BigInteger(), nullable=False),
        sa.Column("apr_pct", sa.String(length=16), nullable=False, server_default="0"),
        sa.Column("min_payment_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("due_day", sa.Integer(), nullable=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debt_accounts_id", "debt_accounts", ["id"])
    op.create_table(
        "statement_imports",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        sa.Column("file_digest", sa.String(length=64), nullable=False),
        sa.Column("institution", sa.String(length=32), nullable=False),
        sa.Column("file_format", sa.String(length=8), nullable=False),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("added_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["debt_accounts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("file_digest", name="uq_statement_imports_file_digest"),
    )
    op.create_index("ix_statement_imports_id", "statement_imports", ["id"])
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.String(), nullable=False),
        _ts("created_at"),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("institution", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("import_id", sa.String(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["debt_accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["import_id"], ["statement_imports.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("dedupe_key", name="uq_ledger_entries_dedupe_key"),
    )
    op.create_index("ix_ledger_entries_id", "ledger_entries", ["id"])
    op.create_index("ix_ledger_entries_entry_date", "ledger_entries", ["entry_date"])


def downgrade() -> None:
    op.drop_index("ix_ledger_entries_entry_date", table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_id", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_index("ix_statement_imports_id", table_name="statement_imports")
    op.drop_table("statement_imports")
    op.drop_index("ix_debt_accounts_id", table_name="debt_accounts")
    op.drop_table("debt_accounts")
