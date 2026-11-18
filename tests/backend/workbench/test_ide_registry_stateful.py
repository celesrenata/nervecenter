"""Property 15: IDE registry limits (stateful).

# Feature: opencode-menagerie-workbench, Property 15: IDE registry limits
**Validates: Requirements 5.10, 5.13, 9.7**
"""
from __future__ import annotations

import asyncio
from typing import Any

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from backend.workbench.ide import (
    IdeConfig,
    IdeLimitReached,
    IdeManager,
    IdeNotFound,
    IdeProject,
)
from backend.workbench.kubeclient import KubeError

IMAGE = "registry.local/ide@sha256:" + "a" * 64
SLUGS = ["alpha", "beta", "gamma", "delta"]


class FakeCluster:
    """In-memory cluster. Pods are Running and Ready as soon as created."""

    def __init__(self) -> None:
        self.pods: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, dict[str, Any]] = {}
        self.created = 0
        self.max_pods_seen = 0

    async def list_pods(self, selector: str) -> list[dict[str, Any]]:
        return [dict(p) for p in self.pods.values()]

    async def get_pod(self, name: str) -> dict[str, Any]:
        if name not in self.pods:
            raise KubeError(404, "not found")
        return dict(self.pods[name])

    async def create_pod(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.created += 1
        name = f"ide-{self.created}"
        meta = dict(spec["metadata"])
        meta.pop("generateName", None)
        meta.update(name=name, uid=f"uid-{self.created}")
        pod = {
            "metadata": meta,
            "status": {
                "phase": "Running",
                "podIP": f"10.0.0.{self.created}",
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
        self.pods[name] = pod
        self.max_pods_seen = max(self.max_pods_seen, len(self.pods))
        return dict(pod)

    async def delete_pod(self, name: str) -> None:
        if self.pods.pop(name, None) is None:
            raise KubeError(404, "not found")

    async def create_secret(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.secrets[spec["metadata"]["name"]] = spec
        return spec

    async def delete_secret(self, name: str) -> None:
        self.secrets.pop(name, None)

    async def list_events(self, selector: str) -> list[dict[str, Any]]:
        return []


async def _key(slug: str) -> str:
    return "test-key"


def _manager(cluster: FakeCluster) -> IdeManager:
    return IdeManager(
        cluster,  # type: ignore[arg-type]
        IdeConfig(image=IMAGE),
        key_issuer=_key,
        sleep=_nosleep,
    )


async def _nosleep(_: float) -> None:
    return None


def _labels(pod: dict[str, Any]) -> tuple[str, str]:
    lab = pod["metadata"]["labels"]
    return lab["workbench/project"], lab["workbench/kind"]


class IdeRegistryMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.cluster = FakeCluster()
        self.mgr = _manager(self.cluster)
        self.model: dict[str, str] = {}  # slug -> pod name

    def teardown(self) -> None:
        self.loop.close()

    def run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    @rule(slug=st.sampled_from(SLUGS))
    def start(self, slug: str) -> None:
        before = self.cluster.created
        project = IdeProject(slug, "personal")
        if slug in self.model:
            inst = self.run(self.mgr.start(project))
            assert inst.pod_name == self.model[slug]
            assert self.cluster.created == before
        elif len(self.model) >= 2:
            try:
                self.run(self.mgr.start(project))
            except IdeLimitReached:
                pass
            else:
                raise AssertionError("third start was not rejected")
            assert self.cluster.created == before
        else:
            inst = self.run(self.mgr.start(project))
            assert self.cluster.created == before + 1
            self.model[slug] = inst.pod_name

    @rule(slug=st.sampled_from(SLUGS))
    def stop(self, slug: str) -> None:
        if slug in self.model:
            self.run(self.mgr.stop(slug))
            del self.model[slug]
        else:
            try:
                self.run(self.mgr.stop(slug))
            except IdeNotFound:
                pass
            else:
                raise AssertionError("stop of absent IDE succeeded")

    @rule()
    def restart_manager(self) -> None:
        pods_before = set(self.cluster.pods)
        self.mgr = _manager(self.cluster)
        rebuilt = self.run(self.mgr.rediscover())
        assert {(i.slug, i.pod_name) for i in rebuilt} == {
            (s, p) for s, p in self.model.items()
        }
        assert set(self.cluster.pods) == pods_before

    @invariant()
    def limits_hold(self) -> None:
        pods = list(self.cluster.pods.values())
        assert len(pods) <= 2
        assert self.cluster.max_pods_seen <= 2
        projects = [_labels(p) for p in pods]
        assert len(projects) == len(set(projects))
        assert {s: p["metadata"]["name"] for p in pods for s, _ in [_labels(p)]} == self.model

    @invariant()
    def listing_matches_pods(self) -> None:
        listed = self.run(self.mgr.list())
        assert {(i.slug, i.kind): i.pod_name for i in listed} == {
            _labels(p): p["metadata"]["name"] for p in self.cluster.pods.values()
        }


IdeRegistryMachine.TestCase.settings = settings(
    max_examples=100,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=list(HealthCheck),
)

TestIdeRegistryLimits = IdeRegistryMachine.TestCase
