"""Item D: runner alert emission (log only, de-duplicated, T-24)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import _mail_security_harness_helpers as H
import pytest
from structlog.testing import capture_logs

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.mail_security import pipeline_runner as pr
from backend.mail_security import telemetry
from backend.mail_security.ingestion_seam import ENV_MAIL_SECURITY_ENABLED
from backend.mail_security.observability import (
    Alert,
    AlertCode,
    AlertSeverity,
    HealthStage,
)

_ON = {ENV_MAIL_SECURITY_ENABLED: "1"}
_QUIET = 3600.0


@pytest.fixture
async def harness(tmp_path):
    h = await H.make_harness(tmp_path / "alerts.sqlite")
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


class Clock:
    def __init__(self) -> None:
        self.now: datetime = H.CLOCK

    def __call__(self) -> datetime:
        return self.now


def _alert() -> Alert:
    return Alert(
        AlertCode.DEAD_LETTER_PRESENT,
        HealthStage.QUEUE,
        AlertSeverity.WARNING,
        3.0,
        1.0,
    )


async def _runner(harness: Any, clock: Clock, env: dict[str, str] | None = _ON):
    await harness.registry.mail_security.upsert_job(
        account_scope=harness.scope,
        outbox_job_key="CANARY-job-key",
        source_key="CANARY-source@example.com",
        analysis_profile_version="v1",
        state="PENDING",
        meta={"requested": True},
    )
    return pr.MailSecurityPipelineRunner(
        repository=harness.registry.mail_security,
        worker_factory=lambda _scope: H.security_worker(harness),
        clock=clock,
        env=env,
        alert_quiet_seconds=_QUIET,
    )


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [r for r in logs if r["event"] == name]


class TestAlertEmission:
    async def test_fires_once_repeats_after_quiet_then_clears(self, harness, monkeypatch) -> None:
        clock = Clock()
        runner = await _runner(harness, clock)
        active: list[Alert] = [_alert()]
        monkeypatch.setattr(pr, "evaluate_alerts", lambda *_a, **_k: list(active))
        with capture_logs() as logs:
            await runner.run_once()
            clock.now += timedelta(seconds=30)
            await runner.run_once()  # still active, inside quiet period
            assert len(_events(logs, "mail_security.alert")) == 1
            clock.now += timedelta(seconds=_QUIET)
            await runner.run_once()  # quiet period elapsed
            assert len(_events(logs, "mail_security.alert")) == 2
            active.clear()
            await runner.run_once()
            cleared = _events(logs, "mail_security.alert_cleared")
            assert len(cleared) == 1
            assert cleared[0]["log_level"] == "info"
            await runner.run_once()  # already cleared: silent
            assert len(_events(logs, "mail_security.alert_cleared")) == 1
            active.append(_alert())
            await runner.run_once()  # re-activation fires again immediately
            assert len(_events(logs, "mail_security.alert")) == 3
        first = _events(logs, "mail_security.alert")[0]
        assert first["log_level"] == "warning"
        assert first["code"] == "DEAD_LETTER_PRESENT"
        assert first["stage"] == "QUEUE"
        assert first["observed"] == 3.0

    async def test_flag_off_is_silent_and_evaluates_nothing(self, harness, monkeypatch) -> None:
        called: list[int] = []
        monkeypatch.setattr(pr, "evaluate_alerts", lambda *_a, **_k: called.append(1) or [_alert()])
        runner = await _runner(harness, Clock(), env={})
        with capture_logs() as logs:
            assert await runner.run_once() is None
        assert called == []
        assert logs == []

    async def test_log_record_carries_no_identifiers(self, harness, monkeypatch) -> None:
        runner = await _runner(harness, Clock())
        monkeypatch.setattr(pr, "evaluate_alerts", lambda *_a, **_k: [_alert()])
        with capture_logs() as logs:
            await runner.run_once()
        recs = _events(logs, "mail_security.alert")
        assert recs
        assert set(recs[0]) == {
            "event",
            "log_level",
            "code",
            "stage",
            "severity",
            "observed",
            "threshold",
        }
        blob = repr(logs)
        for canary in ("CANARY", harness.scope, "example.com"):
            assert canary not in blob

    async def test_failing_snapshot_does_not_stop_processing(self, harness, monkeypatch) -> None:
        async def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("CANARY-secret")

        monkeypatch.setattr(pr, "collect_snapshot", boom)
        runner = await _runner(harness, Clock())
        with capture_logs() as logs:
            scopes = await runner.run_once()
        assert scopes == [harness.scope]
        jobs = await harness.registry.mail_security.list_jobs(harness.scope)
        assert jobs[0].state != "PENDING"  # the job was still processed
        failed = _events(logs, "mail_security.alert_evaluation_failed")
        assert failed[0]["error_type"] == "RuntimeError"
        assert "CANARY" not in repr(logs)

    async def test_scopes_bounded_per_pass(self, harness, monkeypatch) -> None:
        seen: list[int] = []

        async def spy(_repo: Any, scopes: Any, **_k: Any) -> Any:
            seen.append(len(list(scopes)))
            raise RuntimeError("stop")

        monkeypatch.setattr(pr, "collect_snapshot", spy)
        monkeypatch.setattr(pr, "MAX_ALERT_SCOPES", 1)
        runner = await _runner(harness, Clock())
        await harness.registry.mail_security.upsert_job(
            account_scope="conn:other",
            outbox_job_key="k-2",
            source_key="src-2",
            analysis_profile_version="v1",
            state="PENDING",
            meta={"requested": True},
        )
        scopes = await runner.run_once()
        assert scopes is not None and len(scopes) == 2
        assert seen == [1]
