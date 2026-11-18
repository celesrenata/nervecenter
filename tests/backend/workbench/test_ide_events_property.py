"""Property 25: IDE lifecycle events are logged and counted once.

Validates: Requirements 9.6
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import given, settings, strategies as st
from structlog.testing import capture_logs

from backend.workbench.ide import (
    EVENT_TYPES,
    IdeConfig,
    IdeManager,
    IdeNotFound,
    IdeProject,
    IdeStartFailed,
    emit_event,
    ide_events_total,
)
from backend.workbench.kubeclient import KubeError

_DIGEST = "registry.local/ide@sha256:" + "a" * 64
_EXPECTED_KEYS = {"event", "log_level", "event_type", "project_id", "ide_id", "at"}
_SECRET_FILE = "secret-file-name.py"
_SECRET_BODY = "FILE-CONTENTS-SENTINEL"

_event = st.sampled_from(sorted(EVENT_TYPES))
_ident = st.from_regex(r"[a-z0-9][a-z0-9-]{0,20}", fullmatch=True)


def _count(event_type: str) -> float:
    return ide_events_total.labels(event_type)._value.get()


def _counts() -> dict[str, float]:
    return {t: _count(t) for t in EVENT_TYPES}


def _assert_entry(entry: dict[str, Any], event_type: str, project: str, ide: str | None) -> None:
    assert set(entry) == _EXPECTED_KEYS
    assert entry["event_type"] == f"ide.{event_type}"
    assert entry["project_id"] == project
    if ide is not None:
        assert entry["ide_id"] == ide
    assert datetime.fromisoformat(entry["at"]).tzinfo is not None
    blob = repr(entry)
    assert _SECRET_FILE not in blob and _SECRET_BODY not in blob


@settings(max_examples=100, deadline=None)
@given(events=st.lists(st.tuples(_event, _ident, _ident), max_size=15))
def test_emit_event_logs_and_counts_once_each(events):
    before = _counts()
    with capture_logs() as logs:
        for et, pid, iid in events:
            emit_event(et, pid, iid)
    entries = [e for e in logs if str(e.get("event", "")).startswith("ide.")]
    assert len(entries) == len(events)
    for entry, (et, pid, iid) in zip(entries, events, strict=True):
        _assert_entry(entry, et, pid, iid)
    after = _counts()
    for t in EVENT_TYPES:
        assert after[t] - before[t] == sum(1 for e in events if e[0] == t)


class FakeKube:
    """In-memory kube client; ``fail_create`` makes pod creation fail."""

    def __init__(self, fail_create: bool = False) -> None:
        self.pods: dict[str, dict[str, Any]] = {}
        self.fail_create = fail_create
        self._n = 0

    async def list_pods(self, selector: str) -> list[dict[str, Any]]:
        return list(self.pods.values())

    async def create_pod(self, spec: dict[str, Any]) -> dict[str, Any]:
        if self.fail_create:
            raise KubeError(500, "boom")
        self._n += 1
        name = f"ide-pod-{self._n}"
        labels = dict(spec["metadata"].get("labels") or {})
        pod = {
            "metadata": {"name": name, "uid": f"uid{self._n}", "labels": labels},
            "status": {
                "phase": "Running",
                "podIP": "10.0.0.1",
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
        self.pods[name] = pod
        return pod

    async def create_secret(self, body: dict[str, Any]) -> dict[str, Any]:
        return body

    async def get_pod(self, name: str) -> dict[str, Any]:
        return self.pods[name]

    async def delete_pod(self, name: str) -> None:
        self.pods.pop(name, None)

    async def delete_secret(self, name: str) -> None:
        return None

    async def list_events(self, selector: str) -> list[dict[str, Any]]:
        return [{"message": "event"}]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2025, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def _noop(_: float) -> None:
    return None


async def _key(_: str) -> str:
    return "k"


def _manager(kube: FakeKube, clock: Clock) -> IdeManager:
    return IdeManager(
        kube,  # type: ignore[arg-type]
        IdeConfig(image=_DIGEST),
        key_issuer=_key,
        clock=clock,
        sleep=_noop,
    )


@settings(max_examples=40, deadline=None)
@given(
    slug=_ident,
    kind=st.sampled_from(["code", "ops"]),
    outcome=st.sampled_from(["start_stop", "start_idle", "start_fail", "stop_missing"]),
)
def test_manager_operations_emit_exactly_one_event(slug, kind, outcome):
    kube = FakeKube(fail_create=outcome == "start_fail")
    clock = Clock()
    mgr = _manager(kube, clock)
    project = IdeProject(slug=slug, kind=kind)
    before = _counts()

    async def run() -> list[str]:
        expected: list[str] = []
        if outcome == "start_fail":
            try:
                await mgr.start(project)
            except IdeStartFailed:
                pass
            expected.append("failed")
        elif outcome == "stop_missing":
            try:
                await mgr.stop(slug)
            except IdeNotFound:
                pass
        else:
            await mgr.start(project)
            expected.append("started")
            if outcome == "start_stop":
                await mgr.stop(slug)
                expected.append("stopped")
            else:
                clock.now += timedelta(minutes=61)
                assert len(await mgr.reap_once()) == 1
                expected.append("idle_stopped")
        return expected

    with capture_logs() as logs:
        expected = asyncio.run(run())
    entries = [e for e in logs if str(e.get("event", "")).startswith("ide.") and "event_type" in e]
    assert [e["event_type"] for e in entries] == [f"ide.{t}" for t in expected]
    for entry, t in zip(entries, expected, strict=True):
        _assert_entry(entry, t, slug, None)
    after = _counts()
    for t in EVENT_TYPES:
        assert after[t] - before[t] == expected.count(t)
