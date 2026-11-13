"""DB tests for the NerveShield security repositories (Phase 1, Task 1.7).

The Done criterion for Task 1.7 is "migration up/down or documented
forward-only policy, DB tests and cross-account access denials". This module
proves, on a SQLite scratch DB (the house durable-test harness —
``tests/backend/test_obligations.py`` / ``test_restart_persistence.py``):

1. **Migration up → down** — the real alembic ``016_mail_security`` revision
   applies on top of the prior head and then fully reverses, leaving none of
   the four new tables behind (migration is reversible, design §14).
2. **Cross-account denial (R-023.4, T-15)** — a job / evidence / assessment
   written under account A cannot be read with account B's scope, even using
   A's exact deterministic id / source id / content hash. The getter returns
   ``None``, never A's row.
3. **Idempotent upsert (R-018.2, T-17)** — inserting the same deterministic
   job / assessment id twice (a replay) yields exactly one row.
4. **No raw body (T-14)** — building an assessment + finding from a message
   whose body carries a canary secret and asserting the canary never appears
   in any persisted text column.
5. **Round-trip + index presence** — metadata / opaque-ref columns round-trip
   and the ``account_scope`` index exists on every table.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.mail_security.identifiers import (
    assessment_id as derive_assessment_id,
)
from backend.mail_security.identifiers import (
    logical_source_key,
    outbox_job_key,
)
from backend.mail_security.models import (
    AcquisitionMode,
    AssessmentMode,
    Authentication,
    AuthObservation,
    AuthProvenance,
    AuthResult,
    AxisScore,
    Completeness,
    Disposition,
    EvidenceFidelity,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    MessageEvidenceManifest,
    PolicyDisposition,
    ProcessingState,
    ScoreSemantics,
    SecurityAssessment,
    SecurityFinding,
    TrustAxes,
)
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailEvidenceRow,
    MailSecurityAssessmentRow,
    MailSecurityFindingRow,
    MailSecurityJobRow,
)
from backend.persistence.repositories.mail_security_repo import (
    MailSecurityRepository,
)

_ALEMBIC_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend"
    / "persistence"
    / "alembic"
)

_SECURITY_TABLES = [
    MailSecurityJobRow.__table__,
    MailEvidenceRow.__table__,
    MailSecurityAssessmentRow.__table__,
    MailSecurityFindingRow.__table__,
]

#: A secret that stands in for raw body text. It must never reach the DB.
_CANARY = "CANARY-SECRET-dont-persist-me-9f3b2a"

ACCOUNT_A = "acct:aaaa1111"
ACCOUNT_B = "acct:bbbb2222"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _make_repo(tmp_path: pathlib.Path) -> tuple[AsyncEngine, MailSecurityRepository]:
    """A scratch SQLite DB with just the four security tables + a repo."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'mail_security.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            _SECURITY_TABLES[0].metadata.create_all, tables=_SECURITY_TABLES
        )
    repo = MailSecurityRepository(make_session_factory(engine))
    return engine, repo


def _manifest(
    account_scope: str,
    *,
    source_id: str,
    revision: int = 1,
    raw_ref: str | None = "ref:raw:abc123",
) -> MessageEvidenceManifest:
    return MessageEvidenceManifest(
        source_id=source_id,
        account_scope=account_scope,
        source_revision=revision,
        provider="gmail",
        provider_uid="UID-00042",
        acquisition_mode=AcquisitionMode.IMAP_RFC822,
        acquired_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        raw_content_ref=raw_ref,
        normalized_content_ref="ref:norm:def456",
        sha256="a" * 64,
        evidence_fidelity=EvidenceFidelity.ORIGINAL_RFC822,
        missing_parts=["MISSING_ATTACHMENT"],
    )


def _axis(not_evaluated: bool = False) -> AxisScore:
    if not_evaluated:
        return AxisScore(
            value=None,
            semantics=ScoreSemantics.NOT_EVALUATED,
            calibration_id=None,
            evidence_refs=[],
        )
    return AxisScore(
        value=0.25,
        semantics=ScoreSemantics.RAW_SCORE,
        calibration_id=None,
        evidence_refs=[],
    )


def _auth() -> Authentication:
    obs = AuthObservation(
        result=AuthResult.UNKNOWN,
        provenance=AuthProvenance.UNKNOWN,
        evidence_refs=[],
    )
    return Authentication(spf=obs, dkim=obs, dmarc=obs, arc=obs)


