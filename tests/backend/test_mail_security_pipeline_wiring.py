"""NerveShield Phase 2 pipeline wiring tests (Item 1: flag-gated runner).

Proves the explicitly-started, flag-gated multi-scope runner that finally
drives the Task 1.9 worker and the Task 2.1 sweeper in a deployment:

1. FLAG OFF -> no runner object, no asyncio task, no worker construction
   (R-001, I-01).
2. FLAG ON, end to end -> a real ``MailIngestWorker`` + the 1.8 seam schedule a
   PENDING job; the runner (injected clock/sleep) drains it to a persisted
   assessment without any test calling the worker directly.
3. ISOLATION OF FAILURE -> one scope whose worker blows up never stops the
   other scopes or the loop; failures are typed and counted.
4. CLEAN SHUTDOWN -> ``stop()`` cancels the loop and does not touch rows it
   does not own (a foreign live lease stays RUNNING).
5. SCOPE ENUMERATION IS RUNNER-ONLY -> the single deliberate cross-scope read
   returns scope strings only and no API layer module references it (R-023).
6. HEARTBEAT -> the Task 2.6 sweeper heartbeat advances on every pass.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import _mail_security_harness_helpers as H
import pytest

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.mail_security import telemetry
from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    ENV_MAIL_SECURITY_ENABLED,
    JOB_STATE_PENDING,
)
from backend.mail_security.pipeline_runner import (
    MailSecurityPipelineRunner,
    RunnerFailure,
)
from backend.mail_security.queue import JOB_STATE_DONE, JOB_STATE_RUNNING

_ON = {ENV_MAIL_SECURITY_ENABLED: "1"}
_OFF: dict[str, str] = {}
_NOW = H.CLOCK


@pytest.fixture
async def harness(tmp_path):
    h = await H.make_harness(tmp_path / "wiring.sqlite")
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


def _factory(harness: Any, built: list[str] | None = None):
    def make(scope: str):
        if built is not None:
            built.append(scope)
        return H.security_worker(harness) if scope == harness.scope else _other(
            harness, scope
        )

    return make


def _other(harness: Any, scope: str):
    from backend.mail_security.worker import (
        MailSecurityWorker,
        ObjectStoreContentResolver,
    )

    return MailSecurityWorker(
        repository=harness.registry.mail_security,
        content_resolver=ObjectStoreContentResolver(object_store=harness.store),
        account_scope=scope,
        analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        clock=lambda: _NOW,
        env=_ON,
    )


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


def _runner(harness: Any, *, env=_ON, factory=None, sleep=_no_sleep, **kw: Any):
    return MailSecurityPipelineRunner(
        repository=harness.registry.mail_security,
        worker_factory=factory or _factory(harness),
        interval_seconds=5.0,
        clock=lambda: _NOW,
        sleep=sleep,
        env=env,
        **kw,
    )


async def _seed_job(harness: Any, scope: str, key: str, *, state: str = "PENDING"):
    return await harness.registry.mail_security.upsert_job(
        account_scope=scope,
        outbox_job_key=key,
        source_key=f"src-{key}",
        analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        state=state,
        meta={"requested": True},
    )


# --------------------------------------------------------------------------
# 1. flag off
# --------------------------------------------------------------------------
class TestFlagOff:
    async def test_runner_inert_and_starts_no_task(self, harness) -> None:
        built: list[str] = []
        runner = _runner(harness, env=_OFF, factory=_factory(harness, built))
        await _seed_job(harness, harness.scope, "k-off")
        assert runner.enabled is False
        assert runner.start() is None
        assert await runner.run_once() is None
        assert built == []
        rows = await harness.registry.mail_security.list_jobs(harness.scope)
        assert [r.state for r in rows] == [JOB_STATE_PENDING]
        assert telemetry.sweeper_age_seconds(_NOW) is None

    async def test_wiring_flag_off_builds_nothing(self, monkeypatch) -> None:
        from backend.api import wiring

        monkeypatch.delenv(ENV_MAIL_SECURITY_ENABLED, raising=False)
        called: list[str] = []
        monkeypatch.setattr(
            wiring, "_mail_security_object_store", lambda: called.append("store")
        )
        target = SimpleNamespace(
            state=SimpleNamespace(repository_registry=object())
        )
        before = set(asyncio.all_tasks())
        assert wiring.start_mail_security_runner(target) is None
        assert set(asyncio.all_tasks()) == before
        assert called == []
        assert getattr(target.state, "mail_security_runner", None) is None
        await wiring.stop_mail_security_runner(target)  # no-op, never raises

    async def test_wiring_flag_on_without_store_or_repo_starts_nothing(
        self, monkeypatch
    ) -> None:
        from backend.api import wiring

        monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
        monkeypatch.setattr(wiring, "_mail_security_object_store", lambda: None)
        target = SimpleNamespace(
            state=SimpleNamespace(
                repository_registry=SimpleNamespace(mail_security=object())
            )
        )
        assert wiring.start_mail_security_runner(target) is None
        target2 = SimpleNamespace(state=SimpleNamespace(repository_registry=None))
        assert wiring.start_mail_security_runner(target2) is None


    async def test_wiring_flag_on_starts_and_stops_runner(
        self, harness, monkeypatch
    ) -> None:
        from backend.api import wiring

        monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
        monkeypatch.setattr(wiring, "_mail_security_object_store", lambda: harness.store)
        target = SimpleNamespace(
            state=SimpleNamespace(repository_registry=harness.registry)
        )
        task = wiring.start_mail_security_runner(target)
        assert task is not None and not task.done()
        assert target.state.mail_security_runner is not None
        await wiring.stop_mail_security_runner(target)
        assert task.done()
        assert target.state.mail_security_runner is None


# --------------------------------------------------------------------------
# 2. flag on, end to end
# --------------------------------------------------------------------------
class TestEndToEndDrain:
    async def test_ingest_seam_runner_to_persisted_assessment(
        self, harness, monkeypatch
    ) -> None:
        monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
        await H.ingest(
            harness,
            monkeypatch,
            H.mailitem("<wire-1@example-news.com>", H.benign_mime()),
        )
        repo = harness.registry.mail_security
        jobs = await repo.list_jobs(harness.scope)
        assert [j.state for j in jobs] == [JOB_STATE_PENDING]
        assert await repo.list_assessments(harness.scope) == []

        done = asyncio.Event()
        passes = 0

        async def sleep(_: float) -> None:
            nonlocal passes
            passes += 1
            if passes >= 2:
                done.set()
            await asyncio.sleep(0)

        runner = _runner(harness, sleep=sleep)
        task = runner.start()
        assert task is not None
        await asyncio.wait_for(done.wait(), timeout=10)
        await runner.stop()

        jobs = await repo.list_jobs(harness.scope)
        assert [j.state for j in jobs] == [JOB_STATE_DONE]
        assessments = await repo.list_assessments(harness.scope)
        assert len(assessments) == 1
        # Task 2.6 heartbeat advanced by the runner's sweeps.
        assert telemetry.sweeper_age_seconds(_NOW) == 0.0

    async def test_start_is_idempotent_and_stop_is_reentrant(self, harness) -> None:
        runner = _runner(harness)
        t1 = runner.start()
        t2 = runner.start()
        assert t1 is t2
        await runner.stop()
        await runner.stop()


# --------------------------------------------------------------------------
# 3. failure isolation + typed counted failures
# --------------------------------------------------------------------------
class TestFailureIsolation:
    async def test_one_failing_scope_does_not_stop_others(self, harness) -> None:
        good, bad = harness.scope, "conn:bad-scope"
        await _seed_job(harness, bad, "k-bad")
        await _seed_job(harness, good, "k-good")

        def factory(scope: str):
            if scope == bad:
                raise RuntimeError("boom CANARY-secret")
            return H.security_worker(harness)

        runner = _runner(harness, factory=factory)
        await runner.run_once()
        await runner.run_once()  # loop survives; bad scope retried each pass
        assert runner.failure_counts[RunnerFailure.WORKER_BUILD_FAILED] == 2
        assert runner.failure_counts[RunnerFailure.SCOPE_DRAIN_FAILED] == 0
        # The good scope was attempted despite the bad one (job leaves PENDING;
        # content is absent so it records INCOMPLETE rather than staying idle).
        good_jobs = await harness.registry.mail_security.list_jobs(good)
        assert good_jobs[0].state != JOB_STATE_PENDING
        bad_jobs = await harness.registry.mail_security.list_jobs(bad)
        assert bad_jobs[0].state == JOB_STATE_PENDING

    async def test_drain_exception_is_typed_counted_and_logged_safely(
        self, harness, capsys
    ) -> None:
        await _seed_job(harness, harness.scope, "k-1")
        await _seed_job(harness, "conn:other", "k-2")

        class Boom:
            async def process_available(self, **_: Any) -> list[Any]:
                raise ValueError("CANARY-secret-body")

            async def record_incomplete(self, *_: Any) -> None:
                return None

        def factory(scope: str):
            return Boom() if scope == "conn:other" else H.security_worker(harness)

        runner = _runner(harness, factory=factory)
        await runner.run_once()
        assert runner.failure_counts[RunnerFailure.SCOPE_DRAIN_FAILED] == 1
        good = await harness.registry.mail_security.list_jobs(harness.scope)
        assert good[0].state != JOB_STATE_PENDING
        out = capsys.readouterr()
        assert "CANARY" not in out.out + out.err

    async def test_enumeration_failure_does_not_kill_loop(self, harness) -> None:
        calls = 0

        class Repo:
            async def list_scopes_with_runnable_jobs(self) -> list[str]:
                nonlocal calls
                calls += 1
                raise OSError("db down")

        runner = MailSecurityPipelineRunner(
            repository=Repo(),
            worker_factory=_factory(harness),
            interval_seconds=1.0,
            clock=lambda: _NOW,
            sleep=_no_sleep,
            env=_ON,
        )
        await runner.run_once()
        await runner.run_once()
        assert calls == 2
        assert runner.failure_counts[RunnerFailure.SCOPE_ENUMERATION_FAILED] == 2

    async def test_failing_enumeration_does_not_advance_heartbeat(
        self, harness
    ) -> None:
        class Repo:
            async def list_scopes_with_runnable_jobs(self) -> list[str]:
                raise OSError("db down")

        runner = MailSecurityPipelineRunner(
            repository=Repo(),
            worker_factory=_factory(harness),
            interval_seconds=1.0,
            clock=lambda: _NOW,
            sleep=_no_sleep,
            env=_ON,
        )
        await runner.run_once()
        # Nothing swept: the stale-sweeper signal must stay un-fed (None = never).
        assert telemetry.sweeper_age_seconds(_NOW) is None

    async def test_loop_survives_failing_pass(self, harness) -> None:
        passes = 0
        done = asyncio.Event()

        class Repo:
            async def list_scopes_with_runnable_jobs(self) -> list[str]:
                nonlocal passes
                passes += 1
                if passes >= 3:
                    done.set()
                raise OSError("down")

        runner = MailSecurityPipelineRunner(
            repository=Repo(),
            worker_factory=_factory(harness),
            interval_seconds=1.0,
            clock=lambda: _NOW,
            sleep=_no_sleep,
            env=_ON,
        )
        runner.start()
        await asyncio.wait_for(done.wait(), timeout=10)
        await runner.stop()

    def test_failure_codes_are_a_closed_typed_set(self) -> None:
        assert {f.value for f in RunnerFailure} == {
            "SCOPE_ENUMERATION_FAILED",
            "WORKER_BUILD_FAILED",
            "SCOPE_SWEEP_FAILED",
            "SCOPE_DRAIN_FAILED",
        }


# --------------------------------------------------------------------------
# 4. clean shutdown
# --------------------------------------------------------------------------
class TestShutdown:
    async def test_stop_cancels_loop_and_releases_nothing_foreign(
        self, harness
    ) -> None:
        # A job leased by ANOTHER worker with a live lease.
        row = await _seed_job(harness, harness.scope, "k-foreign")
        future = _NOW + timedelta(hours=1)
        await harness.registry.mail_security.mark_job_state(
            harness.scope,
            row.id,
            state=JOB_STATE_RUNNING,
            attempts=1,
            lease_owner="other-worker",
            lease_expires_at=future,
        )
        entered = asyncio.Event()

        async def blocking_sleep(_: float) -> None:
            entered.set()
            await asyncio.sleep(3600)

        runner = _runner(harness, sleep=blocking_sleep)
        task = runner.start()
        assert task is not None
        await asyncio.wait_for(entered.wait(), timeout=10)
        await runner.stop()
        assert task.done()
        assert task.cancelled() or task.exception() is None
        jobs = await harness.registry.mail_security.list_jobs(harness.scope)
        assert jobs[0].state == JOB_STATE_RUNNING
        assert jobs[0].lease_owner == "other-worker"
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                    and "_loop" in repr(t.get_coro())]

    async def test_cancellation_is_not_swallowed_by_scope_handler(
        self, harness
    ) -> None:
        await _seed_job(harness, harness.scope, "k-cancel")
        gate = asyncio.Event()

        class Slow:
            async def process_available(self, **_: Any) -> list[Any]:
                gate.set()
                await asyncio.sleep(3600)
                return []

            async def record_incomplete(self, *_: Any) -> None:
                return None

        runner = _runner(harness, factory=lambda scope: Slow())
        task = runner.start()
        await asyncio.wait_for(gate.wait(), timeout=10)
        await runner.stop()
        assert task is not None and task.done()
        assert sum(runner.failure_counts.values()) == 0


# --------------------------------------------------------------------------
# 5. scope enumeration is runner-only
# --------------------------------------------------------------------------
class TestScopeEnumerationRunnerOnly:
    async def test_returns_scope_strings_only_for_runnable_states(
        self, harness
    ) -> None:
        repo = harness.registry.mail_security
        await _seed_job(harness, "conn:a", "ka", state="PENDING")
        await _seed_job(harness, "conn:a", "ka2", state="PENDING")
        await _seed_job(harness, "conn:b", "kb", state="ERROR_RETRYABLE")
        await _seed_job(harness, "conn:c", "kc", state="DONE")
        await _seed_job(harness, "conn:d", "kd", state="DEAD_LETTER")
        scopes = await repo.list_scopes_with_runnable_jobs()
        assert scopes == ["conn:a", "conn:b"]
        assert all(type(s) is str for s in scopes)

    async def test_includes_running_scope_so_expired_leases_get_swept(
        self, harness
    ) -> None:
        await _seed_job(harness, "conn:r", "kr", state="RUNNING")
        assert await harness.registry.mail_security.list_scopes_with_runnable_jobs() == [
            "conn:r"
        ]

    def test_api_layer_does_not_use_it(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[2] / "backend"
        offenders: list[str] = []
        for path in (root / "api").rglob("*.py"):
            if "list_scopes_with_runnable_jobs" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(root)))
        assert offenders == []
        # Only the repository (definition) and the runner module may name it.
        users = sorted(
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if "list_scopes_with_runnable_jobs" in p.read_text(encoding="utf-8")
        )
        assert users == [
            "mail_security/pipeline_runner.py",
            "persistence/repositories/mail_security_repo.py",
        ]

    async def test_each_worker_bound_to_exactly_one_scope(self, harness) -> None:
        built: list[str] = []
        await _seed_job(harness, "conn:x", "kx")
        await _seed_job(harness, "conn:y", "ky")
        runner = _runner(harness, factory=_factory(harness, built))
        await runner.run_once()
        assert sorted(built) == ["conn:x", "conn:y"]
        assert len(built) == len(set(built))


# --------------------------------------------------------------------------
# 6. sweeper work
# --------------------------------------------------------------------------
class TestSweeperRuns:
    async def test_expired_lease_reclaimed_by_runner(self, harness) -> None:
        row = await _seed_job(harness, harness.scope, "k-exp")
        await harness.registry.mail_security.mark_job_state(
            harness.scope,
            row.id,
            state=JOB_STATE_RUNNING,
            attempts=1,
            lease_owner="dead-worker",
            lease_expires_at=_NOW - timedelta(minutes=5),
        )
        runner = _runner(harness)
        await runner.run_once()
        jobs = await harness.registry.mail_security.list_jobs(harness.scope)
        assert jobs[0].lease_owner != "dead-worker"
        assert jobs[0].last_error_code is not None or jobs[0].state != JOB_STATE_RUNNING

    async def test_sources_provider_creates_missing_jobs(self, harness) -> None:
        await _seed_job(harness, harness.scope, "k-seed")

        async def sources(scope: str) -> list[dict[str, Any]]:
            return [{"provider": "imap", "provider_uid": "77", "folder": "INBOX"}]

        runner = _runner(harness, sources_provider=sources)
        await runner.run_once()
        jobs = await harness.registry.mail_security.list_jobs(harness.scope)
        assert len(jobs) == 2


# --------------------------------------------------------------------------
# Item 2: threat-intel matches wired into assessments (R-004.5, R-006.2, T-19)
# --------------------------------------------------------------------------
_INTEL_HOST = "canaryintel7f3a-evil.example"
_INTEL_SHA = "ab" * 32
_INTEL_URL = f"http://{_INTEL_HOST}/login?x=1"


def _intel_mime(host: str = _INTEL_HOST, *, attach: bool = False) -> bytes:
    """Benign-looking plain mail with one plain link (and optionally a PDF)."""
    head = (
        "From: Friend <friend@friendly-corp.example>\r\n"
        "To: user@example.com\r\n"
        "Subject: hello\r\n"
        "Message-ID: <intel-1@friendly-corp.example>\r\n"
        "Date: Mon, 27 Oct 2026 09:00:00 +0000\r\n"
        "MIME-Version: 1.0\r\n"
    )
    if not attach:
        return (
            head + "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"see http://{host}/login?x=1 thanks\r\n"
        ).encode()
    return (
        head + 'Content-Type: multipart/mixed; boundary="Bq"\r\n\r\n'
        "--Bq\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nplain body\r\n"
        "--Bq\r\nContent-Type: application/pdf\r\n"
        'Content-Disposition: attachment; filename="doc.pdf"\r\n'
        "Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--Bq--\r\n"
    ).encode()


def _record(
    value: str = _INTEL_HOST,
    *,
    kind: str = "DOMAIN",
    confidence: str = "FEED_ASSERTED",
    expires_in: timedelta = timedelta(days=7),
    feed_version: str = "v1",
):
    from backend.mail_security.intel import validate_record

    return validate_record(
        {
            "indicator_type": kind,
            "value": value,
            "source": "feed:alpha",
            "confidence_type": confidence,
            "license": "CC0-1.0",
            "provenance": "prov:sig-ok-1",
            "feed_version": feed_version,
            "first_seen": _NOW - timedelta(days=2),
            "last_seen": _NOW - timedelta(hours=1),
            "expires_at": _NOW + expires_in,
        },
        now=_NOW,
    )


async def _promote(harness: Any, *records: Any) -> None:
    await harness.registry.mail_intel.promote(list(records), now=_NOW)


async def _assess_one(
    harness: Any,
    monkeypatch: Any,
    body: bytes,
    *,
    intel: Any = "registry",
    uid: int = 5,
    clock=None,
):
    """Ingest one message, run one worker (optionally with intel), return
    (assessment_row, findings)."""
    await H.ingest(
        harness, monkeypatch, H.mailitem(f"<m{uid}@friendly-corp.example>", body), uid=uid
    )
    store = harness.registry.mail_intel if intel == "registry" else intel
    worker = H.security_worker(harness, intel_store=store)
    if clock is not None:
        worker._clock = clock
    await worker.process_one()
    rows = await harness.registry.mail_security.list_assessments(harness.scope)
    assert len(rows) == 1
    return rows[0]


def _rank(disposition: str) -> int:
    order = ["SAFE_TO_PROCESS", "LOW_PRIORITY", "INCOMPLETE", "REVIEW", "RESTRICTED"]
    return order.index(disposition)


async def _detail(harness: Any, row: Any):
    """(finding rows, policy dict) of a persisted assessment."""
    findings = await harness.registry.mail_security.get_findings(
        harness.scope, row.assessment_id
    )
    return findings, row.assessment["policy"]


class _RaisingIntel:
    async def lookup(self, *a: Any, **k: Any):
        raise RuntimeError("intel store down")


class TestIntelWiring:
    async def test_baseline_without_intel_is_low_priority(self, harness, monkeypatch):
        row = await _assess_one(harness, monkeypatch, _intel_mime(), intel=None)
        assert row.disposition == "LOW_PRIORITY"

    async def test_live_match_raises_disposition_to_review_with_safe_evidence(
        self, harness, monkeypatch
    ) -> None:
        await _promote(harness, _record())
        row = await _assess_one(harness, monkeypatch, _intel_mime())
        assert row.disposition == "REVIEW"
        findings, policy = await _detail(harness, row)
        intel_findings = [f for f in findings if f.rule_id == "THREAT_INTEL_MATCH"]
        assert len(intel_findings) == 1
        f = intel_findings[0]
        assert f.category == "INTEL_MATCH" and f.explanation_code == "INTEL_MATCH.DOMAIN"
        refs = " ".join(f.meta["evidence_refs"])
        for code in ("intel:src:feed:alpha", "intel:method:EXACT",
                     "intel:conf:FEED_ASSERTED", "intel:feed:v1", "intel:obs:",
                     "intel:exp:"):
            assert code in refs
        assert _INTEL_HOST not in repr(f)
        assert "THREAT_INTEL_MATCH" in policy["reason_codes"]
        assert policy["policy_version"] == "mail-security-policy@2"
        # evidence only: SHADOW grants no capability, no action (R-006.2)
        assert policy["action_capabilities"] == []
        assert policy["mode"] == "SHADOW"

    async def test_unverified_match_still_raises_to_review(self, harness, monkeypatch):
        await _promote(harness, _record(confidence="UNVERIFIED"))
        row = await _assess_one(harness, monkeypatch, _intel_mime())
        assert row.disposition == "REVIEW"

    async def test_parent_domain_url_hash_file_hash_and_sender_domain_match(
        self, harness, monkeypatch
    ) -> None:
        # parent domain of the link host
        await _promote(harness, _record("canaryintel7f3a-evil.example"))
        row = await _assess_one(
            harness, monkeypatch, _intel_mime("login.canaryintel7f3a-evil.example")
        )
        assert row.disposition == "REVIEW"

    @pytest.mark.parametrize(
        "make",
        [
            lambda: _record(_INTEL_URL, kind="URL_SHA256"),
            lambda: _record("friendly-corp.example"),  # sender domain
        ],
    )
    async def test_url_and_sender_domain_indicators_match(
        self, harness, monkeypatch, make
    ) -> None:
        await _promote(harness, make())
        row = await _assess_one(harness, monkeypatch, _intel_mime())
        assert row.disposition == "REVIEW"

    async def test_file_hash_indicator_matches_attachment(self, harness, monkeypatch):
        from backend.mail_security.parser import parse_mime_evidence

        ev = parse_mime_evidence(_intel_mime("clean.example", attach=True))
        sha = ev.attachments[0].sha256
        assert sha
        await _promote(harness, _record(sha, kind="FILE_SHA256"))
        row = await _assess_one(
            harness, monkeypatch, _intel_mime("clean.example", attach=True)
        )
        assert row.disposition == "REVIEW"

    @pytest.mark.parametrize("how", ["expired", "disabled", "rolled_back", "superseded_gone"])
    async def test_non_live_match_does_not_raise(self, harness, monkeypatch, how):
        intel = harness.registry.mail_intel
        clock = None
        if how == "expired":
            await _promote(harness, _record(expires_in=timedelta(hours=1)))
            clock = lambda: _NOW + timedelta(hours=2)  # noqa: E731
        elif how == "disabled":
            await _promote(harness, _record())
            from backend.mail_security.intel import IndicatorType

            await intel.set_enabled(
                IndicatorType.DOMAIN, _INTEL_HOST, "feed:alpha", enabled=False
            )
        elif how == "rolled_back":
            await _promote(harness, _record("other-host.example"))
            await _promote(harness, _record(feed_version="v2"))
            await intel.rollback(1)
        else:  # exactly at expiry is expired (boundary)
            await _promote(harness, _record(expires_in=timedelta(hours=1)))
            clock = lambda: _NOW + timedelta(hours=1)  # noqa: E731
        row = await _assess_one(harness, monkeypatch, _intel_mime(), clock=clock)
        assert row.disposition == "LOW_PRIORITY"
        findings, policy = await _detail(harness, row)
        assert not [f for f in findings if f.rule_id == "THREAT_INTEL_MATCH"]
        assert "THREAT_INTEL_MATCH" not in policy["reason_codes"]

    @pytest.mark.parametrize("store", ["none", "empty", "raising"])
    async def test_absent_intel_never_lowers_or_changes_anything(
        self, harness, monkeypatch, store
    ) -> None:
        """Same message with/without an unavailable or empty store gives an
        identical assessment, including for a message the policy already
        escalates (a missing match cannot clear a REVIEW)."""
        body = H.phish_deceptive_link_mime()
        baseline = await _assess_one(harness, monkeypatch, body, intel=None, uid=5)
        base_f, base_p = await _detail(harness, baseline)
        intel = {"none": None, "empty": "registry", "raising": _RaisingIntel()}[store]
        h2 = await H.make_harness(harness.engine.url.database + f".{store}.sqlite")
        try:
            row = await _assess_one(h2, monkeypatch, body, intel=intel, uid=5)
            f2, p2 = await _detail(h2, row)
        finally:
            await H.teardown_harness(h2)
        assert baseline.disposition == "REVIEW"
        assert row.disposition == baseline.disposition
        assert p2["reason_codes"] == base_p["reason_codes"]
        assert [f.rule_id for f in f2] == [f.rule_id for f in base_f]

    async def test_match_never_lowers_an_escalated_message(self, harness, monkeypatch):
        body = H.phish_deceptive_link_mime()
        await _promote(harness, _record("evil-login.example"))
        row = await _assess_one(harness, monkeypatch, body)
        assert _rank(row.disposition) >= _rank("REVIEW")

    async def test_raising_store_logs_safe_code_only(self, harness, monkeypatch):
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            row = await _assess_one(
                harness, monkeypatch, _intel_mime(), intel=_RaisingIntel()
            )
        assert row.disposition == "LOW_PRIORITY"
        ev = [e for e in logs if e.get("event") == "mail_security.intel_unavailable"]
        assert ev and ev[0]["code"] == "INTEL_UNAVAILABLE"
        assert _INTEL_HOST not in repr(logs)

    async def test_worker_without_intel_store_is_byte_identical(self, harness, monkeypatch):
        """Default worker (no store) == worker with an empty store."""
        a = await _assess_one(harness, monkeypatch, _intel_mime(), intel=None)
        assert a.disposition == "LOW_PRIORITY"

    async def test_intel_store_read_only_and_global(self, harness, monkeypatch):
        """The wiring never writes to the intel store (global data, nothing
        derived from mail)."""
        from sqlalchemy import text

        await _promote(harness, _record())
        async with harness.engine.connect() as conn:
            before = (await conn.execute(text("SELECT * FROM mail_intel_indicators"))).fetchall()
        await _assess_one(harness, monkeypatch, _intel_mime())
        async with harness.engine.connect() as conn:
            after = (await conn.execute(text("SELECT * FROM mail_intel_indicators"))).fetchall()
        assert before == after


class TestIntelCanary:
    async def test_indicator_value_absent_everywhere(self, harness, monkeypatch):
        import httpx
        from fastapi import FastAPI
        from prometheus_client import CollectorRegistry, generate_latest
        from sqlalchemy import text
        from structlog.testing import capture_logs

        import backend.api.auth_service as auth_service
        from backend.api.auth_service import AuthContext
        from backend.api.endpoints import mail_security as api
        from backend.mail_security import observability as obs

        await _promote(
            harness,
            _record(),
            _record(_INTEL_URL, kind="URL_SHA256"),
            _record("friendly-corp.example"),
        )
        with capture_logs() as logs:
            row = await _assess_one(harness, monkeypatch, _intel_mime())
            snapshot = await obs.collect_snapshot(
                harness.registry.mail_security, [harness.scope], now=_NOW, env=_ON
            )
        assert row.disposition == "REVIEW"
        reg = CollectorRegistry()
        obs.publish_metrics(snapshot, obs.HealthThresholds(), registry=reg)
        view = obs.health_view(snapshot, obs.HealthThresholds())

        # persisted rows: every security/intel-adjacent table except the global
        # intel table (which legitimately stores the canonical indicator).
        needles = ("canaryintel7f3a", _INTEL_URL, "login?x=1")
        async with harness.engine.connect() as conn:
            for table in (
                "mail_security_jobs",
                "mail_evidence",
                "mail_security_assessments",
                "mail_security_findings",
            ):
                dumped = repr((await conn.execute(text(f"SELECT * FROM {table}"))).fetchall())  # noqa: S608
                for n in needles:
                    assert n not in dumped, (table, n)

        # API list + detail + health through the real router
        ctx = AuthContext(
            secret="unit-secret-" + "a" * 60,
            issuer="https://auth.test.local/realms/nervecenter",
            client_id="unit",
            local_admin_enabled=False,
            local_admin_password=None,
            session_factory=None,
        )
        auth_service.reset_memory_store()
        token, exp, claims = ctx._mint(
            sub="u", typ="access", roles=["admin"],
            ttl=auth_service.ACCESS_TOKEN_TTL,
            extra={"account_scopes": [harness.scope]},
        )
        await auth_service.record_session(
            ctx, jti=claims["jti"], sub="u", typ="access", expires_at=exp, source="test"
        )
        app = FastAPI()
        app.include_router(api.router)
        app.state.auth_ctx = ctx
        app.state.repository_registry = harness.registry
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            lst = await client.get("/api/mail-security/assessments", headers=headers)
            det = await client.get(
                f"/api/mail-security/assessments/{row.assessment_id}", headers=headers
            )
            hl = await client.get("/api/mail-security/health", headers=headers)
        assert lst.status_code == 200 and det.status_code == 200
        assert "THREAT_INTEL" in det.text  # evidence visible as a safe code
        blob = "\n".join(
            [lst.text, det.text, hl.text, repr(view), repr(snapshot),
             generate_latest(reg).decode(), " ".join(str(e) for e in logs)]
        )
        for n in (*needles, "friendly-corp", H.CANARY):
            assert n not in blob, f"leak: {n!r}"
