"""NerveShield minimum security tables (Phase 1, Task 1.7).

Adds the *minimum* durable schema the NerveShield spec allows in Phase 1
(design.md §9 "Data storage and interfaces": evidence manifest, job,
assessment/findings — **not** the intel/dataset/model/action tables, which
arrive behind later gated migrations):

- ``mail_security_jobs`` — account-scoped, restartable security job. Unique on
  the deterministic Task 1.2 outbox job key within an account scope
  ``(account_scope, outbox_job_key)`` so a replay resolves to the same job
  (R-018, T-17).
- ``mail_evidence`` — immutable per-revision evidence manifest. Opaque content
  refs only (``raw_content_ref`` / ``normalized_content_ref``) — **no raw
  bytes** (design §4.1/§9/§10, T-14). Unique on
  ``(account_scope, source_id, revision)``.
- ``mail_security_assessments`` — metadata-only assessment keyed
  ``(account_scope, assessment_id)``.
- ``mail_security_findings`` — typed findings with a safe explanation code and
  an opaque evidence ref; **no raw body fragments**.

Account isolation (R-023/T-15): every table carries an explicit
``account_scope`` column with an index, modelled on
``calendar_events.account_id`` (migration ``007_calendar``). The existing
``communications`` table has no account column, which is the gap these tables
close for the security plane.

Reversibility: this migration is fully **reversible**. ``upgrade()`` creates
the four new tables and their indexes; ``downgrade()`` drops them in reverse
order. There is no data migration — these are brand-new tables, so the
downgrade cannot lose pre-existing rows. Read-only compatibility holds because
no existing table/column is altered (design §14 "Include migration downgrade
strategy and read-only compatibility").

Conventions match :mod:`015_inference_providers` / :mod:`007_calendar`:
identifier columns are ``sa.String()``, JSONB uses
``postgresql.JSONB(astext_type=sa.Text())`` so the migration stays portable to
the SQLite scratch databases the test suite relies on, user-controlled string
columns are length-capped, and indexes follow ``ix_<table>_<column>``.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "016_mail_security"
down_revision = "015_inference_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- mail_security_jobs -------------------------------------------------
    op.create_table(
        "mail_security_jobs",
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
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("outbox_job_key", sa.String(length=200), nullable=False),
        sa.Column("source_key", sa.String(length=200), nullable=False),
        sa.Column("analysis_profile_version", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="DISCOVERED"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_security_jobs_account_scope",
        "mail_security_jobs",
        ["account_scope"],
        unique=False,
    )
    op.create_index("ix_mail_security_jobs_state", "mail_security_jobs", ["state"], unique=False)
    op.create_index(
        "ix_mail_security_jobs_scope_key",
        "mail_security_jobs",
        ["account_scope", "outbox_job_key"],
        unique=True,
    )

    # -- mail_evidence ------------------------------------------------------
    op.create_table(
        "mail_evidence",
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
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("acquisition_mode", sa.String(length=32), nullable=False),
        sa.Column("evidence_fidelity", sa.String(length=32), nullable=False),
        # Opaque refs only — never raw bytes (T-14).
        sa.Column("raw_content_ref", sa.String(length=200), nullable=True),
        sa.Column("normalized_content_ref", sa.String(length=200), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "missing_parts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_evidence_account_scope",
        "mail_evidence",
        ["account_scope"],
        unique=False,
    )
    op.create_index(
        "ix_mail_evidence_scope_source_rev",
        "mail_evidence",
        ["account_scope", "source_id", "revision"],
        unique=True,
    )

    # -- mail_security_assessments -----------------------------------------
    op.create_table(
        "mail_security_assessments",
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
        sa.Column("assessment_id", sa.String(length=160), nullable=False),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("analysis_profile_version", sa.String(length=100), nullable=False),
        sa.Column("model_version", sa.String(length=160), nullable=True),
        sa.Column("policy_version", sa.String(length=100), nullable=False),
        sa.Column("processing_state", sa.String(length=32), nullable=False),
        sa.Column("evidence_fidelity", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="SHADOW"),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column(
            "assessment",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_security_assessments_account_scope",
        "mail_security_assessments",
        ["account_scope"],
        unique=False,
    )
    op.create_index(
        "ix_mail_security_assessments_disposition",
        "mail_security_assessments",
        ["disposition"],
        unique=False,
    )
    op.create_index(
        "ix_mail_security_assessments_scope_asmt",
        "mail_security_assessments",
        ["account_scope", "assessment_id"],
        unique=True,
    )

    # -- mail_security_findings --------------------------------------------
    op.create_table(
        "mail_security_findings",
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
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("assessment_id", sa.String(length=160), nullable=False),
        sa.Column("finding_key", sa.String(length=200), nullable=False),
        sa.Column("rule_id", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("confidence_semantics", sa.String(length=32), nullable=True),
        sa.Column(
            "confidence_value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        # Opaque evidence ref + safe code only — never raw body fragments.
        sa.Column("evidence_ref", sa.String(length=200), nullable=True),
        sa.Column("explanation_code", sa.String(length=80), nullable=False),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mail_security_findings_account_scope",
        "mail_security_findings",
        ["account_scope"],
        unique=False,
    )
    op.create_index(
        "ix_mail_security_findings_scope_assessment",
        "mail_security_findings",
        ["account_scope", "assessment_id"],
        unique=False,
    )
    op.create_index(
        "ix_mail_security_findings_scope_asmt_key",
        "mail_security_findings",
        ["account_scope", "assessment_id", "finding_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_mail_security_findings_scope_asmt_key", table_name="mail_security_findings")
    op.drop_index("ix_mail_security_findings_scope_assessment", table_name="mail_security_findings")
    op.drop_index("ix_mail_security_findings_account_scope", table_name="mail_security_findings")
    op.drop_table("mail_security_findings")

    op.drop_index(
        "ix_mail_security_assessments_scope_asmt",
        table_name="mail_security_assessments",
    )
    op.drop_index(
        "ix_mail_security_assessments_disposition",
        table_name="mail_security_assessments",
    )
    op.drop_index(
        "ix_mail_security_assessments_account_scope",
        table_name="mail_security_assessments",
    )
    op.drop_table("mail_security_assessments")

    op.drop_index("ix_mail_evidence_scope_source_rev", table_name="mail_evidence")
    op.drop_index("ix_mail_evidence_account_scope", table_name="mail_evidence")
    op.drop_table("mail_evidence")

    op.drop_index("ix_mail_security_jobs_scope_key", table_name="mail_security_jobs")
    op.drop_index("ix_mail_security_jobs_state", table_name="mail_security_jobs")
    op.drop_index("ix_mail_security_jobs_account_scope", table_name="mail_security_jobs")
    op.drop_table("mail_security_jobs")
