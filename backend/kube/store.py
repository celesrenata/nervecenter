"""Service accessor for the kube agent (NS-ADR-120): the stores the agent tools read from."""
from __future__ import annotations

from typing import Any

from backend.kube.issues import IssueStore, ReportStore
from backend.kube.settings import KubeOverrideStore, KubeSettingsStore


class KubeServices:
    """Settings, overrides, issue ledger and reports on one session factory."""

    def __init__(self, session_factory: Any) -> None:
        self.settings = KubeSettingsStore(session_factory)
        self.overrides = KubeOverrideStore(session_factory, self.settings)
        self.issues = IssueStore(session_factory)
        self.reports = ReportStore(session_factory)


def get_services(app: Any) -> KubeServices | None:
    svc = getattr(app.state, "kube_services", None)
    if svc is not None:
        return svc
    registry = getattr(app.state, "repository_registry", None)
    if registry is None:
        return None
    svc = KubeServices(registry.session_factory)
    app.state.kube_services = svc
    return svc


async def kubernetes_connector(infra: Any) -> str | None:
    """Name of the first kubernetes connector (the cluster the agent looks after)."""
    conns = [c for c in await infra.list_connectors() if c.get("kind") == "kubernetes"]
    return str(conns[0]["name"]) if conns else None


__all__ = ["KubeServices", "get_services", "kubernetes_connector"]
