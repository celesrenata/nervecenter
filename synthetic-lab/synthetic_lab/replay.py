"""Deterministic replay of the synthetic corpus against the local planes.

Replay is **fixture-only and offline** (T003.AC10): it drives in-process
services (the R103 detection service with assist off, the R108 attention
policy service, and — when the T003.AC9 ask pipeline + recorded-fixture
interface exist — the ask path). No network, no secrets, no clock reads:
every time-sensitive call is pinned to the corpus ``anchor``.

Replay produces *observations* (plain JSON-safe dicts), not verdicts —
the :mod:`synthetic_lab.evaluator` compares observations against case
expectations and reports pass/fail with evidence.

Ask-plane contract (T003.AC9): replay checks
``synthetic-lab/ask_fixture.json`` (path overridable). If it exists, it is
loaded and each ask case's ``expect`` block is applied to the recorded
answer (keys ``answer_contains`` / ``provenance_ids`` / ``grounded``). If
it does not exist — which is the current state, since the ask pipeline
returns the honest 202 ACK stub and has no recorded-fixture interface —
every ask case replays as ``pending`` with ``pending_reason``; no
answer, QA verdict, or provenance is invented.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import backend.api.app  # noqa: F401  (resolves the attention↔api circular import)
from backend.api.models import AttentionItem, AttentionState
from backend.attention.policy import AttentionPolicyService
from backend.attention.service import AttentionService
from backend.communications.detection.noise import clear_subjects, register_subject
from backend.communications.detection.service import DetectionService
from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)

from .schema import (
    AskCase,
    AttentionCase,
    DetectionCase,
    SyntheticCorpus,
    default_corpus_path,
)

#: Where the T003.AC9 ask pipeline's recorded fixture would live.
ASK_FIXTURE_PATH = default_corpus_path().parent / "ask_fixture.json"

CASE_STATUS_PASS = "pass"
CASE_STATUS_FAIL = "fail"
CASE_STATUS_PENDING = "pending"


def _anchor_dt(corpus: SyntheticCorpus) -> datetime:
    return datetime.fromisoformat(corpus.anchor)


def _enum_by_value(enum_cls, value: str):
    """Case-insensitive enum lookup (corpus labels stay human-readable)."""
    for member in enum_cls:
        if member.value.lower() == value.lower():
            return member
    raise ValueError(f"{value!r} is not a valid {enum_cls.__name__}")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


# -- detection replay ------------------------------------------------------------


def replay_detection(case: DetectionCase, anchor: datetime) -> dict[str, Any]:
    """Run one detection case on a fresh in-memory service (assist off)."""
    clear_subjects()
    detection = DetectionService(attention=AttentionService())
    try:
        comm = Communication(
            communication_id=case.id,
            channel=_enum_by_value(CommunicationChannel, case.channel),
            direction=_enum_by_value(CommunicationDirection, case.direction),
            sender=case.sender,
            timestamp=anchor,
            content_ref=f"cr:{case.id}",
        )
        detection.register_content(comm.content_ref, case.text)
        if case.subject is not None:
            register_subject(comm.content_ref, case.subject)
        report = detection.detect(comm, now=anchor)
        return {
            "kinds": [f.kind for f in report.findings],
            "finding_ids": [f.finding_id for f in report.findings],
            "due": {f.kind: _iso(f.due) for f in report.findings},
            "model_used": [f.model_used for f in report.findings],
            "status": [f.status.value for f in report.findings],
            "confidence": {f.kind: f.confidence for f in report.findings},
            "events": [e.event_type for e in report.events],
            "why": {f.kind: list(f.why) for f in report.findings},
            "suppressed": list(report.suppressed),
        }
    finally:
        detection.reset()
        clear_subjects()


# -- attention replay --------------------------------------------------------------


def _make_items(case: AttentionCase, anchor: datetime) -> list[AttentionItem]:
    return [
        AttentionItem(
            attention_id=spec.attention_id,
            reason=f"fixture: {spec.attention_id}",
            priority=spec.priority,
            urgency=spec.urgency,
            importance=spec.priority,
            due=datetime.fromisoformat(spec.due) if spec.due else None,
            entities=list(spec.entities),
            sources=list(spec.sources),
            status=AttentionState(spec.status),
            why=list(spec.why),
        )
        for spec in case.items
    ]


def replay_attention(case: AttentionCase, anchor: datetime) -> dict[str, Any]:
    """Run one attention-policy case on a fresh in-memory policy service."""
    service = AttentionPolicyService()
    if case.budget:
        service.set_budget(**case.budget)
    if case.quiet_period is not None:
        service.add_quiet_period(
            case.quiet_period["kind"],
            case.quiet_period["start"],
            case.quiet_period["end"],
            reason=case.quiet_period.get("reason", ""),
        )
    for pref in case.preferences or []:
        service.set_escalation_preference(
            pref["mode"],
            person=pref.get("person"),
            channel=pref.get("channel"),
            reason=pref.get("reason", ""),
        )

    items = _make_items(case, anchor)
    obs: dict[str, Any] = {}

    if case.operation == "budget_decision":
        decision = service.budget_decision(items, now=anchor)
        obs.update(
            {
                "individual": list(decision.individual),
                "bundled": list(decision.bundled),
                "urgent": list(decision.urgent),
                "bundle_count": decision.bundle.count if decision.bundle else None,
                "bundle_id": decision.bundle.bundle_id if decision.bundle else None,
                "reasons": list(decision.reasons),
            }
        )
    elif case.operation == "notification_flags":
        flags = service.notification_flags(items, now=anchor)
        obs.update(
            {
                "notify": {i.attention_id: flags[i.attention_id].notify for i in items},
                "quiet": {
                    i.attention_id: (
                        flags[i.attention_id].quiet_period.kind.value
                        if flags[i.attention_id].quiet_period
                        else None
                    )
                    for i in items
                },
                "status": {i.attention_id: i.status.value for i in items},
                "reasons": {
                    i.attention_id: list(flags[i.attention_id].reasons) for i in items
                },
            }
        )
    elif case.operation == "deadline_escalation":
        records = service.apply_deadline_escalation(items, now=anchor)
        promoted = {r.attention_id for r in records}
        obs.update(
            {
                "promoted": sorted(promoted),
                "not_promoted": sorted(
                    i.attention_id for i in items if i.attention_id not in promoted
                ),
                "status_after": {i.attention_id: i.status.value for i in items},
                "reasons": {
                    r.attention_id: r.reason for r in records
                },
            }
        )
    elif case.operation == "adaptive_escalation":
        obs.update(
            {
                "mode": {
                    i.attention_id: service.adaptive_escalation(i).mode.value for i in items
                },
                "preference": {
                    i.attention_id: service.adaptive_escalation(i).preference_id
                    for i in items
                },
                "reasons": {
                    i.attention_id: service.adaptive_escalation(i).reason for i in items
                },
            }
        )
    else:  # pragma: no cover — schema-validated literal
        raise ValueError(f"unknown attention operation: {case.operation}")

    return obs


# -- ask replay (T003.AC9 fixture interface) --------------------------------------


def ask_fixture_available(path: str | Path = ASK_FIXTURE_PATH) -> bool:
    """Whether the AC9 ask pipeline's recorded fixture interface exists."""
    return Path(path).is_file()


