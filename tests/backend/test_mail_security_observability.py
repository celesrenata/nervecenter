"""NerveShield observability tests (Phase 2, Task 2.6).

Done criterion (tasks.md 2.6): "no PII in metrics/logs; failure alert proof."
Requirements R-018, R-021 (R-021.4), R-017.2, R-022.4; invariants I-02;
threats T-14 (canary absent from logs/metrics), T-15 (cross-account), T-24.

Reuses the SQLite scratch-DB ingest harness. Time is injected everywhere (no
sleeps, no wall clock). SQLite proves the aggregation LOGIC only.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import _mail_security_harness_helpers as H
import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

import backend.api.auth_service as auth_service
from backend.api.auth_service import AuthContext
from backend.api.endpoints import mail_security
from backend.mail_security import observability as obs
from backend.mail_security import telemetry
from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    ENV_MAIL_SECURITY_ENABLED,
)
from backend.mail_security.queue import QueuePolicy, sweep_once
from backend.mail_security.worker import MailSecurityWorker, ObjectStoreContentResolver
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailEvidenceRow,
    MailIntelIndicatorRow,
    MailSecurityAssessmentRow,
    MailSecurityFindingRow,
    MailSecurityJobRow,
)
from backend.persistence.repositories import RepositoryRegistry

_T0 = datetime(2026, 10, 27, 10, 0, 0, tzinfo=UTC)
_ON = {ENV_MAIL_SECURITY_ENABLED: "1"}
_POLICY = QueuePolicy(max_attempts=3, lease_seconds=60, base_delay_seconds=30.0)
_TH = obs.HealthThresholds()


@pytest.fixture(autouse=True)
def _reset_heartbeat():
    telemetry.reset_sweeper_heartbeat()
    yield
    telemetry.reset_sweeper_heartbeat()


def _job(state: str, attempts: int = 0, age: float = 0.0, **kw: Any) -> NS:
    return NS(
        state=state,
        attempts=attempts,
        created_at=_T0 - timedelta(seconds=age),
        lease_owner=None,
        lease_expires_at=None,
        **kw,
    )


def _row(
    *,
    fidelity: str = "ORIGINAL_RFC822",
    state: str = "ASSESSED",
    rule_ids: tuple[str, ...] = (),
    reasons: tuple[str, ...] = (),
) -> NS:
    return NS(
        evidence_fidelity=fidelity,
        processing_state=state,
        assessment={
            "findings": [{"rule_id": r} for r in rule_ids],
            "policy": {"reason_codes": list(reasons)},
        },
    )


def _snap(
    *,
    jobs: list[Any] | None = None,
    rows: list[Any] | None = None,
    feeds: list[Any] | None = None,
    egress: list[Any] | None = None,
    sweeper_age: float | None = 10.0,
    enabled: bool = True,
) -> obs.ObservabilitySnapshot:
    return obs.build_snapshot(
        now=_T0,
        jobs=jobs if jobs is not None else [],
        assessments=rows if rows is not None else [],
        feed_rows=feeds,
        egress_records=egress,
        policy=_POLICY,
        latency=telemetry.LatencyRecorder(),
        sweeper_age=sweeper_age,
        enabled=enabled,
    )


def _status(snapshot: obs.ObservabilitySnapshot, stage: obs.HealthStage) -> obs.HealthStatus:
    return next(h.status for h in obs.compute_health(snapshot, _TH) if h.stage == stage)


def _feed_row(
    source: str = "feed-a", *, age: float = 60.0, ttl: float = 86400.0, **kw: Any
) -> NS:
    base: dict[str, Any] = {
        "source": source,
        "feed_version": "feed-abc123",
        "last_seen": _T0 - timedelta(seconds=age),
        "expires_at": _T0 + timedelta(seconds=ttl),
        "enabled": True,
        "rolled_back": False,
        "superseded_in_version": None,
    }
    base.update(kw)
    return NS(**base)


def _healthy() -> obs.ObservabilitySnapshot:
    return _snap(
        jobs=[_job("DONE", 1) for _ in range(6)] + [_job("PENDING", 0, age=5)],
        rows=[_row() for _ in range(10)],
        feeds=[_feed_row()],
        egress=[NS(reason="EGRESS_DISABLED", decision="deny")],
        sweeper_age=30.0,
    )


# --------------------------------------------------------------------------- #
# depth / age / attempts (injected clock)
# --------------------------------------------------------------------------- #


class TestQueueAggregates:
    def test_depth_age_and_attempt_buckets(self) -> None:
        jobs = [
            _job("PENDING", 0, age=100),
            _job("PENDING", 0, age=700),
            _job("RUNNING", 1),
            _job("ERROR_RETRYABLE", 2),
            _job("ERROR_RETRYABLE", 3),  # at ceiling (max_attempts=3)
            _job("DEAD_LETTER", 5),
            _job("DONE", 1),
            _job("WEIRD_UNKNOWN_STATE", 0),
        ]
        q = obs.build_queue_snapshot(jobs, now=_T0, policy=_POLICY)
        assert q.depth == {
            "PENDING": 2,
            "RUNNING": 1,
            "ERROR_RETRYABLE": 2,
            "DEAD_LETTER": 1,
            "DONE": 1,
            "OTHER": 1,
        }
        assert q.oldest_pending_age_seconds == 700.0
        assert q.attempts == {"0": 3, "1": 2, "2": 1, "3_4": 1, "5_PLUS": 1}
        assert q.at_ceiling == 1
        assert q.total == 8

    def test_naive_datetimes_and_empty(self) -> None:
        naive = NS(state="PENDING", attempts=0, created_at=datetime(2026, 10, 27, 9, 0, 0))
        q = obs.build_queue_snapshot([naive], now=_T0)
        assert q.oldest_pending_age_seconds == 3600.0
        empty = obs.build_queue_snapshot([], now=_T0)
        assert empty.oldest_pending_age_seconds is None and empty.total == 0

    @pytest.mark.parametrize(
        ("n", "bucket"),
        [(0, "0"), (1, "1"), (2, "2"), (3, "3_4"), (4, "3_4"), (5, "5_PLUS"), (99, "5_PLUS")],
    )
    def test_attempt_bucket(self, n: int, bucket: str) -> None:
        assert obs.attempt_bucket(n).value == bucket


# --------------------------------------------------------------------------- #
# latency: numbers only
# --------------------------------------------------------------------------- #


class TestLatency:
    def test_numbers_only_and_enum_keyed(self) -> None:
        rec = telemetry.LatencyRecorder()
        rec.observe("SCAN", 0.02)
        rec.observe(telemetry.LatencyStage.SCAN, 2)
        rec.observe("NOT_A_STAGE", 1.0)  # dropped
        rec.observe("user@example.com", 1.0)  # dropped
        rec.observe("SCAN", "1.0")  # dropped (text)
        rec.observe("SCAN", True)  # dropped (bool)
        rec.observe("SCAN", float("nan"))  # dropped
        rec.observe("SCAN", -1.0)  # dropped
        snap = rec.snapshot()
        assert set(snap) == {s.value for s in telemetry.LatencyStage}
        assert snap["SCAN"].count == 2
        assert math.isclose(snap["SCAN"].sum_seconds, 2.02)
        assert snap["SCAN"].max_seconds == 2.0
        assert sum(snap["SCAN"].buckets) == 2
        for stats in snap.values():
            for value in (stats.count, stats.sum_seconds, stats.max_seconds, *stats.buckets):
                assert isinstance(value, (int, float)) and not isinstance(value, bool)

    @pytest.fixture
    async def harness(self, tmp_path: Path):
        h = await H.make_harness(tmp_path / "obs-lat.sqlite")
        yield h
        await H.teardown_harness(h)

    async def test_worker_records_stage_latencies_without_changing_behavior(
        self, harness, monkeypatch
    ) -> None:
        # Run the SAME ingest twice: once with, once without the latency hook.
        results = []
        for with_hook in (False, True):
            h = await H.make_harness(Path(harness.engine.url.database).with_suffix(
                f".{int(with_hook)}.sqlite"
            ))
            try:
                await H.ingest(h, monkeypatch, H.mailitem("<lat@corp>", H.benign_mime()))
                ticks = iter(range(0, 1000))
                seen: list[tuple[str, float]] = []
                kwargs: dict[str, Any] = {}
                if with_hook:
                    kwargs = {
                        "latency_sink": lambda s, v, seen=seen: seen.append((s, v)),
                        "latency_clock": lambda ticks=ticks: float(next(ticks)),
                    }
                worker = H.security_worker(h, **kwargs)
                row = await worker.process_one()
                assessments = await h.registry.mail_security.list_assessments(h.scope)
                results.append(
                    (
                        row.state,
                        row.attempts,
                        row.last_error_code,
                        [
                            (a.disposition, a.processing_state, a.evidence_fidelity)
                            for a in assessments
                        ],
                    )
                )
                if with_hook:
                    assert [s for s, _ in seen] == [
                        "RESOLVE",
                        "SCAN",
                        "ASSESS",
                        "PERSIST",
                        "TOTAL",
                    ]
                    assert all(isinstance(v, float) and v >= 0 for _, v in seen)
            finally:
                await H.teardown_harness(h)
        assert results[0] == results[1]  # behavior identical with the hook

    async def test_failing_sink_cannot_affect_the_verdict(self, harness, monkeypatch) -> None:
        await H.ingest(harness, monkeypatch, H.mailitem("<lat2@corp>", H.benign_mime()))

        def boom(stage: str, seconds: float) -> None:
            raise RuntimeError("sink down")

        worker = H.security_worker(harness, latency_sink=boom)
        row = await worker.process_one()
        assert row.state == "DONE"


# --------------------------------------------------------------------------- #
# fidelity / completeness / feeds / egress
# --------------------------------------------------------------------------- #


class TestDistributions:
    def test_fidelity_and_completeness(self) -> None:
        rows = [
            _row(),
            _row(fidelity="BODY_ONLY"),
            _row(fidelity="UNKNOWN", state="INCOMPLETE"),  # NOT_RUN
            _row(state="INCOMPLETE", reasons=("INSPECTION_INCOMPLETE",)),
            _row(rule_ids=("ATTACHMENT_SCAN_INCOMPLETE",)),
            _row(reasons=("INSPECTION_INCOMPLETE",)),
            _row(fidelity="free text <script>"),  # collapses to UNKNOWN
        ]
        fidelity, completeness, total = obs.build_assessment_distributions(rows)
        assert total == 7
        assert fidelity["ORIGINAL_RFC822"] == 4
        assert fidelity["BODY_ONLY"] == 1
        assert fidelity["UNKNOWN"] == 2
        assert set(fidelity) == {
            "ORIGINAL_RFC822", "PROVIDER_FULL", "SELECTED_HEADERS_AND_BODY",
            "BODY_ONLY", "METADATA_ONLY", "UNKNOWN",
        }  # fmt: skip
        assert completeness == {
            "COMPLETE": 3,
            "INSPECTION_INCOMPLETE": 2,
            "ATTACHMENT_SCAN_INCOMPLETE": 1,
            "NOT_RUN": 1,
        }

    def test_feed_freshness_version_and_live_counts(self) -> None:
        rows = [
            _feed_row("feed-a", age=100),
            _feed_row("feed-a", age=500, enabled=False),
            _feed_row("feed-a", age=900, ttl=-10),  # expired
            _feed_row("bad feed name with spaces/and\nnewline"),
        ]
        stats = {f.feed: f for f in obs.build_feed_stats(rows, now=_T0)}
        assert set(stats) == {"feed-a", "OTHER"}
        assert stats["feed-a"].live_indicators == 1
        assert stats["feed-a"].total_indicators == 3
        assert stats["feed-a"].age_seconds == 100.0
        assert stats["feed-a"].feed_version == "feed-abc123"

    def test_feed_cardinality_is_bounded(self) -> None:
        rows = [_feed_row(f"f{i}") for i in range(obs.MAX_FEEDS + 10)]
        assert len(obs.build_feed_stats(rows, now=_T0)) <= obs.MAX_FEEDS + 1

    def test_egress_counts_by_reason_only(self) -> None:
        recs = [
            NS(reason="EGRESS_DISABLED", decision="deny", host_hash="h", account_scope="a"),
            NS(reason="EGRESS_DISABLED", decision="deny"),
            NS(reason="https://evil.example/x?token=1", decision="oops"),
        ]
        reasons, decisions = obs.build_egress_counts(recs) or ({}, {})
        assert reasons == {"EGRESS_DISABLED": 2, "OTHER": 1}
        assert decisions == {"deny": 2, "OTHER": 1}
        assert obs.build_egress_counts(None) is None

    def test_every_real_egress_reason_passes_the_shape_gate(self) -> None:
        from backend.mail_security.egress import EgressReason

        recs = [NS(reason=r, decision="deny") for r in EgressReason]
        reasons, _ = obs.build_egress_counts(recs) or ({}, {})
        assert "OTHER" not in reasons and len(reasons) == len(EgressReason)


# --------------------------------------------------------------------------- #
# health transitions and UNKNOWN semantics
# --------------------------------------------------------------------------- #


class TestHealth:
    def test_healthy_snapshot_is_ok_everywhere_it_has_data(self) -> None:
        s = _healthy()
        for stage in (
            obs.HealthStage.QUEUE,
            obs.HealthStage.EVIDENCE_FIDELITY,
            obs.HealthStage.SCAN_COMPLETENESS,
            obs.HealthStage.INTEL_FEEDS,
            obs.HealthStage.SWEEPER,
            obs.HealthStage.EGRESS,
        ):
            assert _status(s, stage) in (obs.HealthStatus.OK, obs.HealthStatus.DISABLED), stage
        assert _status(s, obs.HealthStage.QUEUE) is obs.HealthStatus.OK
        assert _status(s, obs.HealthStage.INTEL_FEEDS) is obs.HealthStatus.OK

    def test_no_data_is_unknown_or_disabled_never_ok(self) -> None:
        s = _snap(sweeper_age=None)
        statuses = {h.stage: h.status for h in obs.compute_health(s, _TH)}
        assert statuses[obs.HealthStage.QUEUE] is obs.HealthStatus.UNKNOWN
        assert statuses[obs.HealthStage.EVIDENCE_FIDELITY] is obs.HealthStatus.UNKNOWN
        assert statuses[obs.HealthStage.SCAN_COMPLETENESS] is obs.HealthStatus.UNKNOWN
        assert statuses[obs.HealthStage.SWEEPER] is obs.HealthStatus.UNKNOWN
        assert statuses[obs.HealthStage.INTEL_FEEDS] is obs.HealthStatus.UNKNOWN  # unreadable
        assert statuses[obs.HealthStage.EGRESS] is obs.HealthStatus.UNKNOWN
        assert obs.HealthStatus.OK not in statuses.values()
        # empty-but-readable feeds / zero egress decisions => DISABLED, not OK
        s2 = _snap(feeds=[], egress=[], sweeper_age=None)
        assert _status(s2, obs.HealthStage.INTEL_FEEDS) is obs.HealthStatus.DISABLED
        assert _status(s2, obs.HealthStage.EGRESS) is obs.HealthStatus.DISABLED

    def test_flag_off_everything_disabled(self) -> None:
        s = obs.disabled_snapshot(_T0)
        assert {h.status for h in obs.compute_health(s, _TH)} == {obs.HealthStatus.DISABLED}
        assert obs.evaluate_alerts(s, _TH) == []

    def test_dead_letter_transitions(self) -> None:
        s = _snap(jobs=[_job("DEAD_LETTER", 5), _job("DONE", 1)])
        assert _status(s, obs.HealthStage.QUEUE) is obs.HealthStatus.DEGRADED
        many = _snap(jobs=[_job("DEAD_LETTER", 5) for _ in range(10)])
        assert _status(many, obs.HealthStage.QUEUE) is obs.HealthStatus.FAILING

    def test_stuck_queue_transitions(self) -> None:
        degraded = _snap(jobs=[_job("PENDING", 0, age=_TH.stuck_pending_seconds + 1)])
        failing = _snap(jobs=[_job("PENDING", 0, age=_TH.stuck_pending_failing_seconds + 1)])
        assert _status(degraded, obs.HealthStage.QUEUE) is obs.HealthStatus.DEGRADED
        assert _status(failing, obs.HealthStage.QUEUE) is obs.HealthStatus.FAILING

    def test_attempt_ceiling_degrades(self) -> None:
        s = _snap(jobs=[_job("ERROR_RETRYABLE", 3)])
        assert _status(s, obs.HealthStage.QUEUE) is obs.HealthStatus.DEGRADED

    def test_stale_feed_transitions(self) -> None:
        stale = _snap(feeds=[_feed_row(age=_TH.feed_stale_seconds + 1)])
        very = _snap(feeds=[_feed_row(age=_TH.feed_failing_seconds + 1)])
        dead = _snap(feeds=[_feed_row(ttl=-5)])
        assert _status(stale, obs.HealthStage.INTEL_FEEDS) is obs.HealthStatus.DEGRADED
        assert _status(very, obs.HealthStage.INTEL_FEEDS) is obs.HealthStatus.FAILING
        assert _status(dead, obs.HealthStage.INTEL_FEEDS) is obs.HealthStatus.DEGRADED

    def test_sweeper_silent_transitions(self) -> None:
        assert (
            _status(_snap(sweeper_age=_TH.sweeper_silent_seconds + 1), obs.HealthStage.SWEEPER)
            is obs.HealthStatus.DEGRADED
        )
        assert (
            _status(_snap(sweeper_age=_TH.sweeper_failing_seconds + 1), obs.HealthStage.SWEEPER)
            is obs.HealthStatus.FAILING
        )

    def test_completeness_regression_transitions(self) -> None:
        rows = [_row() for _ in range(3)] + [
            _row(state="INCOMPLETE", reasons=("INSPECTION_INCOMPLETE",)) for _ in range(2)
        ]
        assert _status(_snap(rows=rows), obs.HealthStage.SCAN_COMPLETENESS) is (
            obs.HealthStatus.DEGRADED
        )
        worse = [_row()] * 2 + [_row(state="INCOMPLETE") for _ in range(3)]
        assert _status(_snap(rows=worse), obs.HealthStage.SCAN_COMPLETENESS) is (
            obs.HealthStatus.FAILING
        )

    def test_egress_failures_degrade(self) -> None:
        recs = [NS(reason="TIMEOUT", decision="fail") for _ in range(5)]
        assert _status(_snap(egress=recs), obs.HealthStage.EGRESS) is obs.HealthStatus.DEGRADED


# --------------------------------------------------------------------------- #
# alerts: each failure fires its alert; healthy fires none
# --------------------------------------------------------------------------- #


def _codes(snapshot: obs.ObservabilitySnapshot) -> set[obs.AlertCode]:
    return {a.code for a in obs.evaluate_alerts(snapshot, _TH)}


class TestAlerts:
    def test_healthy_snapshot_fires_nothing(self) -> None:
        assert obs.evaluate_alerts(_healthy(), _TH) == []

    def test_stuck_queue(self) -> None:
        s = _snap(jobs=[_job("PENDING", 0, age=_TH.stuck_pending_seconds + 1)])
        alerts = obs.evaluate_alerts(s, _TH)
        assert obs.AlertCode.STUCK_QUEUE in {a.code for a in alerts}
        crit = _snap(jobs=[_job("PENDING", 0, age=_TH.stuck_pending_failing_seconds + 1)])
        assert any(
            a.code is obs.AlertCode.STUCK_QUEUE and a.severity is obs.AlertSeverity.CRITICAL
            for a in obs.evaluate_alerts(crit, _TH)
        )

    def test_dead_letter_present(self) -> None:
        assert obs.AlertCode.DEAD_LETTER_PRESENT in _codes(_snap(jobs=[_job("DEAD_LETTER", 5)]))

    def test_stale_feed(self) -> None:
        assert obs.AlertCode.STALE_FEED in _codes(
            _snap(feeds=[_feed_row(age=_TH.feed_stale_seconds + 1)])
        )
        assert obs.AlertCode.STALE_FEED in _codes(_snap(feeds=[_feed_row(ttl=-1)]))

    def test_scan_completeness_regression(self) -> None:
        rows = [_row() for _ in range(2)] + [
            _row(rule_ids=("ATTACHMENT_SCAN_INCOMPLETE",)) for _ in range(3)
        ]
        assert obs.AlertCode.SCAN_COMPLETENESS_REGRESSION in _codes(_snap(rows=rows))
        # below the minimum sample size a tiny sample cannot raise the alert
        assert obs.AlertCode.SCAN_COMPLETENESS_REGRESSION not in _codes(
            _snap(rows=[_row(state="INCOMPLETE")])
        )

    def test_sweeper_silent(self) -> None:
        assert obs.AlertCode.SWEEPER_SILENT in _codes(
            _snap(sweeper_age=_TH.sweeper_silent_seconds + 1)
        )
        # never ran, but work exists
        assert obs.AlertCode.SWEEPER_SILENT in _codes(
            _snap(jobs=[_job("PENDING", 0, age=1)], sweeper_age=None)
        )

    def test_alerts_carry_only_enums_and_numbers(self) -> None:
        s = _snap(jobs=[_job("DEAD_LETTER", 5)], sweeper_age=99999)
        for a in obs.evaluate_alerts(s, _TH):
            assert isinstance(a.code, obs.AlertCode)
            assert isinstance(a.stage, obs.HealthStage)
            assert isinstance(a.severity, obs.AlertSeverity)
            assert isinstance(a.observed, float) and isinstance(a.threshold, float)


# --------------------------------------------------------------------------- #
# safe trace id
# --------------------------------------------------------------------------- #


class TestTraceId:
    def test_deterministic_opaque_and_contains_no_input(self) -> None:
        job_id = "11111111-2222-3333-4444-555555555555"
        t1 = telemetry.safe_trace_id(job_id)
        assert t1 == telemetry.safe_trace_id(job_id)
        assert re.fullmatch(r"tr_[0-9a-f]{24}", t1)
        assert telemetry.safe_trace_id(job_id + "x") != t1
        assert job_id not in t1
        sensitive = "CANARY-msgid-<abc@corp>-user@example.com"
        out = telemetry.safe_trace_id(sensitive)
        for token in ("CANARY", "msgid", "abc", "corp", "user", "example", "@", "<"):
            assert token not in out

    def test_degenerate_inputs(self) -> None:
        assert telemetry.safe_trace_id("") == telemetry.NO_TRACE_ID
        assert telemetry.safe_trace_id(None) == telemetry.NO_TRACE_ID
        assert telemetry.safe_trace_id(123) == telemetry.NO_TRACE_ID


# --------------------------------------------------------------------------- #
# Prometheus publication (existing prometheus_client mechanism)
# --------------------------------------------------------------------------- #


class TestPrometheus:
    def test_publish_uses_only_enum_labels(self) -> None:
        reg = CollectorRegistry()
        snap = _snap(
            jobs=[_job("DEAD_LETTER", 5), _job("PENDING", 0, age=2000)],
            rows=[_row() for _ in range(5)],
            feeds=[_feed_row("feed-a"), _feed_row("bad name/with\nstuff")],
            egress=[NS(reason="EGRESS_DISABLED", decision="deny")],
        )
        assert obs.publish_metrics(snap, _TH, registry=reg) is True
        text = generate_latest(reg).decode()
        assert "nervcenter_mail_security_queue_depth" in text
        assert 'feed="feed-a"' in text and 'feed="OTHER"' in text
        assert "bad name" not in text
        label_values = set(re.findall(r'(\w+)="([^"]*)"', text))
        allowed_keys = {
            "state", "bucket", "fidelity", "status", "feed", "reason", "stage", "code",
        }  # fmt: skip
        assert {k for k, _ in label_values} <= allowed_keys
        assert 'nervcenter_mail_security_alert_active{code="DEAD_LETTER_PRESENT"} 1.0' in text
        # republishing is idempotent (no duplicate registration)
        assert obs.publish_metrics(snap, _TH, registry=reg) is True

    def test_flag_off_publishes_nothing(self) -> None:
        reg = CollectorRegistry()
        assert obs.publish_metrics(obs.disabled_snapshot(_T0), _TH, registry=reg) is False
        assert generate_latest(reg).decode() == ""


# --------------------------------------------------------------------------- #
# full pipeline canary (T-14): ingest -> worker -> sweeper -> observability
# --------------------------------------------------------------------------- #

_CANARIES = {
    "msgid": "CANARYMSGID-7f3a",
    "sender": "canarysender7f3a",
    "subject": "CANARYSUBJECT-7f3a",
    "url": "canaryurl7f3a.example",
    "file": "CANARYFILE-7f3a.pdf",
    "body": "CANARYBODY-7f3a",
}


def _canary_mime() -> bytes:
    c = _CANARIES
    return (
        f"From: Evil <{c['sender']}@canaryhost7f3a.example>\r\n"
        f"To: user@example.com\r\n"
        f"Subject: {c['subject']}\r\n"
        f"Message-ID: <{c['msgid']}@canaryhost7f3a.example>\r\n"
        f"Date: Mon, 27 Oct 2026 09:00:00 +0000\r\n"
        f"MIME-Version: 1.0\r\n"
        f'Content-Type: multipart/mixed; boundary="B7f3a"\r\n'
        f"\r\n"
        f"--B7f3a\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{c['body']} visit http://{c['url']}/login now\r\n"
        f"--B7f3a\r\n"
        f"Content-Type: application/pdf\r\n"
        f'Content-Disposition: attachment; filename="{c["file"]}"\r\n'
        f"Content-Transfer-Encoding: base64\r\n\r\n"
        f"JVBERi0xLjQK\r\n"
        f"--B7f3a--\r\n"
    ).encode()


@pytest.fixture
async def harness(tmp_path: Path):
    h = await H.make_harness(tmp_path / "obs.sqlite")
    yield h
    await H.teardown_harness(h)


def _worker(harness, clock: Any, **kw: Any) -> MailSecurityWorker:
    return MailSecurityWorker(
        repository=harness.registry.mail_security,
        content_resolver=kw.pop(
            "content_resolver", ObjectStoreContentResolver(object_store=harness.store)
        ),
        account_scope=harness.scope,
        analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        clock=clock,
        env=_ON,
        worker_id="obs-w",
        max_attempts=_POLICY.max_attempts,
        lease_seconds=_POLICY.lease_seconds,
        **kw,
    )


class TestCanaryPrivacy:
    async def test_no_identifier_or_content_in_any_telemetry(
        self, harness, monkeypatch
    ) -> None:
        monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
        c = _CANARIES
        item = H.mailitem(
            f"<{c['msgid']}@canaryhost7f3a.example>",
            _canary_mime(),
            sender=f"Evil <{c['sender']}@canaryhost7f3a.example>",
        )
        recorder = telemetry.LatencyRecorder()
        now = [_T0]
        with capture_logs() as logs:
            await H.ingest(harness, monkeypatch, item)
            repo = harness.registry.mail_security
            # one healthy job, one failing job (content missing) to hit warn paths
            w = _worker(harness, lambda: now[0], latency_sink=recorder.observe)
            await w.process_one()
            await H.ingest(
                harness,
                monkeypatch,
                H.mailitem("<CANARYMSGID-second@canaryhost7f3a.example>", H.benign_mime()),
                uid=6,
            )
            saved = dict(harness.store._objects)
            harness.store._objects.clear()
            for _ in range(5):
                now[0] += timedelta(seconds=100_000)
                await w.process_one()
            harness.store._objects.update(saved)
            await sweep_once(repo, harness.scope, now=now[0], policy=_POLICY)
            snapshot = await obs.collect_snapshot(
                repo,
                [harness.scope],
                now=now[0],
                policy=_POLICY,
                egress_records=[NS(reason="EGRESS_DISABLED", decision="deny")],
                latency=recorder,
                env=_ON,
            )
        reg = CollectorRegistry()
        obs.publish_metrics(snapshot, _TH, registry=reg)
        view = obs.health_view(snapshot, _TH)
        alerts = obs.evaluate_alerts(snapshot, _TH)
        assert snapshot.queue.total == 2
        assert snapshot.queue.depth["DONE"] == 1
        assert snapshot.queue.depth["DEAD_LETTER"] == 1  # failure path really ran
        assert recorder.snapshot()["TOTAL"].count >= 2

        jobs = await repo.list_jobs(harness.scope)
        assessments = await repo.list_assessments(harness.scope)
        ids = [harness.scope, harness.conn.id, *(j.id for j in jobs)]
        ids += [j.source_key for j in jobs] + [a.assessment_id for a in assessments]
        ids += [a.source_id for a in assessments]
        # Pre-existing ``mail_ingest.*`` lines (not changed by Task 2.6) carry a
        # connection_id; the Task 2.6 contract covers the mail-security pipeline
        # lines, so id checks use those. Content canaries are checked on ALL logs.
        sec_logs = [e for e in logs if str(e.get("event", "")).startswith("mail_security.")]
        assert sec_logs
        base = [generate_latest(reg).decode(), repr(view), repr(alerts), repr(snapshot)]
        blob = "\n".join([*base, " ".join(str(e) for e in sec_logs)])
        all_blob = "\n".join([*base, " ".join(str(e) for e in logs)])
        content = (*c.values(), "canaryhost7f3a", H.CANARY, H.SECRET)
        for needle in (*content, H.ACCOUNT, *ids):
            assert needle and needle not in blob, f"leak: {needle!r}"
        for needle in content:
            assert needle not in all_blob, f"content leak in logs: {needle!r}"
        # pipeline log lines carry the safe trace id, not the job id
        pipeline = [e for e in logs if str(e.get("event", "")).startswith("mail_security.job_")]
        assert pipeline and all(re.fullmatch(r"tr_[0-9a-f]{24}", e["trace_id"]) for e in pipeline)
        for entry in logs:
            assert "account_scope" not in entry and "job_id" not in entry

    async def test_log_trace_id_correlates_to_job(self, harness, monkeypatch) -> None:
        await H.ingest(harness, monkeypatch, H.mailitem("<t@corp>", H.benign_mime()))
        job = (await harness.registry.mail_security.list_jobs(harness.scope))[0]
        with capture_logs() as logs:
            await _worker(harness, lambda: _T0).process_one()
        done = [e for e in logs if e.get("event") == "mail_security.job_done"]
        assert done and done[0]["trace_id"] == telemetry.safe_trace_id(job.id)


class TestFlagOff:
    async def test_flag_off_collect_does_no_reads_and_heartbeat_is_inert(
        self, harness, monkeypatch
    ) -> None:
        monkeypatch.delenv(ENV_MAIL_SECURITY_ENABLED, raising=False)

        class Boom:
            async def list_jobs(self, *a: Any, **k: Any) -> Any:
                raise AssertionError("repository read while flag off")

            list_assessments = list_jobs

        snap = await obs.collect_snapshot(Boom(), ["x"], now=_T0)
        assert snap.enabled is False and snap.queue.total == 0
        await sweep_once(harness.registry.mail_security, harness.scope, now=_T0)
        assert telemetry.sweeper_age_seconds(_T0) is None  # heartbeat not stamped

    async def test_flag_on_sweep_stamps_heartbeat(self, harness, monkeypatch) -> None:
        monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
        await sweep_once(harness.registry.mail_security, harness.scope, now=_T0)
        assert telemetry.sweeper_age_seconds(_T0 + timedelta(seconds=7)) == 7.0

    async def test_default_worker_has_no_latency_side_effects(
        self, harness, monkeypatch
    ) -> None:
        before = telemetry.LATENCY.snapshot()
        await H.ingest(harness, monkeypatch, H.mailitem("<n@corp>", H.benign_mime()))
        await _worker(harness, lambda: _T0).process_one()
        assert telemetry.LATENCY.snapshot() == before


# --------------------------------------------------------------------------- #
# real intel table read (feed freshness/version end to end)
# --------------------------------------------------------------------------- #


class TestIntelCollection:
    async def test_collect_feed_rows_from_store(self, tmp_path: Path) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'intel.db'}",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sc: MailIntelIndicatorRow.__table__.create(sc, checkfirst=True)
            )
        factory = make_session_factory(engine)
        async with factory() as session:
            session.add(
                MailIntelIndicatorRow(
                    indicator_type="DOMAIN",
                    canonical_indicator="bad.example",
                    source="feed-a",
                    confidence_type="FEED_ASSERTED",
                    license="CC0-1.0",
                    provenance="feed:feed-a:v1",
                    scope="GLOBAL",
                    feed_version="feed-deadbeef",
                    first_seen=_T0 - timedelta(days=1),
                    last_seen=_T0 - timedelta(hours=1),
                    expires_at=_T0 + timedelta(days=1),
                    enabled=True,
                    promotion_version=1,
                    rolled_back=False,
                )
            )
            await session.commit()
        rows = await obs.collect_feed_rows(factory)
        assert rows is not None
        stats = obs.build_feed_stats(rows, now=_T0)
        assert stats[0].feed == "feed-a" and stats[0].live_indicators == 1
        assert stats[0].feed_version == "feed-deadbeef" and stats[0].age_seconds == 3600.0
        # the canonical indicator itself is never part of the stats
        assert "bad.example" not in repr(stats)
        await engine.dispose()

        # unreadable store => None => UNKNOWN, never OK
        def broken() -> Any:
            raise RuntimeError("store down")

        assert await obs.collect_feed_rows(broken) is None


# --------------------------------------------------------------------------- #
# API: GET /api/mail-security/health
# --------------------------------------------------------------------------- #

JWT_SECRET = "unit-secret-" + "a" * 60
JWT_ISSUER = "https://auth.test.local/realms/nervecenter"
ACCOUNT_A = "conn:aaaa1111"
ACCOUNT_B = "conn:bbbb2222"
_SECURITY_TABLES = [
    MailSecurityJobRow.__table__,
    MailEvidenceRow.__table__,
    MailSecurityAssessmentRow.__table__,
    MailSecurityFindingRow.__table__,
]
_HEALTH_ALLOWED = {
    "captured_at", "enabled", "stages", "queue", "fidelity", "completeness",
    "intel_feeds", "egress_reasons", "latency", "sweeper_age_seconds", "alerts",
}  # fmt: skip


def _auth_ctx() -> AuthContext:
    return AuthContext(
        secret=JWT_SECRET,
        issuer=JWT_ISSUER,
        client_id="unit",
        local_admin_enabled=False,
        local_admin_password=None,
        session_factory=None,
    )


class _Env:
    def __init__(self, app: FastAPI, registry: RepositoryRegistry, loop: Any) -> None:
        self.app, self.registry, self._loop = app, registry, loop

    def run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def token(self, scopes: list[str] | None) -> str:
        async def mint() -> str:
            ctx = _auth_ctx()
            extra: dict[str, Any] = {}
            if scopes is not None:
                extra["account_scopes"] = scopes
            token, exp, claims = ctx._mint(
                sub="u", typ="access", roles=["admin"],
                ttl=auth_service.ACCESS_TOKEN_TTL, extra=extra,
            )  # fmt: skip
            await auth_service.record_session(
                ctx, jti=claims["jti"], sub="u", typ="access", expires_at=exp, source="test"
            )
            return token

        return self.run(mint())

    def seed_job(self, scope: str, key: str, state: str, attempts: int = 0) -> None:
        async def go() -> None:
            repo = self.registry.mail_security
            row = await repo.upsert_job(
                account_scope=scope,
                outbox_job_key=key,
                source_key=f"src-{key}",
                analysis_profile_version="2026.01",
                state="PENDING",
                meta={},
            )
            await repo.mark_job_state(scope, row.id, state=state, attempts=attempts)

        self.run(go())


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    auth_service.reset_memory_store()
    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")

    async def mk() -> Any:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'health.db'}",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.run_sync(
                _SECURITY_TABLES[0].metadata.create_all, tables=_SECURITY_TABLES
            )
        return engine

    engine = loop.run_until_complete(mk())
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


def _get(env: _Env, token: str | None) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with TestClient(env.app) as client:
        return client.get("/api/mail-security/health", headers=headers)


class TestHealthEndpoint:
    def test_unauthenticated_rejected(self, api_env) -> None:
        assert _get(api_env, None).status_code == 401

    def test_no_scopes_sees_nothing_not_global_counts(self, api_env) -> None:
        api_env.seed_job(ACCOUNT_A, "k1", "DEAD_LETTER", 5)
        for scopes in (None, []):
            resp = _get(api_env, api_env.token(scopes))
            assert resp.status_code == 404
            assert resp.json()["detail"] == {"error_code": "not_found"}
            assert "DEAD_LETTER" not in resp.text

    def test_cross_account_counts_never_leak(self, api_env, monkeypatch) -> None:
        telemetry.note_sweep(datetime.now(UTC))  # sweeper is alive; isolate dead-letter
        api_env.seed_job(ACCOUNT_A, "a1", "DONE", 1)
        for i in range(3):
            api_env.seed_job(ACCOUNT_B, f"b{i}", "DEAD_LETTER", 5)
        resp = _get(api_env, api_env.token([ACCOUNT_A]))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["queue"]["depth"]["DEAD_LETTER"] == 0  # B's dead letters invisible
        assert data["queue"]["depth"]["DONE"] == 1
        assert not data["alerts"]
        both = _get(api_env, api_env.token([ACCOUNT_A, ACCOUNT_B])).json()["data"]
        assert both["queue"]["depth"]["DEAD_LETTER"] == 3
        assert {a["code"] for a in both["alerts"]} == {"DEAD_LETTER_PRESENT"}
        # an unauthorized account_id-style probing can't widen: no such param exists
        narrow = _get(api_env, api_env.token([ACCOUNT_B])).json()["data"]
        assert narrow["queue"]["depth"]["DONE"] == 0

    def test_only_allow_listed_fields_and_no_identifiers(self, api_env) -> None:
        api_env.seed_job(ACCOUNT_A, "a1", "ERROR_RETRYABLE", 2)
        resp = _get(api_env, api_env.token([ACCOUNT_A]))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == _HEALTH_ALLOWED
        assert ACCOUNT_A not in resp.text and "src-a1" not in resp.text
        statuses = {s["stage"]: s["status"] for s in data["stages"]}
        assert statuses["INTEL_FEEDS"] == "UNKNOWN"  # table absent => unreadable => UNKNOWN
        assert statuses["QUEUE"] == "OK"
        assert statuses["SCAN_COMPLETENESS"] == "UNKNOWN"

    def test_flag_off_reports_disabled_without_reads(self, api_env, monkeypatch) -> None:
        api_env.seed_job(ACCOUNT_A, "a1", "DEAD_LETTER", 5)
        monkeypatch.delenv(ENV_MAIL_SECURITY_ENABLED, raising=False)
        resp = _get(api_env, api_env.token([ACCOUNT_A]))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["enabled"] is False
        assert {s["status"] for s in data["stages"]} == {"DISABLED"}
        assert data["queue"]["depth"]["DEAD_LETTER"] == 0
        assert data["alerts"] == []

    def test_route_is_get_only(self) -> None:
        methods = {
            m
            for r in mail_security.router.routes
            if getattr(r, "path", "") == "/api/mail-security/health"
            for m in getattr(r, "methods", set())
        }
        assert methods == {"GET"}


def test_modules_import_no_network_or_new_observability_dependency() -> None:
    for mod in (obs, telemetry):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for banned in ("import socket", "import httpx", "import requests", "aiohttp", "urllib"):
            assert banned not in src
    assert structlog is not None
