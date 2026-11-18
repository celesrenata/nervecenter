"""IDE failure-injection tests (fake kube client).

Validates: Requirements 5.10, 5.11, 9.8
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backend.workbench.ide import (
    IdeConfig,
    IdeLimitReached,
    IdeManager,
    IdeProject,
    IdeStartFailed,
)
from backend.workbench.kubeclient import KubeError

IMAGE = "registry.local/ide@sha256:" + "a" * 64


class FakeKube:
    def __init__(self) -> None:
        self.pods: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.events: list[dict[str, Any]] = []
        self.ready = False
        self.fail_create_pod: KubeError | None = None
        self.fail_create_secret: KubeError | None = None
        self.fail_get_pod: KubeError | None = None
        self._n = 0

    def add_running(self, slug: str, kind: str = "code") -> str:
        self._n += 1
        name = f"ide-pre{self._n}"
        self.pods[name] = {
            "metadata": {"name": name, "labels": {
                "app": "ide", "workbench/project": slug, "workbench/kind": kind}},
            "status": {"phase": "Running", "podIP": "10.0.0.9"},
        }
        return name

    async def list_pods(self, selector: str = "") -> list[dict[str, Any]]:
        return list(self.pods.values())

    async def create_pod(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_pod", ""))
        if self.fail_create_pod:
            raise self.fail_create_pod
        self._n += 1
        name = f"ide-new{self._n}"
        pod = {
            "metadata": {**spec["metadata"], "name": name, "uid": f"u{self._n}"},
            "status": {"phase": "Pending"},
        }
        self.pods[name] = pod
        return pod

    async def create_secret(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_secret", spec["metadata"]["name"]))
        if self.fail_create_secret:
            raise self.fail_create_secret
        self.secrets[spec["metadata"]["name"]] = spec
        return spec

    async def get_pod(self, name: str) -> dict[str, Any]:
        if self.fail_get_pod:
            raise self.fail_get_pod
        pod = self.pods[name]
        if self.ready:
            pod = {**pod, "status": {
                "phase": "Running", "podIP": "10.0.0.5",
                "conditions": [{"type": "Ready", "status": "True"}]}}
        return pod

    async def list_events(self, selector: str = "") -> list[dict[str, Any]]:
        return self.events

    async def delete_pod(self, name: str) -> None:
        self.calls.append(("delete_pod", name))
        self.pods.pop(name, None)

    async def delete_secret(self, name: str) -> None:
        self.calls.append(("delete_secret", name))
        self.secrets.pop(name, None)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def _issue_key(slug: str) -> str:
    return "test-key"


def _manager(kube: FakeKube, **kw: Any) -> IdeManager:
    clock = Clock()
    return IdeManager(
        kube,  # type: ignore[arg-type]
        IdeConfig(image=IMAGE),
        key_issuer=_issue_key,
        clock=clock,
        sleep=clock.sleep,
        **kw,
    )


async def test_not_ready_start_deletes_pod_then_secret_and_returns_last_event():
    kube = FakeKube()
    kube.events = [{"message": "pulling image"}, {"message": "ImagePullBackOff: boom"}]
    mgr = _manager(kube, start_timeout=5.0, poll_interval=1.0)
    with pytest.raises(IdeStartFailed) as ei:
        await mgr.start(IdeProject("alpha", "code"))
    assert ei.value.message == "ImagePullBackOff: boom"
    deletes = [c[0] for c in kube.calls if c[0].startswith("delete_")]
    assert deletes == ["delete_pod", "delete_secret"]
    assert not kube.pods and not kube.secrets


async def test_third_start_rejected_without_creating_pod():
    kube = FakeKube()
    kube.add_running("one")
    kube.add_running("two", "agent")
    mgr = _manager(kube)
    with pytest.raises(IdeLimitReached):
        await mgr.start(IdeProject("three", "code"))
    assert not [c for c in kube.calls if c[0] == "create_pod"]
    assert len(kube.pods) == 2


async def test_create_pod_api_error_leaves_no_orphans():
    kube = FakeKube()
    kube.fail_create_pod = KubeError(500, "boom")
    mgr = _manager(kube)
    with pytest.raises(IdeStartFailed):
        await mgr.start(IdeProject("alpha", "code"))
    assert not kube.pods and not kube.secrets


async def test_create_secret_api_error_deletes_pod():
    kube = FakeKube()
    kube.fail_create_secret = KubeError(500, "boom")
    mgr = _manager(kube)
    with pytest.raises(IdeStartFailed):
        await mgr.start(IdeProject("alpha", "code"))
    assert not kube.pods and not kube.secrets


async def test_get_pod_api_errors_during_wait_leave_no_orphans():
    kube = FakeKube()
    kube.fail_get_pod = KubeError(503, "unavailable")
    mgr = _manager(kube, start_timeout=3.0, poll_interval=1.0)
    with pytest.raises(IdeStartFailed):
        await mgr.start(IdeProject("alpha", "code"))
    assert not kube.pods and not kube.secrets


async def test_stop_only_deletes_pod_and_performs_no_file_ops(monkeypatch, tmp_path):
    import builtins
    import os
    import shutil

    kube = FakeKube()
    kube.add_running("alpha")
    mgr = _manager(kube)
    project_file = tmp_path / "alpha" / "main.py"
    project_file.parent.mkdir()
    project_file.write_text("print(1)")

    def forbid(*a: Any, **k: Any) -> None:
        raise AssertionError("file operation during stop")

    for target in (os.remove, os.unlink, os.rmdir, os.rename, os.replace):
        monkeypatch.setattr(os, target.__name__, forbid)
    monkeypatch.setattr(shutil, "rmtree", forbid)
    monkeypatch.setattr(builtins, "open", forbid)

    await mgr.stop("alpha")
    assert [c for c in kube.calls] == [("delete_pod", "ide-pre1")]
    monkeypatch.undo()
    assert project_file.read_text() == "print(1)"
