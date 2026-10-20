"""Pass/fail evaluation of replayed corpus observations (T003.AC10).

The evaluator compares each replayed observation against the case's
labeled expectation and produces:

- per-case ``pass`` / ``fail`` / ``pending`` verdicts with evidence
  (expected vs observed for every failed assertion),
- aggregate detection-plane precision/recall over the labeled cases
  (prompt2 §72, recall-first / precision-first),
- a regression verdict: any ``fail`` case is a regression; a regression
  makes :func:`run_evaluation` return a nonzero exit code (CI gate),
  while ``pending`` cases (the ask plane until T003.AC9's fixture
  interface lands) are reported but do not fail the gate.

No network, no secrets, no clock reads — everything is derived from the
replay observations and the labeled expectations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .schema import (
    AskCase,
    AttentionCase,
    DetectionCase,
    SyntheticCorpus,
)

CaseStatus = Literal["pass", "fail", "pending"]


@dataclass
class CaseVerdict:
    """Verdict + evidence for one case."""

    case_id: str
    plane: str
    title: str
    status: CaseStatus
    evidence: list[str] = field(default_factory=list)


@dataclass
class PlaneMetrics:
    """Precision/recall over the labeled detection cases (prompt2 §72)."""

    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    true_negatives: int = 0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 1.0

    @property
    def f1(self) -> float:
        if self.precision + self.recall == 0:
            return 0.0
        return (
            2 * self.precision * self.recall
            / (self.precision + self.recall)
        )


@dataclass
class EvaluationReport:
    """Full evaluation: per-case verdicts, metrics, regression verdict."""

    corpus_id: str
    version: int
    fingerprint: str
    verdicts: list[CaseVerdict] = field(default_factory=list)
    detection_metrics: PlaneMetrics = field(default_factory=PlaneMetrics)
    pending: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[str]:
        return [v.case_id for v in self.verdicts if v.status == "fail"]

    @property
    def passed(self) -> list[str]:
        return [v.case_id for v in self.verdicts if v.status == "pass"]

    @property
    def regression(self) -> bool:
        return bool(self.failed)

    @property
    def exit_code(self) -> int:
        """0 when no regression; 1 when at least one case failed.

        ``pending`` cases (ask plane without the AC9 fixture interface)
        are reported but never fail the gate.
        """
        return 1 if self.regression else 0

    def summary(self) -> str:
        lines = [
            f"synthetic-lab corpus {self.corpus_id} v{self.version} "
            f"({self.fingerprint})",
            f"cases: {len(self.verdicts)} | pass: {len(self.passed)} | "
            f"fail: {len(self.failed)} | pending: {len(self.pending)}",
            f"detection precision={self.detection_metrics.precision:.3f} "
            f"recall={self.detection_metrics.recall:.3f} "
            f"f1={self.detection_metrics.f1:.3f} "
            f"(tp={self.detection_metrics.true_positives}, "
            f"fp={self.detection_metrics.false_positives}, "
            f"fn={self.detection_metrics.false_negatives}, "
            f"tn={self.detection_metrics.true_negatives})",
        ]
        for verdict in self.verdicts:
            if verdict.status != "pass" or verdict.evidence:
                lines.append(f"[{verdict.status.upper()}] {verdict.case_id} — {verdict.title}")
                for line in verdict.evidence:
                    lines.append(f"    {line}")
        if self.pending:
            lines.append(
                "pending (no AC9 fixture interface — no results invented): "
                + ", ".join(self.pending)
            )
        lines.append("REGRESSION: yes" if self.regression else "REGRESSION: no")
        return "\n".join(lines)


# -- helpers ---------------------------------------------------------------------


def _sorted_unique(values: Any) -> list[str]:
    return sorted(set(values or []))


def _check_list_match(observed: list[str], expected: list[str], label: str,
                      evidence: list[str]) -> bool:
    if _sorted_unique(observed) != _sorted_unique(expected):
        evidence.append(f"{label}: expected {sorted(expected)}, observed {sorted(observed)}")
        return False
    return True


def _check_contains(substrings: list[str], haystack: str, label: str,
                    evidence: list[str]) -> bool:
    low = haystack.lower()
    for needle in substrings:
        if needle.lower() not in low:
            evidence.append(f"{label}: {needle!r} not in {haystack[:120]!r}")
            return False
    return True


# -- detection -------------------------------------------------------------------


def evaluate_detection(case: DetectionCase, obs: dict[str, Any]) -> list[str]:
    """Expected-vs-observed evidence lines for one detection case."""
    evidence: list[str] = []
    expect = case.expect

    _check_list_match(obs["kinds"], expect.kinds, "kinds", evidence)
    if expect.finding_ids is not None:
        _check_list_match(obs["finding_ids"], expect.finding_ids, "finding_ids", evidence)

    for kind, expected_due in expect.due.items():
        actual = obs["due"].get(kind, "<kind absent>")
        if actual != expected_due:
            evidence.append(f"due[{kind}]: expected {expected_due}, observed {actual}")

    for model in obs["model_used"]:
        if model != expect.model_used:
            evidence.append(f"model_used: expected {expect.model_used!r}, observed {model!r}")
            break

    if expect.status is not None:
        _check_list_match(obs["status"], expect.status, "status", evidence)

    if expect.confidence is not None:
        for kind, expected in expect.confidence.items():
            actual = obs["confidence"].get(kind)
            if actual is None or abs(actual - expected) > 1e-9:
                evidence.append(f"confidence[{kind}]: expected {expected}, observed {actual}")

    if expect.events is not None:
        _check_list_match(obs["events"], expect.events, "events", evidence)

    for kind, needles in expect.why_contains.items():
        haystack = " | ".join(obs["why"].get(kind, []))
        _check_contains(needles, haystack, f"why[{kind}]", evidence)

    if expect.suppressed:
        if not obs["suppressed"]:
            evidence.append("suppressed: expected suppression, observed none")
        if expect.suppression_contains is not None:
            _check_contains(
                [expect.suppression_contains],
                " | ".join(obs["suppressed"]),
                "suppression_reason",
                evidence,
            )
    elif obs["suppressed"]:
        evidence.append(f"suppressed: expected none, observed {obs['suppressed']}")
    return evidence


def detection_confusion(case: DetectionCase, obs: dict[str, Any]) -> tuple[int, int, int, int]:
    """(tp, fp, fn, tn) for one detection case, kind-level.

    A case whose expectation is empty (noise/precision case) counts as a
    true negative when no findings are produced, false positive when
    findings are. A recall case counts per kind.
    """
    expected_kinds = set(expect_kinds_of(case))
    observed_kinds = set(obs["kinds"])
    if not expected_kinds:
        return (0, len(observed_kinds), 0, 1 if not observed_kinds else 0)
    tp = len(expected_kinds & observed_kinds)
    fp = len(observed_kinds - expected_kinds)
    fn = len(expected_kinds - observed_kinds)
    return tp, fp, fn, 0


def expect_kinds_of(case: DetectionCase) -> list[str]:
    return list(case.expect.kinds)


# -- attention -------------------------------------------------------------------


def evaluate_attention(case: AttentionCase, obs: dict[str, Any]) -> list[str]:
    """Expected-vs-observed evidence lines for one attention case."""
    evidence: list[str] = []
    expect = case.expect

    if expect.individual is not None:
        _check_list_match(obs.get("individual", []), expect.individual, "individual", evidence)
    if expect.bundled is not None:
        _check_list_match(obs.get("bundled", []), expect.bundled, "bundled", evidence)
    if expect.urgent is not None:
        _check_list_match(obs.get("urgent", []), expect.urgent, "urgent", evidence)
    if expect.bundle_count is not None:
        actual = obs.get("bundle_count")
        if actual != expect.bundle_count:
            evidence.append(f"bundle_count: expected {expect.bundle_count}, observed {actual}")

    if expect.notify is not None:
        for iid, expected in expect.notify.items():
            actual = obs.get("notify", {}).get(iid)
            if actual != expected:
                evidence.append(f"notify[{iid}]: expected {expected}, observed {actual}")
    if expect.quiet is not None:
        for iid, expected in expect.quiet.items():
            actual = obs.get("quiet", {}).get(iid)
            if actual != expected:
                evidence.append(f"quiet[{iid}]: expected {expected!r}, observed {actual!r}")
    if expect.status is not None:
        for iid, expected in expect.status.items():
            actual = obs.get("status", {}).get(iid)
            if actual != expected:
                evidence.append(f"status[{iid}]: expected {expected!r}, observed {actual!r}")
    if expect.promoted is not None:
        _check_list_match(obs.get("promoted", []), expect.promoted, "promoted", evidence)
    if expect.not_promoted is not None:
        _check_list_match(
            obs.get("not_promoted", []), expect.not_promoted, "not_promoted", evidence
        )
    if expect.status_after is not None:
        for iid, expected in expect.status_after.items():
            actual = obs.get("status_after", {}).get(iid)
            if actual != expected:
                evidence.append(f"status_after[{iid}]: expected {expected!r}, observed {actual!r}")
    if expect.mode is not None:
        for iid, expected in expect.mode.items():
            actual = obs.get("mode", {}).get(iid)
            if actual != expected:
                evidence.append(f"mode[{iid}]: expected {expected!r}, observed {actual!r}")
    if expect.preference is not None:
        for iid, expected in expect.preference.items():
            actual = obs.get("preference", {}).get(iid)
            if actual != expected:
                evidence.append(f"preference[{iid}]: expected {expected!r}, observed {actual!r}")

    if expect.reason_contains is not None:
        for iid, needle in expect.reason_contains.items():
            haystack = ""
            if case.operation == "deadline_escalation":
                haystack = obs.get("reasons", {}).get(iid, "")
            elif case.operation == "adaptive_escalation":
                haystack = obs.get("reasons", {}).get(iid, "")
            elif case.operation == "notification_flags":
                haystack = " | ".join(obs.get("reasons", {}).get(iid, []))
            _check_contains([needle], haystack, f"reason[{iid}]", evidence)
    if expect.reasons_contains is not None:
        reasons = obs.get("reasons")
        haystack = " | ".join(reasons if isinstance(reasons, list) else [])
        _check_contains(expect.reasons_contains, haystack, "reasons", evidence)
    return evidence


# -- ask ---------------------------------------------------------------------------


def evaluate_ask(case: AskCase, obs: dict[str, Any]) -> tuple[CaseStatus, list[str]]:
    """Verdict for one ask case.

    Pending (no AC9 fixture) → ``pending`` with the recorded reason. With
    a fixture entry, the labeled ``expect`` assertions are checked:
    ``answer_contains`` (list of substrings), ``provenance_ids`` (exact
    set), ``grounded`` (bool).
    """
    if obs.get("pending"):
        return "pending", [f"pending: {obs.get('pending_reason')}"]
    evidence: list[str] = []
    answer = str(obs.get("answer") or "")
    for needle in case.expect.get("answer_contains", []):
        if needle.lower() not in answer.lower():
            evidence.append(f"answer: {needle!r} not in {answer[:120]!r}")
    expected_prov = sorted(case.expect.get("provenance_ids", []))
    observed_prov = sorted(obs.get("provenance_ids", []))
    if expected_prov != observed_prov:
        evidence.append(f"provenance: expected {expected_prov}, observed {observed_prov}")
    if "grounded" in case.expect and bool(obs.get("grounded")) != case.expect["grounded"]:
        evidence.append(
            f"grounded: expected {case.expect['grounded']}, "
            f"observed {obs.get('grounded')}"
        )
    return ("fail" if evidence else "pass"), evidence


# -- full evaluation ------------------------------------------------------------------


def evaluate(
    corpus: SyntheticCorpus,
    observations: dict[str, dict[str, Any]],
    fingerprint: str = "",
) -> EvaluationReport:
    """Evaluate replayed observations against the labeled corpus."""
    report = EvaluationReport(
        corpus_id=corpus.corpus_id,
        version=corpus.version,
        fingerprint=fingerprint,
    )
    metrics = PlaneMetrics()
    for case in corpus.cases:
        obs = observations.get(case.id)
        if obs is None:
            report.verdicts.append(
                CaseVerdict(case.id, case.plane, case.title, "fail",
                            ["no observation produced by replay"])
            )
            continue
        if isinstance(case, DetectionCase):
            evidence = evaluate_detection(case, obs)
            tp, fp, fn, tn = detection_confusion(case, obs)
            metrics.true_positives += tp
            metrics.false_positives += fp
            metrics.false_negatives += fn
            metrics.true_negatives += tn
            report.verdicts.append(
                CaseVerdict(case.id, case.plane, case.title,
                            "fail" if evidence else "pass", evidence)
            )
        elif isinstance(case, AttentionCase):
            evidence = evaluate_attention(case, obs)
            report.verdicts.append(
                CaseVerdict(case.id, case.plane, case.title,
                            "fail" if evidence else "pass", evidence)
            )
        elif isinstance(case, AskCase):
            status, evidence = evaluate_ask(case, obs)
            report.verdicts.append(
                CaseVerdict(case.id, case.plane, case.title, status, evidence)
            )
            if status == "pending":
                report.pending.append(case.id)
        else:  # pragma: no cover — schema-validated union
            report.verdicts.append(
                CaseVerdict(case.id, "?", case.title, "fail",
                            ["unknown case type in corpus"])
            )
    report.detection_metrics = metrics
    return report


def run_evaluation(
    corpus: SyntheticCorpus,
    observations: dict[str, dict[str, Any]],
    fingerprint: str = "",
) -> EvaluationReport:
    """Alias kept for the CI-gate call site (evaluator = evaluate + report)."""
    return evaluate(corpus, observations, fingerprint=fingerprint)


__all__ = [
    "CaseVerdict",
    "EvaluationReport",
    "PlaneMetrics",
    "detection_confusion",
    "evaluate",
    "evaluate_attention",
    "evaluate_ask",
    "evaluate_detection",
    "run_evaluation",
]
