"""Task 7.7: operations dashboard — live data wiring and redaction tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from test_mail_security_api import _SECURITY_TABLES, _assessment, _auth_ctx, _Env, _mint_token

import backend.api.auth_service as auth_service
from backend.api.endpoints import mail_security
from backend.attention.policy import reset_attention_policy
from backend.mail_security.classifier import arcforge_status as arc_status
from backend.mail_security.classifier.arcforge_client import (
    MetricsResult,
    ModelIdentity,
    ModelsResult,
    Outcome,
    ReadyResult,
)
from backend.mail_security.models import Disposition, ModelRun, ModelRunStatus
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailCampaignBandRow,
    MailCampaignMemberRow,
    MailSecurityConfirmationRow,
    MailSecurityFeedbackRow,
    MailSecurityHeldPromotionRow,
    MailThreadLedgerRow,
)
from backend.persistence.models.personal import (
    AttentionItemRow,
    CommitmentRow,
    CommunicationRow,
    FollowupRow,
    PersonRow,
)
from backend.persistence.repositories import RepositoryRegistry

SCOPE_A = "conn:aaaa1111"
SCOPE_B = "conn:bbbb2222"
CANARY = "CANARY-ops-9e1f"


# -- Arc status ---------------------------------------------------------------------


def test_summarize_samples_allow_lists_and_sums_labels():
    s = arc_status.summarize_samples(
        {
            'arcforge_embedding_queue_depth{model="a"}': 2,
            'arcforge_embedding_queue_depth{model="b"}': 1,
            "arcforge_embedding_inference_seconds_count": 4,
            "arcforge_embedding_inference_seconds_sum": 2.0,
            "arcforge_device_wedged": 3,
            f'arcforge_secret_series{{x="{CANARY}"}}': 9,
        }
    )
    assert s == {
        "device_wedged": 1.0,
        "inference_count": 4.0,
        "mean_inference_seconds": 0.5,
        "queue_depth": 3.0,
    }


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("", None),
        ("http://8.8.8.8:8100", "TARGET_NOT_PRIVATE"),
        ("http://arcforge.local:8100", "TARGET_NOT_LITERAL_IP"),
        ("http://10.1.1.15:8100/metrics", "TARGET_PATH"),
    ],
)
def test_unconfigured_or_invalid_target_opens_no_socket(url, code):
    def boom(_t: Any) -> Any:
        raise AssertionError("client must not be built")

    r = arc_status.fetch_status({arc_status.ENV_ARCFORGE_URL: url}, client_factory=boom)
    assert r["status"] == "NOT_CONFIGURED" and r.get("code") == code


class _FakeClient:
    def __init__(self, ready_ok: bool = True, model_id: str = "bge-small") -> None:
        self.ready_ok, self.model_id = ready_ok, model_id

    def ready(self) -> ReadyResult:
        if not self.ready_ok:
            return ReadyResult(Outcome.CONNECT_ERROR, "CONNECT_FAILED")
        return ReadyResult(Outcome.OK, "READY_OK", 200)

    def models(self) -> ModelsResult:
        return ModelsResult(
            Outcome.OK,
            "MODELS_OK",
            200,
            identity=ModelIdentity(self.model_id, "1.0", 384, "GPU.0"),
        )

    def metrics(self) -> MetricsResult:
        return MetricsResult(
            Outcome.OK,
            "METRICS_OK",
            200,
            samples={"arcforge_embedding_active_inferences": 1.0},
        )


def test_ready_status_reports_identity_and_activity():
    r = arc_status.fetch_status(
        {arc_status.ENV_ARCFORGE_URL: "http://10.1.1.15:8100"},
        client_factory=lambda t: _FakeClient(),
    )
    assert r["status"] == "READY"
    (host,) = r["hosts"]
    assert host["label"] == "arcforge-1"
    assert host["model"] == {"model_id": "bge-small", "version": "1.0", "device": "GPU.0"}
    assert host["activity"] == {"active_inferences": 1.0}
    assert "10.1.1.15" not in str(r)


def test_unsafe_identity_strings_are_dropped_and_unreachable_is_coded():
    r = arc_status.fetch_status(
        {arc_status.ENV_ARCFORGE_URL: "http://10.1.1.15:8100"},
        client_factory=lambda t: _FakeClient(model_id=f"<script>{CANARY}</script>"),
    )
    assert r["hosts"][0]["model"]["model_id"] is None
    down = arc_status.fetch_status(
        {arc_status.ENV_ARCFORGE_URL: "http://10.1.1.15:8100"},
        client_factory=lambda t: _FakeClient(ready_ok=False),
    )
    assert down == {
        "status": "UNREACHABLE",
        "hosts": [{"label": "arcforge-1", "status": "UNREACHABLE", "code": "CONNECT_FAILED"}],
    }


def test_multiple_hosts_degraded_and_limits():
    clients = iter([_FakeClient(), _FakeClient(ready_ok=False)])
    r = arc_status.fetch_status(
        {arc_status.ENV_ARCFORGE_URL: "http://10.1.1.15:8100, http://10.1.1.12:8100"},
        client_factory=lambda t: next(clients),
    )
    assert r["status"] == "DEGRADED"
    assert [h["label"] for h in r["hosts"]] == ["arcforge-1", "arcforge-2"]
    many = ",".join(f"http://10.1.1.{i}:8100" for i in range(1, 6))
    assert arc_status.fetch_status({arc_status.ENV_ARCFORGE_URL: many})["code"] == (
        "TOO_MANY_TARGETS"
    )
    mixed = "http://10.1.1.15:8100,http://8.8.8.8:8100"

    def boom(_t: Any) -> Any:
        raise AssertionError("no socket when any entry is invalid")

    assert (
        arc_status.fetch_status({arc_status.ENV_ARCFORGE_URL: mixed}, client_factory=boom)["status"]
        == "NOT_CONFIGURED"
    )


def test_status_is_cached():
    arc_status.reset_cache()
    calls: list[int] = []

    def factory(t: Any) -> Any:
        calls.append(1)
        return _FakeClient()

    env = {arc_status.ENV_ARCFORGE_URL: "http://10.1.1.15:8100"}
    t = [100.0]
    arc_status.cached_status(env, clock=lambda: t[0], client_factory=factory)
    t[0] += 10
    arc_status.cached_status(env, clock=lambda: t[0], client_factory=factory)
    t[0] += arc_status.CACHE_SECONDS
    arc_status.cached_status(env, clock=lambda: t[0], client_factory=factory)
    assert len(calls) == 2
    arc_status.reset_cache()


# -- API ----------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv(arc_status.ENV_ARCFORGE_URL, raising=False)
    arc_status.reset_cache()
    reset_attention_policy()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    auth_service.reset_memory_store()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ops.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [
        *_SECURITY_TABLES,
        MailSecurityFeedbackRow.__table__,
        MailSecurityHeldPromotionRow.__table__,
        MailThreadLedgerRow.__table__,
        MailCampaignMemberRow.__table__,
        MailCampaignBandRow.__table__,
        MailSecurityConfirmationRow.__table__,
        PersonRow.__table__,
        CommunicationRow.__table__,
        CommitmentRow.__table__,
        FollowupRow.__table__,
        AttentionItemRow.__table__,
    ]

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(tables[0].metadata.create_all, tables=tables)

    loop.run_until_complete(_setup())
    registry = RepositoryRegistry(make_session_factory(engine))
    app = FastAPI()
    app.include_router(mail_security.router)
    app.state.auth_ctx = _auth_ctx()
    app.state.repository_registry = registry
    try:
        yield _Env(app, registry, loop)
    finally:
        arc_status.reset_cache()
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


def _run(route: str, model: str, status: ModelRunStatus, ms: float) -> ModelRun:
    return ModelRun(
        route=route,
        model_id=model,
        runtime="openvino",
        status=status,
        privacy_policy_version="priv-v1",
        latency_ms=ms,
    )


def _seed(env: _Env) -> None:
    a1 = _assessment(
        SCOPE_A,
        assessment_id=f"asm:{CANARY}:1",
        disposition=Disposition.REVIEW,
        source_ref=f"src:{CANARY}",
    )
    a1 = a1.model_copy(
        update={
            "model_runs": [
                _run("secure/reader", "granite-4", ModelRunStatus("COMPLETED"), 120.0),
                _run("secure/reader", "granite-4", ModelRunStatus("COMPLETED"), 80.0),
            ]
        }
    )
    env.seed(a1)
    env.seed(_assessment(SCOPE_A, assessment_id="asm:2", disposition=Disposition.SAFE_TO_PROCESS))
    env.seed(_assessment(SCOPE_B, assessment_id="asm:b", disposition=Disposition.RESTRICTED))
    for fid, kind in (
        ("fb:1", "FALSE_POSITIVE"),
        ("fb:2", "CONFIRMED_THREAT"),
        ("fb:3", "FALSE_POSITIVE"),
    ):
        env.run(
            env.registry.mail_security.record_feedback(
                feedback_id=fid,
                account_scope=SCOPE_A,
                assessment_id="asm:2",
                verdict="SAFE",
                kind=kind,
                source="ACCOUNT_OWNER",
                reviewer_ref="rev:x",
                disposition_seen="REVIEW",
                proposals=[],
            )
        )


def _get(env: _Env, scopes: list[str]):
    token = env.run(_mint_token(account_scopes=scopes))
    with TestClient(env.app) as c:
        return c.get("/api/mail-security/ops", headers={"Authorization": f"Bearer {token}"})


def test_ops_wires_live_counts_for_callers_scope(env):
    _seed(env)
    r = _get(env, [SCOPE_A])
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    counts = d["counts"]
    assert counts["dispositions"] == {"REVIEW": 1, "SAFE_TO_PROCESS": 1}  # no SCOPE_B row
    run = counts["model_runs"]["secure/reader|granite-4|openvino|COMPLETED"]
    assert run == {"count": 2, "latency_ms_mean": 100.0, "latency_ms_max": 120.0}
    assert counts["feedback"]["by_kind"] == {"CONFIRMED_THREAT": 1, "FALSE_POSITIVE": 2}
    assert counts["feedback"]["flagged_false_positive_rate"] == pytest.approx(2 / 3)
    assert d["review_queue"]["pending"] == 1
    assert d["arc"] == {"status": "NOT_CONFIGURED"}
    assert d["runner"] == {"running": False, "failures": {}}
    assert {s["stage"] for s in d["health"]["stages"]}  # existing health view composed


def test_ops_response_carries_no_identifiers_or_free_text(env):
    _seed(env)
    text = _get(env, [SCOPE_A]).text
    for needle in (CANARY, SCOPE_A, SCOPE_B, "asm:", "src:", "rev:", "fb:"):
        assert needle not in text, needle

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                assert isinstance(k, str) and len(k) <= 120
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        else:
            assert v is None or isinstance(v, (bool, int, float, str))
            if isinstance(v, str):
                assert len(v) <= 120

    walk(json.loads(text))


def test_ops_unsafe_model_labels_are_bucketed(env):
    a = _assessment(SCOPE_A, assessment_id="asm:x", disposition=Disposition.REVIEW)
    env.seed(
        a.model_copy(
            update={
                "model_runs": [
                    _run(f"route {CANARY}!", "m", ModelRunStatus("COMPLETED"), 1.0),
                ]
            }
        )
    )
    body = _get(env, [SCOPE_A]).json()["data"]["counts"]["model_runs"]
    assert list(body) == ["OTHER|m|openvino|COMPLETED"]


def test_ops_requires_scopes(env):
    assert _get(env, []).status_code == 404
    with TestClient(env.app) as c:
        assert c.get("/api/mail-security/ops").status_code == 401
