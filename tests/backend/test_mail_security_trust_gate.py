"""Task 7.4: pre-promotion trust gate in the legacy promotion flow.

Done criterion: malicious email cannot auto-mint an actionable non-review
obligation, safe mail still can, no duplicate/replayed tasks. Proves, with
the real CommunicationPipeline, DetectionService and SQLite repositories:

- OFF (default): no gate is bound and the durable attention row is written
  at ingest exactly as before; non-mail refs are never gated;
- ON + RESTRICTED verdict: no ``attention_items`` row, the in-memory item is
  suppressed, the hold becomes a REVIEW_CANDIDATE, and Today stays empty;
- ON + SAFE verdict: the release writes the *same* id and columns the ungated
  path writes;
- replays (same process and after a restart) never duplicate holds or rows;
- no verdict keeps the hold (fail closed); another scope's verdict never
  releases it; a hold failure never falls back to an actionable write;
- held commitments behave the same way.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_mail_security_api import _SECURITY_TABLES, _assessment

from backend.api.app import create_app  # noqa: F401 - import-order guard
from backend.attention.service import AttentionService
from backend.communications.detection.service import DetectionService
from backend.communications.detection.wiring import CommunicationPipeline
from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.followups.service import FollowUpService
from backend.mail_security import pipeline_runner as pr
from backend.mail_security.ingestion_seam import ENV_MAIL_SECURITY_ENABLED
from backend.mail_security.models import Disposition
from backend.mail_security.trust_gate import (
    ENV_PROMOTION_GATE,
    GateMode,
    MailPromotionGate,
    gate_mode,
)
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailCampaignBandRow,
    MailCampaignMemberRow,
    MailSecurityFeedbackRow,
    MailSecurityHeldPromotionRow,
    MailThreadLedgerRow,
)
from backend.persistence.models.personal import (
    AttentionItemRow,
    CommitmentRow,
    CommunicationRow,
    ConnectionRow,
    FollowupRow,
    PersonRow,
    PreferenceRow,
)
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.mail_security_gate_repo import (
    HeldPromotionRepository,
    release_pass,
)

NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
CONN = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
SCOPE, OTHER_SCOPE = f"conn:{CONN}", f"conn:{OTHER}"
PERSON = "person:dana"
REQUEST = "Can you send me the report."
COMMIT = "I will send you the deck by Friday."

_TABLES = (
    *_SECURITY_TABLES,
    MailSecurityHeldPromotionRow.__table__,
    MailThreadLedgerRow.__table__,
    MailCampaignMemberRow.__table__,
    MailCampaignBandRow.__table__,
    MailSecurityFeedbackRow.__table__,
    PersonRow.__table__,
    CommitmentRow.__table__,
    CommunicationRow.__table__,
    FollowupRow.__table__,
    AttentionItemRow.__table__,
    ConnectionRow.__table__,
    PreferenceRow.__table__,
)


def _mail_ref(conn: str, mail_id: str) -> tuple[str, str]:
    d = hashlib.sha256(f"{conn}:{mail_id}".encode()).hexdigest()[:24]
    return f"comm-mail-{d}", f"content://mail/{conn}/{d}"


class Env:
    def __init__(self, registry: RepositoryRegistry) -> None:
        self.registry = registry
        self.holds = HeldPromotionRepository(registry.session_factory)

    def pipeline(self, *, gated: bool, gate: Any | None = None) -> CommunicationPipeline:
        attention = AttentionService()
        return CommunicationPipeline(
            detection=DetectionService(attention=attention),
            followups=FollowUpService(attention=attention),
            attention=attention,
            attention_store=self.registry.attention,
            commitment_store=self.registry.commitments,
            promotion_gate=(gate or MailPromotionGate(self.holds)) if gated else None,
        )

    async def count(self, model: Any) -> int:
        async with self.registry.session_factory() as s:
            return int((await s.execute(select(func.count()).select_from(model))).scalar())

    async def holds_rows(self) -> list[Any]:
        async with self.registry.session_factory() as s:
            return list((await s.execute(select(MailSecurityHeldPromotionRow))).scalars().all())

    async def verdict(
        self, scope: str, ref: str, aid: str, disposition: Disposition, *, revision: int = 1
    ) -> None:
        key = f"src:{ref[-24:]}"
        await self.registry.mail_security.upsert_job(
            account_scope=scope,
            outbox_job_key=f"job:{scope}:{key}",
            source_key=key,
            analysis_profile_version="2026.01",
            meta={"content_ref": ref},
        )
        a = _assessment(scope, assessment_id=aid, source_ref=key, disposition=disposition)
        await self.registry.mail_security.upsert_assessment(
            a.model_copy(update={"source_revision": revision})
        )

    async def release(self) -> dict[str, int]:
        return await release_pass(
            self.holds,
            attention=self.registry.attention,
            commitments=self.registry.commitments,
            now=NOW,
        )


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'gate.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PersonRow.metadata.create_all, tables=list(_TABLES))
    registry = RepositoryRegistry(make_session_factory(engine))
    await registry.communications.create_person(id=PERSON, display_name="Dana")
    yield Env(registry)
    await engine.dispose()


def _comm(
    ref: str,
    comm_id: str,
    *,
    direction: CommunicationDirection = CommunicationDirection.INCOMING,
) -> Communication:
    return Communication(
        communication_id=comm_id,
        channel=CommunicationChannel.EMAIL,
        direction=direction,
        sender="dana@example.com",
        person_id=PERSON,
        timestamp=NOW,
        content_ref=ref,
    )


async def _ingest(p: CommunicationPipeline, comm: Communication, text: str = REQUEST) -> Any:
    p.detection.register_content(comm.content_ref, text)
    result = p.ingest(comm, now=NOW)
    assert await p.flush_durable() is None
    return result


async def _active(env: Env) -> list[Any]:
    return await env.registry.attention.list_attention_items(state="active")


# -- mode ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_map", "mode"),
    [
        ({}, GateMode.OFF),
        ({ENV_PROMOTION_GATE: "review_gated"}, GateMode.OFF),  # mail security OFF
        ({ENV_PROMOTION_GATE: "enforce", ENV_MAIL_SECURITY_ENABLED: "1"}, GateMode.OFF),
        ({ENV_PROMOTION_GATE: "1", ENV_MAIL_SECURITY_ENABLED: "1"}, GateMode.OFF),
        (
            {ENV_PROMOTION_GATE: " Review_Gated ", ENV_MAIL_SECURITY_ENABLED: "1"},
            GateMode.REVIEW_GATED,
        ),
    ],
)
def test_gate_mode_is_off_unless_exactly_opted_in(env_map, mode):
    assert gate_mode(env_map) is mode


def test_default_pipeline_has_no_gate():
    from backend.communications.detection.wiring import get_pipeline, reset_pipeline

    reset_pipeline()
    try:
        assert get_pipeline()._promotion_gate is None
    finally:
        reset_pipeline()


# -- OFF parity + bypass --------------------------------------------------------------


async def test_off_writes_actionable_row_at_ingest(env):
    comm_id, ref = _mail_ref(CONN, "m-off")
    await _ingest(env.pipeline(gated=False), _comm(ref, comm_id))
    assert len(await _active(env)) == 1
    assert await env.count(MailSecurityHeldPromotionRow) == 0


async def test_non_mail_refs_are_not_gated(env):
    await _ingest(env.pipeline(gated=True), _comm("enc://imessage/1", "message:1"))
    assert len(await _active(env)) == 1
    assert await env.count(MailSecurityHeldPromotionRow) == 0


# -- malicious ----------------------------------------------------------------------------


async def test_restricted_mail_cannot_mint_actionable_attention(env):
    comm_id, ref = _mail_ref(CONN, "m-bad")
    p = env.pipeline(gated=True)
    result = await _ingest(p, _comm(ref, comm_id))
    assert result.finding_ids, "detector must still detect"
    assert await _active(env) == []
    # The in-memory queue (/api/attention) shows nothing live either.
    in_memory = p.attention.list()
    assert in_memory, "detection must have fed the in-memory queue"
    assert all(i.status.value == "SUPPRESSED" for i in in_memory)
    (hold,) = await env.holds_rows()
    assert (hold.status, hold.target_kind, hold.account_scope) == ("HELD", "attention", SCOPE)

    await env.verdict(SCOPE, ref, "asm:bad", Disposition.RESTRICTED)
    assert await env.release() == {"review_candidate": 1}
    assert await _active(env) == []
    (hold,) = await env.holds_rows()
    assert (hold.status, hold.decided_disposition) == ("REVIEW_CANDIDATE", "RESTRICTED")

    # Replay in-process and after a "restart" (fresh pipeline): no new hold,
    # no actionable row, status unchanged.
    await _ingest(p, _comm(ref, comm_id))
    await _ingest(env.pipeline(gated=True), _comm(ref, comm_id))
    assert await env.release() == {"unchanged": 1}
    assert await env.count(MailSecurityHeldPromotionRow) == 1
    assert await env.count(AttentionItemRow) == 0

    # Today (durable read path) stays empty.
    from backend.api.endpoints import today

    app = FastAPI()
    app.include_router(today.router)
    app.state.repository_registry = env.registry
    app.state._test_now = NOW
    with TestClient(app) as client:
        assert client.get("/api/today").json()["needs_you"] == []


# -- safe ----------------------------------------------------------------------------


async def test_safe_mail_releases_the_exact_ungated_row(env, tmp_path):
    comm_id, ref = _mail_ref(CONN, "m-ok")
    await _ingest(env.pipeline(gated=True), _comm(ref, comm_id))
    assert await _active(env) == []
    await env.verdict(SCOPE, ref, "asm:ok", Disposition.SAFE_TO_PROCESS)
    assert await env.release() == {"released": 1}
    (row,) = await _active(env)

    # Reference: the same message through the ungated path in a fresh DB.
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ref.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PersonRow.metadata.create_all, tables=list(_TABLES))
    ref_env = Env(RepositoryRegistry(make_session_factory(engine)))
    await _ingest(ref_env.pipeline(gated=False), _comm(ref, comm_id))
    (expected,) = await _active(ref_env)
    await engine.dispose()
    cols = (
        "id",
        "title",
        "importance",
        "urgency",
        "body",
        "source_refs",
        "recommended_action",
        "state",
        "resolution_condition",
    )
    assert {c: getattr(row, c) for c in cols} == {c: getattr(expected, c) for c in cols}

    # Replays never duplicate; a second release pass has nothing to do.
    await _ingest(env.pipeline(gated=True), _comm(ref, comm_id))
    assert await env.release() == {}
    assert await env.count(AttentionItemRow) == 1
    (hold,) = await env.holds_rows()
    assert hold.status == "RELEASED"


async def test_low_priority_also_releases(env):
    comm_id, ref = _mail_ref(CONN, "m-low")
    await _ingest(env.pipeline(gated=True), _comm(ref, comm_id))
    await env.verdict(SCOPE, ref, "asm:low", Disposition.LOW_PRIORITY)
    assert await env.release() == {"released": 1}
    assert len(await _active(env)) == 1


# -- fail closed / scope / reassessment ---------------------------------------------------------


async def test_no_verdict_keeps_the_hold(env):
    comm_id, ref = _mail_ref(CONN, "m-wait")
    await _ingest(env.pipeline(gated=True), _comm(ref, comm_id))
    assert await env.release() == {"waiting": 1}
    assert await _active(env) == []


@pytest.mark.parametrize("disposition", [Disposition.REVIEW, Disposition.INCOMPLETE])
async def test_review_and_incomplete_are_candidates(env, disposition):
    comm_id, ref = _mail_ref(CONN, f"m-{disposition.value}")
    await _ingest(env.pipeline(gated=True), _comm(ref, comm_id))
    await env.verdict(SCOPE, ref, "asm:x", disposition)
    assert await env.release() == {"review_candidate": 1}
    assert await _active(env) == []


async def test_another_scopes_verdict_never_releases(env):
    comm_id, ref = _mail_ref(CONN, "m-cross")
    await _ingest(env.pipeline(gated=True), _comm(ref, comm_id))
    await env.verdict(OTHER_SCOPE, ref, "asm:other", Disposition.SAFE_TO_PROCESS)
    assert await env.release() == {"waiting": 1}
    assert await _active(env) == []


async def test_reassessment_can_release_a_candidate(env):
    comm_id, ref = _mail_ref(CONN, "m-re")
    await _ingest(env.pipeline(gated=True), _comm(ref, comm_id))
    await env.verdict(SCOPE, ref, "asm:r1", Disposition.REVIEW, revision=1)
    assert await env.release() == {"review_candidate": 1}
    await env.verdict(SCOPE, ref, "asm:r2", Disposition.SAFE_TO_PROCESS, revision=2)
    assert await env.release() == {"released": 1}
    assert len(await _active(env)) == 1


async def test_hold_failure_never_falls_back_to_actionable(env):
    class Boom:
        def applies(self, communication: Any) -> bool:
            return True

        async def hold(self, **_: Any) -> None:
            raise RuntimeError("store down")

    comm_id, ref = _mail_ref(CONN, "m-boom")
    p = env.pipeline(gated=True, gate=Boom())
    p.detection.register_content(ref, REQUEST)
    p.ingest(_comm(ref, comm_id), now=NOW)  # never raises
    await p.flush_durable()
    assert await env.count(AttentionItemRow) == 0


async def test_unsure_gate_holds(env):
    class Unsure(MailPromotionGate):
        def applies(self, communication: Any) -> bool:
            raise RuntimeError("cannot tell")

    comm_id, ref = _mail_ref(CONN, "m-unsure")
    await _ingest(env.pipeline(gated=True, gate=Unsure(env.holds)), _comm(ref, comm_id))
    assert await env.count(AttentionItemRow) == 0
    assert await env.count(MailSecurityHeldPromotionRow) == 1


# -- commitments ---------------------------------------------------------------------------


async def test_outgoing_commitments_are_never_held(env):
    """NS-ADR-066: the owner's own sent mail is not an untrusted inbound
    source and never gets a security verdict, so the gate does not apply to
    it. Its promises are written straight through with the ungated ``ct-`` id
    (holding them would wait forever for a verdict that never comes)."""
    comm_id, ref = _mail_ref(CONN, "m-commit")
    out = _comm(ref, comm_id, direction=CommunicationDirection.OUTGOING)
    assert not MailPromotionGate(env.holds).applies(out)
    await _ingest(env.pipeline(gated=True), out, COMMIT)
    assert await env.count(MailSecurityHeldPromotionRow) == 0
    async with env.registry.session_factory() as s:
        ids = [r[0] for r in (await s.execute(select(CommitmentRow.id))).all()]
    assert len(ids) == 1 and ids[0].startswith("ct-")


# -- runner hook ---------------------------------------------------------------------------


class _NoScopes:
    async def list_scopes_with_runnable_jobs(self) -> list[str]:
        return []


async def test_runner_calls_post_pass_and_counts_failures():
    calls: list[int] = []

    async def ok() -> dict[str, int]:
        calls.append(1)
        return {"released": 0}

    runner = pr.MailSecurityPipelineRunner(
        repository=_NoScopes(),
        worker_factory=lambda s: None,
        env={ENV_MAIL_SECURITY_ENABLED: "1"},
        post_pass=ok,
    )
    await runner.run_once()
    assert calls == [1]

    async def bad() -> dict[str, int]:
        raise RuntimeError("x")

    runner = pr.MailSecurityPipelineRunner(
        repository=_NoScopes(),
        worker_factory=lambda s: None,
        env={ENV_MAIL_SECURITY_ENABLED: "1"},
        post_pass=bad,
    )
    assert await runner.run_once() == []
    assert runner.failure_counts[pr.RunnerFailure.RELEASE_PASS_FAILED] == 1


# -- migration 019 + queue visibility --------------------------------------------------------


def test_migration_019_up_then_down(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine, text
    from test_mail_security_intel import _cfg, _names

    url = f"sqlite:///{tmp_path / 'm19.db'}"
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE alembic_version (version_num VARCHAR(40) NOT NULL, "
                "CONSTRAINT pk_alembic_version PRIMARY KEY (version_num))"
            )
        )
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('018_mail_security_feedback')")
        )
    eng.dispose()
    command.upgrade(_cfg(url), "019_mail_held_promotions")
    names, ver = _names(url)
    assert "mail_security_held_promotions" in names
    assert ver == "019_mail_held_promotions"
    eng = create_engine(url)
    with eng.connect() as c:
        idx = {
            r[1]: r[2]
            for r in c.execute(text("PRAGMA index_list('mail_security_held_promotions')"))
        }
    eng.dispose()
    assert idx.get("ix_mail_security_held_target") == 1  # unique
    command.downgrade(_cfg(url), "018_mail_security_feedback")
    names, ver = _names(url)
    assert "mail_security_held_promotions" not in names
    assert ver == "018_mail_security_feedback"


async def test_held_items_show_in_review_queue_as_candidates(env):
    from backend.persistence.repositories.mail_security_review_repo import (
        load_queue_candidates,
    )

    comm_id, ref = _mail_ref(CONN, "m-queue")
    await env.registry.communications.create_communication(
        id=comm_id, person_id=PERSON, channel="email", direction="inbound", content_ref=ref
    )
    await _ingest(env.pipeline(gated=True), _comm(ref, comm_id))
    await env.verdict(SCOPE, ref, "asm:q", Disposition.RESTRICTED)
    await env.release()
    (cand,) = await load_queue_candidates(env.registry.session_factory, SCOPE)
    assert cand.communication_id == comm_id
    assert [(c.kind, c.status) for c in cand.commitments] == [
        ("held_attention", "REVIEW_CANDIDATE")
    ]


# -- Task 7.8 canary allow-list -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, frozenset()),
        ("", frozenset()),
        ("*", None),
        (SCOPE, frozenset({SCOPE})),
        (f"{SCOPE}, {OTHER_SCOPE}", frozenset({SCOPE, OTHER_SCOPE})),
        (f"{SCOPE},conn:not-a-uuid", frozenset()),  # any malformed entry -> none
        ("all", frozenset()),
    ],
)
def test_gate_scopes_parsing(raw, expected):
    from backend.mail_security.trust_gate import ENV_PROMOTION_GATE_SCOPES, gate_scopes

    env_map = {} if raw is None else {ENV_PROMOTION_GATE_SCOPES: raw}
    assert gate_scopes(env_map) == expected


async def test_canary_gates_only_listed_accounts(env):
    gate = MailPromotionGate(env.holds, scopes=frozenset({SCOPE}))
    in_id, in_ref = _mail_ref(CONN, "m-canary")
    out_id, out_ref = _mail_ref(OTHER, "m-outside")
    await _ingest(env.pipeline(gated=True, gate=gate), _comm(in_ref, in_id))
    await _ingest(env.pipeline(gated=True, gate=gate), _comm(out_ref, out_id))
    held = await env.holds_rows()
    assert [h.account_scope for h in held] == [SCOPE]
    # The non-canary account behaves exactly as with the gate OFF.
    assert len(await _active(env)) == 1


async def test_empty_canary_gates_nothing(env):
    gate = MailPromotionGate(env.holds, scopes=frozenset())
    comm_id, ref = _mail_ref(CONN, "m-none")
    await _ingest(env.pipeline(gated=True, gate=gate), _comm(ref, comm_id))
    assert await env.count(MailSecurityHeldPromotionRow) == 0
    assert len(await _active(env)) == 1