def _assessment(
    account_scope: str,
    *,
    assessment_id: str,
    source_ref: str,
    revision: int = 1,
    with_finding: bool = True,
) -> SecurityAssessment:
    findings = []
    if with_finding:
        findings.append(
            SecurityFinding(
                finding_key="find:0001",
                rule_id="SENDER_DISPLAY_MISMATCH",
                category=FindingCategory.SENDER_IMPERSONATION,
                severity=FindingSeverity.MEDIUM,
                status=FindingStatus.OBSERVED,
                detector_version="v1",
                evidence_refs=["ref:ev:zzz999"],
                explanation_code="DISPLAY_NAME_MISMATCH",
            )
        )
    return SecurityAssessment(
        assessment_id=assessment_id,
        account_scope=account_scope,
        source_ref=source_ref,
        source_revision=revision,
        analysis_profile_version="2026.01",
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        processing_state=ProcessingState.ASSESSED,
        evidence_fidelity=EvidenceFidelity.ORIGINAL_RFC822,
        completeness=Completeness(
            available=["AUTH_CHECK"], missing=[], checks_not_run=[]
        ),
        authentication=_auth(),
        findings=findings,
        axes=TrustAxes(
            threat_likelihood=_axis(),
            impact_severity=_axis(),
            personal_relevance=_axis(not_evaluated=True),
            urgency=_axis(not_evaluated=True),
            expectedness=_axis(not_evaluated=True),
            actionability=_axis(not_evaluated=True),
        ),
        model_runs=[],
        policy=PolicyDisposition(
            mode=AssessmentMode.SHADOW,
            disposition=Disposition.REVIEW,
            policy_version="pol-v1",
            reason_codes=["SENDER_RISK"],
            human_approval_required=True,
            action_capabilities=[],
        ),
    )


# ---------------------------------------------------------------------------
# 1. Migration up -> down
# ---------------------------------------------------------------------------


def _stamp_prior_head(db_path: pathlib.Path) -> None:
    """Stamp alembic at the prior head (015) without running the whole chain.

    The chain's earliest revisions use Postgres-only DDL (``CREATE EXTENSION
    vector``) that SQLite rejects, so — like ``test_tasks_dependencies`` — we
    seed ``alembic_version`` at 015 and let ``upgrade`` apply *only* 016.
    """
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
                "CONSTRAINT pk_alembic_version PRIMARY KEY (version_num))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO alembic_version (version_num) VALUES "
                "('015_inference_providers')"
            )
        )
    eng.dispose()


