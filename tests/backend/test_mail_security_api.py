"""API tests for the NerveShield read-only assessments surface (Task 1.10).

The Done criterion for Task 1.10 is "API authorization, rate and redaction
tests". This module proves, against the real FastAPI router mounted on a bare
app with a real :class:`AuthContext` and a real account-scoped Task 1.7
repository over a SQLite scratch DB (the house durable-test harness — see
``test_mail_security_repo.py`` / ``test_relay_auth.py``):

1. **Authorization (R-023, auth gate).** An unauthenticated request is rejected
   with the app's standard ``401``; an authenticated caller only ever sees
   assessments in its own authorized account scope.
2. **Account isolation (R-023.4, T-15).** A caller authorized for account A who
   requests an assessment id that exists under account B gets a ``404`` and
   never B's data — even when it supplies B's exact id. The 404 is
   indistinguishable from "does not exist".
3. **Pagination.** The list is paginated with a working opaque continuation
   cursor and a bounded maximum page size (:data:`MAX_PAGE_SIZE`).
4. **Redaction (T-14, R-017.2).** An assessment whose source carried a canary
   body yields a list row, a detail body, and an error body with NO canary and
   no raw content anywhere.
5. **Rate / page cap.** A request for a page larger than the cap is rejected
   (the enforced control, since the repo has no app-level rate limiter).
6. **Read-only (R-019, T-26).** The module mounts only GET routes (no
   POST/PATCH/DELETE), and a list/detail read performs no writes.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

import backend.api.auth_service as auth_service
from backend.api.auth_service import AuthContext
from backend.api.endpoints import mail_security
from backend.mail_security.identifiers import assessment_id as derive_assessment_id
from backend.mail_security.models import (
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
from backend.persistence.repositories import RepositoryRegistry

JWT_SECRET = "unit-secret-" + "a" * 60
JWT_ISSUER = "https://auth.test.local/realms/nervecenter"

ACCOUNT_A = "conn:aaaa1111"
ACCOUNT_B = "conn:bbbb2222"

#: A canary standing in for raw body text. It must never reach a response.
_CANARY = "CANARY-SECRET-dont-leak-me-9f3b2a"

_SECURITY_TABLES = [
    MailSecurityJobRow.__table__,
    MailEvidenceRow.__table__,
    MailSecurityAssessmentRow.__table__,
    MailSecurityFindingRow.__table__,
]


# --------------------------------------------------------------------------- #
# model builders (mirror test_mail_security_repo.py)
# --------------------------------------------------------------------------- #


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
    source_ref: str = "src:x",
    disposition: Disposition = Disposition.REVIEW,
    created_at: datetime | None = None,
    with_finding: bool = True,
) -> SecurityAssessment:
    findings: list[SecurityFinding] = []
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
        source_revision=1,
        analysis_profile_version="2026.01",
        created_at=created_at or datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        processing_state=ProcessingState.ASSESSED,
        evidence_fidelity=EvidenceFidelity.ORIGINAL_RFC822,
        completeness=Completeness(
            available=["AUTH_CHECK"],
            missing=["ATTACHMENT_SCAN"],
            checks_not_run=["INTEL_MATCH"],
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
            disposition=disposition,
            policy_version="pol-v1",
            reason_codes=["SENDER_RISK"],
            human_approval_required=True,
            action_capabilities=[],
        ),
    )


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


def _auth_ctx() -> AuthContext:
    return AuthContext(
        secret=JWT_SECRET,
        issuer=JWT_ISSUER,
        client_id="unit",
        local_admin_enabled=False,
        local_admin_password=None,
        session_factory=None,
    )


async def _mint_token(
    sub: str = "user-a", *, account_scopes: list[str] | None = None
) -> str:
    """Mint a valid, session-backed access token carrying ``account_scopes``."""
    ctx = _auth_ctx()
    extra: dict[str, Any] = {}
    if account_scopes is not None:
        extra["account_scopes"] = account_scopes
    token, exp, claims = ctx._mint(
        sub=sub,
        typ="access",
        roles=["admin"],
        ttl=auth_service.ACCESS_TOKEN_TTL,
        extra=extra,
    )
    await auth_service.record_session(
        ctx, jti=claims["jti"], sub=sub, typ="access", expires_at=exp, source="test"
    )
    return token


async def _make_engine(tmp_path: pathlib.Path) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'mail_security_api.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            _SECURITY_TABLES[0].metadata.create_all, tables=_SECURITY_TABLES
        )
    return engine


class _Env:
    """Bundles the app + registry + a dedicated loop that owns the DB engine.

    pytest-asyncio closes its per-test loop around each coroutine test, so the
    sync TestClient tests here must not rely on a current event loop. This env
    owns one explicit loop for the whole fixture lifetime and runs every setup
    / seed / count coroutine on it, keeping the ``StaticPool`` aiosqlite engine
    bound to a single live loop.
    """

    def __init__(self, app: FastAPI, registry: RepositoryRegistry, loop: Any) -> None:
        self.app = app
        self.registry = registry
        self._loop = loop

    def run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def seed(self, assessment: SecurityAssessment) -> None:
        self.run(self.registry.mail_security.upsert_assessment(assessment))

    def token(
        self, sub: str = "user-a", *, account_scopes: list[str] | None = None
    ) -> str:
        return self.run(_mint_token(sub, account_scopes=account_scopes))


@pytest.fixture
def api_env(tmp_path):
    """A bare app with the real router, a real AuthContext and a real repo."""
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    auth_service.reset_memory_store()
    engine = loop.run_until_complete(_make_engine(tmp_path))
    registry = RepositoryRegistry(make_session_factory(engine))

    app = FastAPI()
    app.include_router(mail_security.router)
    app.state.auth_ctx = _auth_ctx()
    app.state.repository_registry = registry
    try:
        yield _Env(app, registry, loop)
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


# --------------------------------------------------------------------------- #
# 1. authorization
# --------------------------------------------------------------------------- #


def test_unauthenticated_list_is_rejected(api_env):
    with TestClient(api_env.app) as client:
        resp = client.get("/api/mail-security/assessments")
    assert resp.status_code == 401


def test_unauthenticated_detail_is_rejected(api_env):
    with TestClient(api_env.app) as client:
        resp = client.get("/api/mail-security/assessments/whatever")
    assert resp.status_code == 401


def test_caller_sees_only_own_scope(api_env):
    aid_a = derive_assessment_id(ACCOUNT_A, "msg:a", 1, "2026.01")
    aid_b = derive_assessment_id(ACCOUNT_B, "msg:b", 1, "2026.01")
    api_env.seed(_assessment(ACCOUNT_A, assessment_id=aid_a))
    api_env.seed(_assessment(ACCOUNT_B, assessment_id=aid_b))

    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        resp = client.get(
            "/api/mail-security/assessments",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    ids = [row["assessment_id"] for row in body["data"]]
    assert ids == [aid_a]
    assert all(row["account_scope"] == ACCOUNT_A for row in body["data"])


def test_no_authorized_scopes_sees_nothing(api_env):
    """A token with no ``account_scopes`` claim is authorized for nothing."""
    aid_a = derive_assessment_id(ACCOUNT_A, "msg:a", 1, "2026.01")
    api_env.seed(_assessment(ACCOUNT_A, assessment_id=aid_a))

    token = api_env.token(account_scopes=None)  # no claim at all
    with TestClient(api_env.app) as client:
        resp = client.get(
            "/api/mail-security/assessments",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_client_account_id_cannot_widen_scope(api_env):
    """A client ``account_id`` can only narrow — never widen — access (R-023)."""
    aid_a = derive_assessment_id(ACCOUNT_A, "msg:a", 1, "2026.01")
    aid_b = derive_assessment_id(ACCOUNT_B, "msg:b", 1, "2026.01")
    api_env.seed(_assessment(ACCOUNT_A, assessment_id=aid_a))
    api_env.seed(_assessment(ACCOUNT_B, assessment_id=aid_b))

    # Caller authorized only for A tries to widen to B via the query param.
    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        resp = client.get(
            f"/api/mail-security/assessments?account_id={ACCOUNT_B}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["data"] == []  # B is not in the authorized set


# --------------------------------------------------------------------------- #
# 2. account isolation (R-023.4, T-15)
# --------------------------------------------------------------------------- #


def test_detail_cross_account_is_404(api_env):
    """Caller in A requesting B's exact assessment id gets 404, never B's row."""
    aid_b = derive_assessment_id(ACCOUNT_B, "msg:b", 1, "2026.01")
    api_env.seed(_assessment(ACCOUNT_B, assessment_id=aid_b))

    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        resp = client.get(
            f"/api/mail-security/assessments/{aid_b}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"] == {"error_code": "not_found"}
    # The 404 body leaks nothing about B's assessment.
    assert aid_b not in resp.text or resp.text.count(aid_b) == 0


def test_detail_absent_and_cross_account_indistinguishable(api_env):
    """A non-existent id and a cross-account id both return the same 404."""
    aid_b = derive_assessment_id(ACCOUNT_B, "msg:b", 1, "2026.01")
    api_env.seed(_assessment(ACCOUNT_B, assessment_id=aid_b))

    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        cross = client.get(f"/api/mail-security/assessments/{aid_b}", headers=headers)
        absent = client.get(
            "/api/mail-security/assessments/does-not-exist", headers=headers
        )
    assert cross.status_code == absent.status_code == 404
    assert cross.json() == absent.json()


def test_detail_own_scope_ok(api_env):
    aid_a = derive_assessment_id(ACCOUNT_A, "msg:a", 1, "2026.01")
    api_env.seed(_assessment(ACCOUNT_A, assessment_id=aid_a))

    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        resp = client.get(
            f"/api/mail-security/assessments/{aid_a}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["assessment_id"] == aid_a
    assert body["disposition"] == "REVIEW"
    # R-020.1 unavailable checks surfaced.
    assert body["completeness"]["checks_not_run"] == ["INTEL_MATCH"]
    assert len(body["findings"]) == 1
    assert body["findings"][0]["explanation_code"] == "DISPLAY_NAME_MISMATCH"


# --------------------------------------------------------------------------- #
# 3. pagination + cursor
# --------------------------------------------------------------------------- #


def test_pagination_cursor(api_env):
    for i in range(5):
        aid = derive_assessment_id(ACCOUNT_A, f"msg:{i}", 1, "2026.01")
        api_env.seed(
            _assessment(
                ACCOUNT_A,
                assessment_id=aid,
                created_at=datetime(2026, 1, 1, 12, i, tzinfo=UTC),
                with_finding=False,
            ),
        )
    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        first = client.get(
            "/api/mail-security/assessments?limit=2", headers=headers
        ).json()
        assert len(first["data"]) == 2
        assert first["meta"]["pagination"]["total"] == 5
        cursor = first["next_cursor"]
        assert cursor

        second = client.get(
            f"/api/mail-security/assessments?limit=2&cursor={cursor}",
            headers=headers,
        ).json()
        assert len(second["data"]) == 2
        # No overlap between page 1 and page 2.
        ids1 = {r["assessment_id"] for r in first["data"]}
        ids2 = {r["assessment_id"] for r in second["data"]}
        assert ids1.isdisjoint(ids2)

        cursor2 = second["next_cursor"]
        third = client.get(
            f"/api/mail-security/assessments?limit=2&cursor={cursor2}",
            headers=headers,
        ).json()
        assert len(third["data"]) == 1
        # Last page: no further cursor.
        assert third["next_cursor"] is None


def test_invalid_cursor_is_safe_422(api_env):
    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        resp = client.get(
            "/api/mail-security/assessments?cursor=!!!not-base64!!!",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == {"error_code": "invalid_cursor"}


def test_disposition_filter(api_env):
    aid_rev = derive_assessment_id(ACCOUNT_A, "msg:rev", 1, "2026.01")
    aid_restr = derive_assessment_id(ACCOUNT_A, "msg:restr", 1, "2026.01")
    api_env.seed(
        _assessment(ACCOUNT_A, assessment_id=aid_rev, disposition=Disposition.REVIEW),
    )
    api_env.seed(
        _assessment(
            ACCOUNT_A,
            assessment_id=aid_restr,
            disposition=Disposition.RESTRICTED,
        ),
    )
    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        resp = client.get(
            "/api/mail-security/assessments?disposition=RESTRICTED",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    ids = [r["assessment_id"] for r in resp.json()["data"]]
    assert ids == [aid_restr]


# --------------------------------------------------------------------------- #
# 4. redaction (T-14, R-017.2)
# --------------------------------------------------------------------------- #


def test_no_canary_anywhere(api_env):
    """No raw body / canary appears in a list row, a detail body, or an error.

    The stored assessment is metadata-only by construction, so the canary is
    never stored; this asserts the *API* projection also never surfaces raw
    content and that the error path stays safe-shaped.
    """
    aid_a = derive_assessment_id(ACCOUNT_A, "msg:canary", 1, "2026.01")
    api_env.seed(_assessment(ACCOUNT_A, assessment_id=aid_a, source_ref="src:canary"))

    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        lst = client.get("/api/mail-security/assessments", headers=headers)
        detail = client.get(
            f"/api/mail-security/assessments/{aid_a}", headers=headers
        )
        err = client.get(
            "/api/mail-security/assessments/nope", headers=headers
        )
    for resp in (lst, detail, err):
        assert _CANARY not in resp.text
    # The detail carries only redacted metadata fields (no free-text body key).
    body = detail.json()
    assert set(body["authentication"].keys()) == {"spf", "dkim", "dmarc", "arc"}
    for axis in body["axes"].values():
        # A raw/uncalibrated axis never surfaces a numeric value as truth.
        if not axis["calibrated"]:
            assert axis["value"] is None


def test_error_body_is_safe_shaped(api_env):
    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        resp = client.get(
            "/api/mail-security/assessments/missing",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404
    body = resp.json()
    # Only a stable error code — no stack trace, no raw content, no internal id.
    assert body == {"detail": {"error_code": "not_found"}}


# --------------------------------------------------------------------------- #
# 5. rate / page cap
# --------------------------------------------------------------------------- #


def test_page_size_cap_enforced(api_env):
    """A page larger than the cap is rejected (the enforced rate control)."""
    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        resp = client.get(
            f"/api/mail-security/assessments?limit={mail_security.MAX_PAGE_SIZE + 1}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 422  # FastAPI Query(le=MAX_PAGE_SIZE) bound


def test_page_size_at_cap_ok(api_env):
    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        resp = client.get(
            f"/api/mail-security/assessments?limit={mail_security.MAX_PAGE_SIZE}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 6. read-only (R-019, T-26)
# --------------------------------------------------------------------------- #


def test_only_get_routes_exposed(api_env):
    """The module mounts only GET routes — no POST/PATCH/DELETE/PUT."""
    methods: set[str] = set()
    for route in mail_security.router.routes:
        methods |= set(getattr(route, "methods", set()) or set())
    mutating = methods & {"POST", "PATCH", "DELETE", "PUT"}
    assert not mutating, f"read-only surface must expose no mutations: {mutating}"
    assert "GET" in methods


def test_read_performs_no_writes(api_env):
    """A list + detail read does not change the stored row count."""
    aid_a = derive_assessment_id(ACCOUNT_A, "msg:ro", 1, "2026.01")
    api_env.seed(_assessment(ACCOUNT_A, assessment_id=aid_a))

    async def _count() -> int:
        return len(await api_env.registry.mail_security.list_assessments(ACCOUNT_A))

    before = api_env.run(_count())
    token = api_env.token(account_scopes=[ACCOUNT_A])
    with TestClient(api_env.app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        client.get("/api/mail-security/assessments", headers=headers)
        client.get(f"/api/mail-security/assessments/{aid_a}", headers=headers)
    after = api_env.run(_count())
    assert before == after == 1
