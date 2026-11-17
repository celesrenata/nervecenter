"""Kube agent settings, issue ledger, overrides and reports (NS-ADR-120).

``kube_settings``: single row (``id`` = ``default``) the owner can tune. ``kube_issues``: one row
per stable issue key. ``kube_overrides``: time-limited permission to change one protected
namespace for one issue. ``kube_reports``: the last snapshots and waste reports.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Jsonb, UUIDv4

DEFAULT_PROTECTED = ["nervecenter", "omniroute", "harbor-service", "kube-system",
                     "longhorn-system", "argocd"]
DEFAULT_KNOWN_DOWN = ["stonks-beta", "comfyui-service"]


class KubeSettings(Base):
    __tablename__ = "kube_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    protected_namespaces: Mapped[list[Any]] = mapped_column(
        Jsonb, nullable=False, default=lambda: list(DEFAULT_PROTECTED))
    known_down: Mapped[list[Any]] = mapped_column(
        Jsonb, nullable=False, default=lambda: list(DEFAULT_KNOWN_DOWN))
    override_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    retention_daily: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    retention_weekly: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    backup_nfs_server: Mapped[str] = mapped_column(String(200), nullable=False,
                                                   default="192.168.42.8")
    backup_nfs_base: Mapped[str] = mapped_column(String(300), nullable=False,
                                                 default="/volume1/Kubernetes")
    daily_run_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    issue_cooldown_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    #: Optional Prometheus base URL (prometheus-service); null = not used
    prometheus_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: True until the owner enables proposals: the agent only diagnoses (FEAT-004 clears it)
    diagnose_only_until_first_report: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_waste_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KubeIssue(Base):
    __tablename__ = "kube_issues"

    id: Mapped[UUIDv4]
    key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    namespace: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    #: known_down | diagnose_only | normal
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    #: open | resolved
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_agent_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(Jsonb, nullable=False, default=dict)


class KubeOverride(Base):
    __tablename__ = "kube_overrides"

    id: Mapped[UUIDv4]
    issue_key: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    namespace: Mapped[str] = mapped_column(String(100), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KubeReport(Base):
    __tablename__ = "kube_reports"

    id: Mapped[UUIDv4]
    #: snapshot | waste
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(Jsonb, nullable=False, default=dict)