def replay_ask(case: AskCase, path: str | Path = ASK_FIXTURE_PATH) -> dict[str, Any]:
    """Replay one ask case against the recorded fixture, if present.

    Without the fixture the observation is ``{"pending": true}`` plus the
    corpus-level pending reason — the evaluator marks the case ``pending``
    (neither pass nor fail). Nothing is invented.
    """
    fixture_path = Path(path)
    if not fixture_path.is_file():
        return {
            "pending": True,
            "pending_reason": case.pending_reason
            or "ask fixture interface (T003.AC9 recorded-fixture path) not present",
        }
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    entry = fixture.get("cases", {}).get(case.id)
    if entry is None:
        return {
            "pending": True,
            "pending_reason": f"no recorded fixture entry for case {case.id!r}",
        }
    return {
        "pending": False,
        "answer": entry.get("answer"),
        "grounded": entry.get("grounded"),
        "provenance_ids": entry.get("provenance_ids", []),
    }


# -- corpus replay ------------------------------------------------------------------


def replay_corpus(
    corpus: SyntheticCorpus,
    ask_fixture_path: str | Path = ASK_FIXTURE_PATH,
) -> dict[str, dict[str, Any]]:
    """Replay every case; returns ``{case_id: observation}``.

    Deterministic: same corpus → same observations (fresh services, fixed
    anchor, assist off, no network, no clock reads outside the anchor).
    """
    anchor = _anchor_dt(corpus)
    observations: dict[str, dict[str, Any]] = {}
    for case in corpus.cases:
        if isinstance(case, DetectionCase):
            observations[case.id] = replay_detection(case, anchor)
        elif isinstance(case, AttentionCase):
            observations[case.id] = replay_attention(case, anchor)
        elif isinstance(case, AskCase):
            observations[case.id] = replay_ask(case, path=ask_fixture_path)
        else:  # pragma: no cover — schema-validated union
            raise ValueError(f"unknown case plane: {type(case).__name__}")
    return observations


def corpus_fingerprint(corpus: SyntheticCorpus) -> str:
    """Stable fingerprint of the corpus (version + seed + case ids)."""
    case_ids = ",".join(c.id for c in corpus.cases)
    return (
        f"{corpus.corpus_id}:v{corpus.version}:seed{corpus.seed}"
        f":cases[{len(corpus.cases)}]:ids[{case_ids}]"
    )


__all__ = [
    "ASK_FIXTURE_PATH",
    "CASE_STATUS_FAIL",
    "CASE_STATUS_PASS",
    "CASE_STATUS_PENDING",
    "ask_fixture_available",
    "corpus_fingerprint",
    "replay_ask",
    "replay_attention",
    "replay_corpus",
    "replay_detection",
]