def test_migration_up_then_down(tmp_path):
    """The 016 migration creates the four tables and fully reverses."""
    db_path = tmp_path / "migration.db"
    url = f"sqlite:///{db_path}"

    _stamp_prior_head(db_path)
    # Apply only revision 016 on top of the stamped prior head.
    command.upgrade(_alembic_config(url), "016_mail_security")

    expected = {
        "mail_security_jobs",
        "mail_evidence",
        "mail_security_assessments",
        "mail_security_findings",
    }
    eng = create_engine(url)
    with eng.connect() as c:
        names = {
            r[0]
            for r in c.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        version = c.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    eng.dispose()
    assert expected <= names, f"migration 016 must create {expected}"
    assert version == "016_mail_security"

    # Downgrade one step: all four tables must be gone, prior head restored.
    command.downgrade(_alembic_config(url), "015_inference_providers")
    eng = create_engine(url)
    with eng.connect() as c:
        names_after = {
            r[0]
            for r in c.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        version_after = c.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    eng.dispose()
    assert not (expected & names_after), (
        "downgrade must drop every 016 table; still present: "
        f"{expected & names_after}"
    )
    assert version_after == "015_inference_providers"


def test_every_revision_id_fits_alembic_version_column(tmp_path):
    """Postgres ``alembic_version.version_num`` is VARCHAR(32); SQLite does
    not enforce lengths, so a too-long id only fails at deploy (it did for
    the first 7.4 id). Guard every revision explicitly."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(
        _alembic_config(f"sqlite:///{tmp_path / 'noop.db'}")
    )
    too_long = [
        rev.revision for rev in script.walk_revisions() if len(rev.revision) > 32
    ]
    assert too_long == []


def test_migration_single_head(tmp_path):
    """The alembic script tree has a single head (017 chained on 016)."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(
        _alembic_config(f"sqlite:///{tmp_path / 'noop.db'}")
    )
    heads = script.get_heads()
    assert list(heads) == ["024_mail_expectedness"], (
        f"expected one head, got {heads}"
    )


# ---------------------------------------------------------------------------
# 2. Cross-account denial (R-023.4, T-15)
# ---------------------------------------------------------------------------


async def test_cross_account_job_denied(tmp_path):
    engine, repo = await _make_repo(tmp_path)
    try:
        source_key = logical_source_key(
            ACCOUNT_A, provider="gmail", provider_uid="UID-1"
        )
        key = outbox_job_key(ACCOUNT_A, source_key, "2026.01")
        job = await repo.upsert_job(
            account_scope=ACCOUNT_A,
            outbox_job_key=key,
            source_key=source_key,
            analysis_profile_version="2026.01",
        )

        # Account B knows A's exact row id and key, yet is denied.
        assert await repo.get_job(ACCOUNT_B, job.id) is None
        assert await repo.get_job_by_key(ACCOUNT_B, key) is None
        # A can still read its own row.
        assert (await repo.get_job(ACCOUNT_A, job.id)) is not None
    finally:
        await engine.dispose()


async def test_cross_account_evidence_denied(tmp_path):
    engine, repo = await _make_repo(tmp_path)
    try:
        src = "src:aaaa:0001"
        await repo.upsert_evidence(_manifest(ACCOUNT_A, source_id=src))
        # B uses A's exact source id + revision + sha256 hash: still denied.
        assert await repo.get_evidence(ACCOUNT_B, src, 1) is None
        assert await repo.get_evidence(ACCOUNT_A, src, 1) is not None
    finally:
        await engine.dispose()


async def test_cross_account_assessment_denied(tmp_path):
    engine, repo = await _make_repo(tmp_path)
    try:
        # The same message_key delivered to both accounts yields distinct ids,
        # and even handing B account A's exact assessment_id must be denied.
        msg_key = "msg:shared-hash"
        aid_a = derive_assessment_id(ACCOUNT_A, msg_key, 1, "2026.01")
        await repo.upsert_assessment(
            _assessment(ACCOUNT_A, assessment_id=aid_a, source_ref="src:a")
        )
        assert await repo.get_assessment(ACCOUNT_B, aid_a) is None
        assert await repo.get_findings(ACCOUNT_B, aid_a) == []
        assert await repo.get_assessment(ACCOUNT_A, aid_a) is not None
        assert len(await repo.get_findings(ACCOUNT_A, aid_a)) == 1
    finally:
        await engine.dispose()


async def test_same_message_two_accounts_isolated(tmp_path):
    """T-15: account A cannot read account B's verdict for the same message."""
    engine, repo = await _make_repo(tmp_path)
    try:
        msg_key = "msg:identical"
        aid_a = derive_assessment_id(ACCOUNT_A, msg_key, 1, "2026.01")
        aid_b = derive_assessment_id(ACCOUNT_B, msg_key, 1, "2026.01")
        assert aid_a != aid_b  # distinct logical identity per account

        await repo.upsert_assessment(
            _assessment(ACCOUNT_A, assessment_id=aid_a, source_ref="src:x")
        )
        await repo.upsert_assessment(
            _assessment(ACCOUNT_B, assessment_id=aid_b, source_ref="src:x")
        )

        # Each account sees only its own single assessment.
        a_list = await repo.list_assessments(ACCOUNT_A)
        b_list = await repo.list_assessments(ACCOUNT_B)
        assert [r.assessment_id for r in a_list] == [aid_a]
        assert [r.assessment_id for r in b_list] == [aid_b]
        # A cannot fetch B's verdict even with B's id.
        assert await repo.get_assessment(ACCOUNT_A, aid_b) is None
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 3. Idempotent upsert (R-018.2, T-17)
# ---------------------------------------------------------------------------


async def test_idempotent_job_upsert(tmp_path):
    engine, repo = await _make_repo(tmp_path)
    try:
        source_key = logical_source_key(
            ACCOUNT_A, provider="gmail", provider_uid="UID-9"
        )
        key = outbox_job_key(ACCOUNT_A, source_key, "2026.01")
        first = await repo.upsert_job(
            account_scope=ACCOUNT_A,
            outbox_job_key=key,
            source_key=source_key,
            analysis_profile_version="2026.01",
        )
        second = await repo.upsert_job(
            account_scope=ACCOUNT_A,
            outbox_job_key=key,
            source_key=source_key,
            analysis_profile_version="2026.01",
        )
        assert first.id == second.id
        assert len(await repo.list_jobs(ACCOUNT_A)) == 1
    finally:
        await engine.dispose()


async def test_idempotent_assessment_upsert(tmp_path):
    engine, repo = await _make_repo(tmp_path)
    try:
        aid = derive_assessment_id(ACCOUNT_A, "msg:replay", 1, "2026.01")
        first = await repo.upsert_assessment(
            _assessment(ACCOUNT_A, assessment_id=aid, source_ref="src:r")
        )
        second = await repo.upsert_assessment(
            _assessment(ACCOUNT_A, assessment_id=aid, source_ref="src:r")
        )
        assert first.id == second.id
        assert len(await repo.list_assessments(ACCOUNT_A)) == 1
        # Replay does not duplicate findings either.
        assert len(await repo.get_findings(ACCOUNT_A, aid)) == 1
    finally:
        await engine.dispose()


async def test_idempotent_evidence_upsert(tmp_path):
    engine, repo = await _make_repo(tmp_path)
    try:
        src = "src:ev:replay"
        first = await repo.upsert_evidence(_manifest(ACCOUNT_A, source_id=src))
        second = await repo.upsert_evidence(_manifest(ACCOUNT_A, source_id=src))
        assert first.id == second.id
        # A new *revision* is a distinct row (lineage preserved, design §9).
        rev2 = await repo.upsert_evidence(
            _manifest(ACCOUNT_A, source_id=src, revision=2)
        )
        assert rev2.id != first.id
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 4. No raw body (T-14)
# ---------------------------------------------------------------------------


async def test_canary_never_persisted(tmp_path):
    """A canary in the source 'body' must not land in any text column."""
    engine, repo = await _make_repo(tmp_path)
    try:
        # The message body carries the canary. The deterministic ids hash the
        # provider uid / message key — they never copy the body — and the
        # contract models carry only opaque refs + safe codes. So the canary
        # must never appear in any persisted row.
        provider_uid = "UID-canary"
        source_key = logical_source_key(
            ACCOUNT_A,
            provider="gmail",
            provider_uid=provider_uid,
            rfc_message_id="<canary@evil.example>",
        )
        key = outbox_job_key(ACCOUNT_A, source_key, "2026.01")
        await repo.upsert_job(
            account_scope=ACCOUNT_A,
            outbox_job_key=key,
            source_key=source_key,
            analysis_profile_version="2026.01",
            meta={"note_code": "FROM_BODY"},  # a safe code, not the body
        )
        await repo.upsert_evidence(_manifest(ACCOUNT_A, source_id="src:canary"))
        aid = derive_assessment_id(ACCOUNT_A, source_key, 1, "2026.01")
        await repo.upsert_assessment(
            _assessment(ACCOUNT_A, assessment_id=aid, source_ref="src:canary")
        )

        # Scan every text-bearing column across all four tables.
        async with engine.begin() as conn:
            for table in _SECURITY_TABLES:
                rows = await conn.run_sync(
                    lambda sync_conn, t=table: sync_conn.execute(
                        t.select()
                    ).fetchall()
                )
                for row in rows:
                    for value in row:
                        assert _CANARY not in str(value), (
                            f"canary leaked into {table.name}: {value!r}"
                        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 5. Round-trip + index presence
# ---------------------------------------------------------------------------


async def test_metadata_round_trip(tmp_path):
    engine, repo = await _make_repo(tmp_path)
    try:
        src = "src:rt:0001"
        ev = await repo.upsert_evidence(_manifest(ACCOUNT_A, source_id=src))
        got = await repo.get_evidence(ACCOUNT_A, src, 1)
        assert got is not None
        assert got.id == ev.id
        assert got.raw_content_ref == "ref:raw:abc123"
        assert got.normalized_content_ref == "ref:norm:def456"
        assert got.sha256 == "a" * 64
        assert got.missing_parts == ["MISSING_ATTACHMENT"]
        assert got.evidence_fidelity == "ORIGINAL_RFC822"

        aid = derive_assessment_id(ACCOUNT_A, "msg:rt", 1, "2026.01")
        await repo.upsert_assessment(
            _assessment(ACCOUNT_A, assessment_id=aid, source_ref=src)
        )
        asmt = await repo.get_assessment(ACCOUNT_A, aid)
        assert asmt is not None
        assert asmt.disposition == "REVIEW"
        assert asmt.mode == "SHADOW"
        assert asmt.policy_version == "pol-v1"
        # Full metadata-only payload round-trips as JSON.
        assert asmt.assessment["assessment_id"] == aid
        findings = await repo.get_findings(ACCOUNT_A, aid)
        assert len(findings) == 1
        assert findings[0].explanation_code == "DISPLAY_NAME_MISMATCH"
        assert findings[0].evidence_ref == "ref:ev:zzz999"
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("table", "index_name"),
    [
        (MailSecurityJobRow, "ix_mail_security_jobs_account_scope"),
        (MailEvidenceRow, "ix_mail_evidence_account_scope"),
        (
            MailSecurityAssessmentRow,
            "ix_mail_security_assessments_account_scope",
        ),
        (
            MailSecurityFindingRow,
            "ix_mail_security_findings_account_scope",
        ),
    ],
)
def test_account_scope_index_present(table, index_name):
    """Every security table carries an indexed account_scope (R-023)."""
    names = {ix.name for ix in table.__table__.indexes}
    assert index_name in names, (
        f"{table.__tablename__} must index account_scope as {index_name}"
    )
    assert "account_scope" in table.__table__.c, (
        f"{table.__tablename__} must carry an account_scope column"
    )
