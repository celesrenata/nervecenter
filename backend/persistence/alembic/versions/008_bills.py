"""Bills plane durable tables (Personal-Ops Recovery §7 — workstream G).

Creates the two tables backing the durable bills plane:

- ``bills`` — an obligation the user may have to pay: ``payee`` /
  ``amount_cents`` (string decimal — no float drift in financial records) /
  ``currency`` / tz-aware ``due_at`` / ``recurrence``
  (``one-time|weekly|monthly|other``) + JSONB ``interval`` payload /
  ``status`` (``pending|confirmed_paid|disputed|stale``) / ``confirmed_at``
  (set ONLY by an explicit user action — a passing due date never confirms) /
  JSONB ``evidence`` (``[{source_kind, source_id, snippet, confidence}]``) /
  self ``duplicate_of`` FK.
- ``bill_candidates`` — an extracted *proposal* from a communication or
  uploaded document: ``source_kind`` / ``source_id`` / ``status``
  (``proposed|accepted|rejected``) / JSONB ``extracted_fields`` with a
  per-field confidence map / JSONB ``evidence`` (matched snippets) /
  JSONB ``needs_review`` (fields below the confidence threshold) / self
  ``duplicate_of`` FK (dedup link) / ``bill_id`` FK (set when the user
  explicitly accepts the candidate and it is promoted to a bill).

Numbering note: the §6 freeze text lists the bills tables as migration
``007`` in a 007–009 sequence (bills / routines / wellness) that has not
landed in this tree — the linear alembic chain head is
``007_calendar``. This revision therefore creates the tables as
``008_bills`` directly on top of ``007_calendar``; the column set is the
frozen one. The parent integrator rewires the chain order after integration.

Conventions match :mod:`007_calendar` / :mod:`006_personal_plane`:
identifier columns are ``sa.String()`` (string UUID ids — no database UUID
columns), JSONB columns use ``postgresql.JSONB(astext_type=sa.Text())`` so
the migration stays portable to the SQLite scratch databases used by the
test suite, timestamps use ``sa.DateTime(timezone=True)`` with a ``now()``
server default, and index names follow the ``ix_<table>_<column>``
convention.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "008_bills"
down_revision = "007_calendar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bills",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("payee", sa.String(length=512), nullable=False),
        sa.Column("amount_cents", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recurrence", sa.String(length=16), nullable=False, server_default="one-time"),
        sa.Column(
            "interval",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("duplicate_of", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["duplicate_of"], ["bills.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bills_payee", "bills", ["payee"], unique=False)
    op.create_index("ix_bills_due_at", "bills", ["due_at"], unique=False)
    op.create_index("ix_bills_status", "bills", ["status"], unique=False)
    op.create_index("ix_bills_duplicate_of", "bills", ["duplicate_of"], unique=False)

    op.create_table(
        "bill_candidates",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v0"),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="proposed"),
        sa.Column(
            "extracted_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "needs_review",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("duplicate_of", sa.String(), nullable=True),
        sa.Column("bill_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["duplicate_of"], ["bill_candidates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bill_candidates_status", "bill_candidates", ["status"], unique=False
    )
    op.create_index(
        "ix_bill_candidates_source_id", "bill_candidates", ["source_id"], unique=False
    )
    op.create_index(
        "ix_bill_candidates_bill_id", "bill_candidates", ["bill_id"], unique=False
    )
    op.create_index(
        "ix_bill_candidates_duplicate_of",
        "bill_candidates",
        ["duplicate_of"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_bill_candidates_duplicate_of", table_name="bill_candidates")
    op.drop_index("ix_bill_candidates_bill_id", table_name="bill_candidates")
    op.drop_index("ix_bill_candidates_source_id", table_name="bill_candidates")
    op.drop_index("ix_bill_candidates_status", table_name="bill_candidates")
    op.drop_table("bill_candidates")

    op.drop_index("ix_bills_duplicate_of", table_name="bills")
    op.drop_index("ix_bills_status", table_name="bills")
    op.drop_index("ix_bills_due_at", table_name="bills")
    op.drop_index("ix_bills_payee", table_name="bills")
    op.drop_table("bills")
