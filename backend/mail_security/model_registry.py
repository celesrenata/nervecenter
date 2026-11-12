"""Model registry with lineage, staged deployment and rollback (NerveShield task 6.5).

Library only; nothing in the service uses it yet. Rules (R-016.3-5, R-021):

* **No auto-promote.** Every forward transition (REGISTERED -> SHADOW -> CANARY ->
  ACTIVE) needs an explicit human approval reference and passes policy checks.
  There is no method that advances a stage on its own; good metrics never
  promote anything.
* **Automatic rollback only.** Recording an observation window that regresses
  against the policy rolls the model back immediately and restores the last
  known-good ACTIVE model. Rollback only ever reduces exposure.
* **Lineage.** Each record binds artifact digests, the signed dataset manifest
  digest and signing key id, benchmark/privacy report digests, the training
  config and the promotion-gate decision. Records are immutable.
* **Tamper evident.** All changes go through an append-only event log; each
  event carries the hash of the previous one. ``verify_log`` detects edits,
  insertions and deletions.
* **Canary routing.** A deterministic hash of the message key decides whether
  a message is in the canary fraction; shadow models never affect decisions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

REGISTRY_VERSION = "mail-model-registry@1"
POLICY_VERSION = "mail-model-deploy-policy@1"
_REF_RE = re.compile(r"^[A-Za-z0-9:_.@-]{1,200}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
#: A dataset manifest must be signed (6.2): SSH fingerprint id or file-key id.
_SIGNING_KEY_ID = re.compile(r"^(ssh:SHA256:[A-Za-z0-9+/]{43}|[0-9a-f]{16})$")
_GENESIS = "0" * 64


class Stage(StrEnum):
    REGISTERED = "REGISTERED"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"
    ROLLED_BACK = "ROLLED_BACK"
    RETIRED = "RETIRED"
    BLOCKED = "BLOCKED"


_FORWARD = {Stage.REGISTERED: Stage.SHADOW, Stage.SHADOW: Stage.CANARY, Stage.CANARY: Stage.ACTIVE}


class RegistryError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Lineage:
    artifact_sha256: Mapping[str, str]  # file name -> sha256
    dataset_manifest_sha256: str
    dataset_signing_key_id: str
    benchmark_report_sha256: str
    privacy_report_sha256: str
    training_config: Mapping[str, Any]
    base_model: str
    code_commit: str


@dataclass(frozen=True)
class ModelRecord:
    model_id: str
    candidate: str
    lineage: Lineage
    gate: Mapping[str, Any]  # promotion_gate.GateDecision.to_dict()
    registered_at: str


@dataclass(frozen=True)
class DeployPolicy:
    """Window thresholds. A window regresses if any check fails."""

    version: str = POLICY_VERSION
    min_window_messages: int = 500
    max_fpr: float = 0.01  # on human-reviewed benign mail in the window
    max_fpr_increase_vs_active: float = 0.002
    min_recall_vs_active_ratio: float = 0.97  # on human-confirmed malicious mail
    max_error_rate: float = 0.005  # inference errors / timeouts
    max_latency_p95_ms: float = 500.0
    max_ece: float = 0.05
    canary_fraction: float = 0.05


@dataclass(frozen=True)
class WindowMetrics:
    messages: int
    fpr: float
    recall: float
    error_rate: float
    latency_p95_ms: float
    ece: float


@dataclass(frozen=True)
class Approval:
    approver_ref: str
    approval_ref: str
    at: datetime


@dataclass
class _State:
    stage: Stage
    windows: list[dict[str, Any]] = field(default_factory=list)
    last_window_ok: bool = False


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _record_digest(record: ModelRecord) -> str:
    return hashlib.sha256(_canonical(asdict(record)).encode()).hexdigest()


class ModelRegistry:
    def __init__(self, policy: DeployPolicy | None = None) -> None:
        self.policy = policy or DeployPolicy()
        self._records: dict[str, ModelRecord] = {}
        self._state: dict[str, _State] = {}
        self._events: list[dict[str, Any]] = []
        self._active: str | None = None
        self._last_good: list[str] = []  # ACTIVE history, newest last

    # -- log -----------------------------------------------------------------------------

    def _append(self, kind: str, model_id: str, data: Mapping[str, Any], at: datetime) -> None:
        prev = self._events[-1]["hash"] if self._events else _GENESIS
        body = {
            "seq": len(self._events),
            "kind": kind,
            "model_id": model_id,
            "data": dict(data),
            "at": at.isoformat(),
            "prev": prev,
        }
        body["hash"] = hashlib.sha256(_canonical(body).encode()).hexdigest()
        self._events.append(body)

    @property
    def events(self) -> list[dict[str, Any]]:
        return json.loads(_canonical(self._events))  # deep copy

    @property
    def head(self) -> str:
        """Hash of the latest event. Store it elsewhere (e.g. signed) to detect truncation."""
        return self._events[-1]["hash"] if self._events else _GENESIS

    @staticmethod
    def verify_log(events: list[dict[str, Any]], expected_head: str | None = None) -> bool:
        """Chain check. Without ``expected_head`` a truncated tail still verifies."""
        prev = _GENESIS
        for seq, ev in enumerate(events):
            body = {k: v for k, v in ev.items() if k != "hash"}
            if ev.get("seq") != seq or ev.get("prev") != prev:
                return False
            if hashlib.sha256(_canonical(body).encode()).hexdigest() != ev.get("hash"):
                return False
            prev = ev["hash"]
        return expected_head is None or prev == expected_head

    # -- register ------------------------------------------------------------------------

    def register(self, record: ModelRecord, at: datetime) -> Stage:
        if not _REF_RE.fullmatch(record.model_id) or record.model_id in self._records:
            raise RegistryError("INVALID_OR_DUPLICATE_MODEL_ID")
        lin = record.lineage
        digests = [
            lin.dataset_manifest_sha256,
            lin.benchmark_report_sha256,
            lin.privacy_report_sha256,
            *lin.artifact_sha256.values(),
        ]
        if not lin.artifact_sha256 or not all(
            isinstance(d, str) and _HEX64.fullmatch(d) for d in digests
        ):
            raise RegistryError("LINEAGE_INCOMPLETE")
        if not _SIGNING_KEY_ID.fullmatch(lin.dataset_signing_key_id):
            raise RegistryError("DATASET_MANIFEST_NOT_SIGNED")
        if not _REF_RE.fullmatch(lin.code_commit):
            raise RegistryError("LINEAGE_INCOMPLETE")
        outcome = record.gate.get("outcome") if isinstance(record.gate, Mapping) else None
        if outcome not in ("BLOCKED", "ELIGIBLE_FOR_HUMAN_REVIEW"):
            raise RegistryError("GATE_DECISION_MISSING")
        stage = Stage.REGISTERED if outcome == "ELIGIBLE_FOR_HUMAN_REVIEW" else Stage.BLOCKED
        self._records[record.model_id] = record
        self._state[record.model_id] = _State(stage)
        self._append(
            "REGISTERED",
            record.model_id,
            {
                "record_sha256": _record_digest(record),
                "stage": stage.value,
                "gate_reasons": list(record.gate.get("reasons", [])),
            },
            at,
        )
        return stage

    def stage(self, model_id: str) -> Stage:
        return self._state[model_id].stage

    @property
    def active(self) -> str | None:
        return self._active

    # -- human-approved forward transitions ----------------------------------------------

    def approve_transition(self, model_id: str, target: Stage, approval: Approval) -> Stage:
        """The only way forward. Never called by the registry itself."""
        state = self._state.get(model_id)
        if state is None:
            raise RegistryError("UNKNOWN_MODEL")
        if not (
            _REF_RE.fullmatch(approval.approver_ref) and _REF_RE.fullmatch(approval.approval_ref)
        ):
            raise RegistryError("INVALID_APPROVAL")
        if _FORWARD.get(state.stage) is not target:
            raise RegistryError(f"ILLEGAL_TRANSITION_{state.stage.value}_TO_{target.value}")
        if (
            target is Stage.SHADOW
            and self._records[model_id].gate.get("outcome") != "ELIGIBLE_FOR_HUMAN_REVIEW"
        ):
            raise RegistryError("GATE_BLOCKS_SHADOW")
        if target in (Stage.CANARY, Stage.ACTIVE) and not state.last_window_ok:
            raise RegistryError("NO_PASSING_WINDOW_IN_CURRENT_STAGE")
        if target is Stage.ACTIVE and self._active is not None:
            self._state[self._active].stage = Stage.RETIRED
            self._append("RETIRED", self._active, {"replaced_by": model_id}, approval.at)
        state.stage = target
        state.last_window_ok = False  # each stage needs its own passing window
        if target is Stage.ACTIVE:
            self._active = model_id
            self._last_good.append(model_id)
        self._append(
            "TRANSITION",
            model_id,
            {
                "to": target.value,
                "approver": approval.approver_ref,
                "approval_ref": approval.approval_ref,
            },
            approval.at,
        )
        return target

    # -- monitoring and automatic rollback -----------------------------------------------

    def _active_window(self) -> dict[str, Any] | None:
        if self._active is None:
            return None
        windows = self._state[self._active].windows
        return windows[-1]["metrics"] if windows else None

    def check_window(self, metrics: WindowMetrics) -> list[str]:
        p = self.policy
        problems = []
        if metrics.messages < p.min_window_messages:
            problems.append("WINDOW_TOO_SMALL")
        if metrics.fpr > p.max_fpr:
            problems.append("FPR_ABOVE_LIMIT")
        if metrics.error_rate > p.max_error_rate:
            problems.append("ERROR_RATE_ABOVE_LIMIT")
        if metrics.latency_p95_ms > p.max_latency_p95_ms:
            problems.append("LATENCY_ABOVE_LIMIT")
        if metrics.ece > p.max_ece:
            problems.append("CALIBRATION_DRIFT")
        ref = self._active_window()
        if ref is not None:
            if metrics.fpr > ref["fpr"] + p.max_fpr_increase_vs_active:
                problems.append("FPR_WORSE_THAN_ACTIVE")
            if ref["recall"] > 0 and metrics.recall < ref["recall"] * p.min_recall_vs_active_ratio:
                problems.append("RECALL_WORSE_THAN_ACTIVE")
        return problems

    def record_window(self, model_id: str, metrics: WindowMetrics, at: datetime) -> Stage:
        """Record observed metrics. A regression rolls back; success never promotes."""
        state = self._state.get(model_id)
        if state is None:
            raise RegistryError("UNKNOWN_MODEL")
        if state.stage not in (Stage.SHADOW, Stage.CANARY, Stage.ACTIVE):
            raise RegistryError("NOT_DEPLOYED")
        problems = self.check_window(metrics)
        insufficient = problems == ["WINDOW_TOO_SMALL"]
        state.windows.append(
            {"metrics": asdict(metrics), "problems": problems, "at": at.isoformat()}
        )
        self._append(
            "WINDOW",
            model_id,
            {"stage": state.stage.value, "metrics": asdict(metrics), "problems": problems},
            at,
        )
        if not problems:
            state.last_window_ok = True
            return state.stage
        state.last_window_ok = False
        if insufficient:
            return state.stage  # not enough evidence: nothing changes either way
        return self._rollback(model_id, problems, at)

    def _rollback(self, model_id: str, problems: list[str], at: datetime) -> Stage:
        state = self._state[model_id]
        was = state.stage
        state.stage = Stage.ROLLED_BACK
        restored = None
        if was is Stage.ACTIVE:
            self._last_good = [m for m in self._last_good if m != model_id]
            self._active = None
            for candidate in reversed(self._last_good):
                if self._state[candidate].stage is Stage.RETIRED:
                    self._state[candidate].stage = Stage.ACTIVE
                    self._active = candidate
                    restored = candidate
                    break
        self._append(
            "ROLLED_BACK",
            model_id,
            {"from": was.value, "problems": problems, "restored_active": restored},
            at,
        )
        return Stage.ROLLED_BACK

    # -- routing -------------------------------------------------------------------------

    def route(self, message_key: str) -> dict[str, str | None]:
        """Which model decides and which only observes, for one message."""
        canary = next((m for m, s in self._state.items() if s.stage is Stage.CANARY), None)
        shadow = [m for m, s in self._state.items() if s.stage is Stage.SHADOW]
        decide = self._active
        if canary is not None:
            bucket = int(hashlib.sha256(f"canary\x1f{message_key}".encode()).hexdigest()[:8], 16)
            if bucket / 0xFFFFFFFF < self.policy.canary_fraction:
                decide = canary
        return {"decides": decide, "observes": ",".join(sorted(shadow)) or None}

    def export(self) -> dict[str, Any]:
        return {
            "version": REGISTRY_VERSION,
            "policy": asdict(self.policy),
            "records": {k: asdict(v) for k, v in sorted(self._records.items())},
            "stages": {k: v.stage.value for k, v in sorted(self._state.items())},
            "active": self._active,
            "events": self.events,
        }


__all__ = [
    "POLICY_VERSION",
    "REGISTRY_VERSION",
    "Approval",
    "DeployPolicy",
    "Lineage",
    "ModelRecord",
    "ModelRegistry",
    "RegistryError",
    "Stage",
    "WindowMetrics",
]
