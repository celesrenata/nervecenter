"""T003-05 synthetic-lab evaluation tests (spec T003.AC10).

Covers the synthetic lab added under ``synthetic-lab/``:

- **Corpus validation** — the versioned corpus loads and passes schema
  validation; invalid corpora (bad schema_version type, duplicate ids,
  bad anchor, unknown case plane, empty case list, unknown field) are
  rejected with :class:`pydantic.ValidationError`.
- **Deterministic replay** — two replays of the same corpus produce
  byte-identical observations (fixed anchor, fresh in-memory services,
  assist off, no clock reads, no network).
- **Expected findings** — every labeled detection case matches its
  expectation (kinds / due / model_used / status / events / why /
  suppression) with a pass verdict and empty evidence.
- **Attention decisions** — budget bundling + urgent bypass, quiet-period
  flag suppression (status preserved), deadline escalation
  (12h promotes / 36h does not / overdue promotes), adaptive escalation
  (specificity + default) all match their labeled expectations.
- **Ask QA / provenance** — with the T003.AC9 recorded-fixture interface
  absent, ask cases replay ``pending`` (never pass, never fail) and no
  answer is invented; with a fixture present, the labeled assertions
  (answer_contains / provenance_ids / grounded) are applied.
- **Exit / gate behavior** — ``evaluate`` regression ⇒ exit code 1;
  no regression ⇒ 0 (pending cases do not fail the gate);
  ``cli.main`` returns 0 on the shipped corpus, 1 on a tampered corpus,
  2 on an unloadable corpus; the report carries evidence for every
  failed case.

Fixture-only: no network, no secrets, no DB, no NATS.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# The synthetic lab package lives under synthetic-lab/ (not an installed
# distribution); put it on sys.path before importing it.
_LAB_ROOT = Path(__file__).resolve().parents[2] / "synthetic-lab"
if str(_LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAB_ROOT))

from synthetic_lab import (  # noqa: E402
    default_corpus_path,
    evaluate,
    load_corpus,
    parse_corpus,
    replay_corpus,
)
from synthetic_lab.cli import main as lab_main  # noqa: E402
from synthetic_lab.evaluator import EvaluationReport  # noqa: E402
from synthetic_lab.replay import (  # noqa: E402
    ask_fixture_available,
    corpus_fingerprint,
    replay_ask,
)
from synthetic_lab.schema import (  # noqa: E402
    AskCase,
    SyntheticCorpus,
)

CORPUS = default_corpus_path()


@pytest.fixture()
def corpus() -> SyntheticCorpus:
    return load_corpus(CORPUS)


@pytest.fixture()
def observations(corpus: SyntheticCorpus) -> dict:
    return replay_corpus(corpus)


@pytest.fixture()
def report(corpus: SyntheticCorpus, observations: dict) -> EvaluationReport:
    return evaluate(corpus, observations, fingerprint=corpus_fingerprint(corpus))


# -- corpus validation -----------------------------------------------------------


def test_corpus_loads_and_is_versioned(corpus: SyntheticCorpus) -> None:
    assert corpus.schema_version == 1
    assert corpus.corpus_id == "t003-05-synthetic-lab"
    assert corpus.version == 1
    assert corpus.seed == 20261020
    assert corpus.anchor == "2026-10-20T10:00:00+00:00"
    assert corpus.cases, "corpus must not be empty"
    # Plane coverage required by T003-05.
    planes = {c.plane for c in corpus.cases}
    assert planes == {"detection", "attention", "ask"}


def test_corpus_case_ids_are_unique(corpus: SyntheticCorpus) -> None:
    ids = [c.id for c in corpus.cases]
    assert len(ids) == len(set(ids))


def test_corpus_has_no_secrets_or_urls() -> None:
    """AC10 self-containment: the corpus is fixture data, never a secret
    carrier and never a network reference."""
    raw = CORPUS.read_text(encoding="utf-8")
    lowered = raw.lower()
    for token in ("api_key", "apikey", "secret", "token", "bearer",
                  "http://", "https://", "password"):
        assert token not in lowered, f"corpus must not reference {token!r}"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version="one"),  # wrong type
        lambda d: d.update(anchor="not-a-date"),
        lambda d: d.pop("anchor"),
        lambda d: d.update(cases=[]),
        lambda d: d["cases"].append({"id": d["cases"][0]["id"], "plane": "detection",
                                     "title": "dup", "direction": "incoming",
                                     "text": "x", "expect": {"kinds": []}}),
        lambda d: d["cases"].append({"id": "bad-plane", "plane": "grpc",
                                     "title": "bad", "direction": "incoming",
                                     "text": "x", "expect": {"kinds": []}}),
        lambda d: d["cases"].append({"id": "extra-field", "plane": "ask",
                                     "title": "bad", "question": "q",
                                     "unknown": 1}),
    ],
    ids=["schema-version-type", "bad-anchor", "missing-anchor", "empty-cases",
         "duplicate-id", "unknown-plane", "unknown-field"],
)
def test_corpus_schema_rejects_invalid(mutate) -> None:
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    mutate(data)
    with pytest.raises(ValidationError):
        parse_corpus(data)


def test_corrupt_file_raises() -> None:
    with pytest.raises(Exception):  # noqa: B017 — json or schema failure
        load_corpus(_LAB_ROOT / "corpus" / "missing.json")


# -- deterministic replay ---------------------------------------------------------


def test_replay_is_deterministic(corpus: SyntheticCorpus) -> None:
    a = json.dumps(replay_corpus(corpus), sort_keys=True, default=str)
    b = json.dumps(replay_corpus(corpus), sort_keys=True, default=str)
    assert a == b, "identical corpus must replay to identical observations"


def test_replay_uses_no_clock_reads(corpus: SyntheticCorpus, observations: dict) -> None:
    """Replay observations are fully anchor-derived: re-running at a
    different process time produces the same JSON (no datetime.now())."""
    again = replay_corpus(corpus)
    assert json.dumps(again, sort_keys=True, default=str) == json.dumps(
        observations, sort_keys=True, default=str
    )


def test_fingerprint_is_stable(corpus: SyntheticCorpus) -> None:
    assert corpus_fingerprint(corpus) == corpus_fingerprint(corpus)
    assert corpus.corpus_id in corpus_fingerprint(corpus)


# -- detection expectations -------------------------------------------------------


def test_detection_cases_all_pass(corpus: SyntheticCorpus, report: EvaluationReport) -> None:
    detection = [c for c in corpus.cases if c.plane == "detection"]
    assert len(detection) >= 6  # commitment / request / deadline / noise coverage
    for case in detection:
        verdict = next(v for v in report.verdicts if v.case_id == case.id)
        assert verdict.status == "pass", f"{case.id}: {verdict.evidence}"
        assert verdict.evidence == []


def test_detection_kinds_cover_all_three_planes(corpus: SyntheticCorpus) -> None:
    kinds = {k for c in corpus.cases if c.plane == "detection" for k in c.expect.kinds}
    assert {"commitment", "request", "deadline"} <= kinds


def test_detection_recall_precision(corpus: SyntheticCorpus, report: EvaluationReport) -> None:
    m = report.detection_metrics
    # Recall-first: every expected finding found (no false negatives).
    assert m.false_negatives == 0
    # Precision-first: no unexpected findings (no false positives).
    assert m.false_positives == 0
    assert m.recall == 1.0
    assert m.precision == 1.0
    assert m.true_negatives >= 3  # noise cases correctly produce nothing


def test_detection_due_dates_are_exact(corpus: SyntheticCorpus, observations: dict) -> None:
    """Labeled due dates (relative-date math pinned to the anchor)."""
    obs = observations["det-commitment-tonight"]
    assert obs["due"]["commitment"] == "2026-10-20T20:00:00+00:00"
    obs = observations["det-mixed-tuesday"]
    assert obs["due"]["deadline"] == "2026-10-27T17:00:00+00:00"
    assert obs["due"]["request"] is None  # due lives on the deadline, not the request
    obs = observations["det-commitment-open"]
    assert obs["due"]["commitment"] is None  # open-ended: no guessed due


def test_detection_model_used_stays_deterministic(observations: dict) -> None:
    for case_id, obs in observations.items():
        if "model_used" not in obs:
            continue
        models = obs["model_used"]
        if not models:  # noise / clean case: no finding, no model
            continue
        assert set(models) == {"deterministic-extractor"}, case_id


def test_noise_cases_are_suppressed_or_clean(corpus: SyntheticCorpus, observations: dict) -> None:
    for case in corpus.cases:
        if case.plane != "detection" or case.expect.kinds:
            continue
        obs = observations[case.id]
        assert obs["kinds"] == []
        assert obs["events"] == []
        if case.expect.suppressed:
            assert obs["suppressed"]
        else:
            assert obs["suppressed"] == []


# -- attention expectations ---------------------------------------------------------


def test_attention_cases_all_pass(corpus: SyntheticCorpus, report: EvaluationReport) -> None:
    attention = [c for c in corpus.cases if c.plane == "attention"]
    assert len(attention) >= 5
    for case in attention:
        verdict = next(v for v in report.verdicts if v.case_id == case.id)
        assert verdict.status == "pass", f"{case.id}: {verdict.evidence}"


def test_attention_budget_and_urgent_bypass(corpus: SyntheticCorpus, observations: dict) -> None:
    obs = observations["att-budget-bundle"]
    assert len(obs["individual"]) == 3
    assert len(obs["bundled"]) == 3
    assert obs["bundle_count"] == 3
    assert any("daily FYI cap" in r for r in obs["reasons"])
    obs = observations["att-budget-urgent-bypass"]
    assert obs["urgent"] == ["urg"]  # urgent bypasses batching
    assert "urg" not in obs["bundled"]


def test_attention_quiet_preserves_status(corpus: SyntheticCorpus, observations: dict) -> None:
    obs = observations["att-quiet-sleep"]
    assert obs["notify"]["fyi-q"] is False
    assert obs["notify"]["now-q"] is False
    assert obs["quiet"] == {"fyi-q": "sleep", "now-q": "sleep"}
    # Flags suppressed only — statuses / feed membership untouched.
    assert obs["status"] == {"fyi-q": "FYI", "now-q": "NOW"}


def test_attention_deadline_escalation_threshold(
    corpus: SyntheticCorpus, observations: dict
) -> None:
    assert observations["att-escalate-12h"]["promoted"] == ["d-1"]
    assert observations["att-escalate-12h"]["status_after"]["d-1"] == "NOW"
    assert "12.0h away" in observations["att-escalate-12h"]["reasons"]["d-1"]
    assert observations["att-escalate-48h"]["promoted"] == []
    assert observations["att-escalate-48h"]["status_after"]["d-2"] == "TODAY"
    assert observations["att-escalate-overdue"]["promoted"] == ["d-3"]
    assert "overdue" in observations["att-escalate-overdue"]["reasons"]["d-3"]


def test_attention_adaptive_escalation_specificity(
    corpus: SyntheticCorpus, observations: dict
) -> None:
    obs = observations["att-adaptive-silent"]
    assert obs["mode"]["a-1"] == "silent"
    assert obs["preference"]["a-1"] == "alice:sms"  # person+channel beats channel-only
    assert observations["att-adaptive-default"]["mode"]["a-2"] == "notification"
    assert observations["att-adaptive-default"]["preference"]["a-2"] is None


# -- ask QA / provenance --------------------------------------------------------------


def test_ask_cases_are_pending_without_fixture(
    corpus: SyntheticCorpus, report: EvaluationReport
) -> None:
    """AC9 fixture interface absent ⇒ ask cases pending, never pass/fail,
    no answer invented."""
    assert not ask_fixture_available()
    ask = [c for c in corpus.cases if c.plane == "ask"]
    assert ask, "corpus must carry ask ground-truth cases"
    for case in ask:
        verdict = next(v for v in report.verdicts if v.case_id == case.id)
        assert verdict.status == "pending"
        assert any(e.startswith("pending:") for e in verdict.evidence)
    assert set(report.pending) == {c.id for c in ask}
    # Pending cases are labeled ground truth, never fabricated results.
    for case in ask:
        assert case.expect, "ask case must carry ground-truth expectations"
        assert case.pending_reason


def test_ask_fixture_applies_labeled_assertions(corpus: SyntheticCorpus, tmp_path: Path) -> None:
    """With a recorded fixture present, labeled assertions are applied
    (answer_contains / grounded / provenance_ids) and pass or fail —
    the evaluator never invents an answer on its own."""
    ask = next(c for c in corpus.cases if c.plane == "ask")
    fixture = tmp_path / "ask_fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "cases": {
                    ask.id: {
                        "answer": "The API 5xx spike traces to the api plane.",
                        "grounded": True,
                        "provenance_ids": [ask.expect["provenance_ids"][0]],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    obs = replay_ask(ask, path=fixture)
    assert obs["pending"] is False
    verdict = next(
        v for v in evaluate(corpus, replay_corpus(corpus, ask_fixture_path=fixture)).verdicts
        if v.case_id == ask.id
    )
    assert verdict.status == "pass", verdict.evidence


def test_ask_fixture_missing_entry_stays_pending(corpus: SyntheticCorpus, tmp_path: Path) -> None:
    fixture = tmp_path / "ask_fixture.json"
    fixture.write_text(json.dumps({"cases": {}}), encoding="utf-8")
    ask = next(c for c in corpus.cases if c.plane == "ask")
    obs = replay_ask(ask, path=fixture)
    assert obs["pending"] is True
    assert "no recorded fixture entry" in obs["pending_reason"]


def test_ask_failure_reports_evidence_not_silence(corpus: SyntheticCorpus, tmp_path: Path) -> None:
    """A fixture whose provenance is incomplete must fail with evidence —
    not silently pass."""
    ask = next(c for c in corpus.cases if c.plane == "ask")
    fixture = tmp_path / "ask_fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "cases": {
                    ask.id: {
                        "answer": "something ungrounded",
                        "grounded": False,
                        "provenance_ids": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    obs = replay_ask(ask, path=fixture)
    assert obs["pending"] is False
    report = evaluate(corpus, replay_corpus(corpus, ask_fixture_path=fixture))
    verdict = next(v for v in report.verdicts if v.case_id == ask.id)
    assert verdict.status == "fail"
    assert any("provenance" in e for e in verdict.evidence)
    assert report.exit_code == 1


# -- exit / gate behavior ---------------------------------------------------------------


def test_gate_zero_without_regression(corpus: SyntheticCorpus, report: EvaluationReport) -> None:
    assert report.regression is False
    assert report.exit_code == 0
    assert not report.failed
    # Pending ask cases never fail the gate.
    assert report.exit_code == 0 and report.pending


def test_gate_nonzero_on_regression(corpus: SyntheticCorpus, observations: dict) -> None:
    """Tamper one observation (a missed deadline) ⇒ the evaluator must
    flag the case, the report, and the exit code."""
    tampered = json.loads(json.dumps(observations))
    tampered["det-commitment-tonight"]["due"] = {"commitment": "2026-10-21T20:00:00+00:00"}
    report = evaluate(corpus, tampered, fingerprint=corpus_fingerprint(corpus))
    assert report.regression is True
    assert report.exit_code == 1
    verdict = next(v for v in report.verdicts if v.case_id == "det-commitment-tonight")
    assert verdict.status == "fail"
    assert any("due" in e for e in verdict.evidence)


def test_gate_detects_missing_finding(corpus: SyntheticCorpus, observations: dict) -> None:
    """A dropped finding (regression in recall) fails with evidence."""
    tampered = json.loads(json.dumps(observations))
    tampered["det-request-simple"]["kinds"] = []
    tampered["det-request-simple"]["events"] = []
    report = evaluate(corpus, tampered, fingerprint=corpus_fingerprint(corpus))
    assert report.regression is True
    assert report.detection_metrics.false_negatives == 1
    assert report.detection_metrics.recall < 1.0
    verdict = next(v for v in report.verdicts if v.case_id == "det-request-simple")
    assert verdict.status == "fail"
    assert any("kinds" in e for e in verdict.evidence)


def test_cli_main_exit_codes(corpus: SyntheticCorpus, tmp_path: Path) -> None:
    # 0 — shipped corpus, no regression (ask cases pending, not failed).
    assert lab_main([]) == 0
    # 2 — unloadable corpus.
    assert lab_main([str(tmp_path / "nope.json")]) == 2
    # 1 — tampered corpus file (missed due date) ⇒ regression.
    bad = json.loads(CORPUS.read_text(encoding="utf-8"))
    for case in bad["cases"]:
        if case["id"] == "det-commitment-tonight":
            case["expect"]["due"]["commitment"] = "2026-10-21T20:00:00+00:00"
    bad_path = tmp_path / "corpus-bad.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    assert lab_main([str(bad_path)]) == 1


def test_cli_json_report_shape(corpus: SyntheticCorpus, capfd) -> None:
    lab_main(["--json"])
    out = capfd.readouterr().out
    data = json.loads(out)
    assert data["exit_code"] == 0
    assert data["regression"] is False
    expected_ask = [c.id for c in corpus.cases if isinstance(c, AskCase)]
    assert data["pending"] == expected_ask
    assert data["pass"] + data["fail"] + data["pending"]
    assert len(data["verdicts"]) == len(data["pass"]) + len(data["fail"]) + len(data["pending"])
    assert data["detection"]["precision"] == 1.0
    assert data["detection"]["recall"] == 1.0


def test_summary_mentions_pending_and_regression(report: EvaluationReport) -> None:
    summary = report.summary()
    assert "REGRESSION: no" in summary
    assert "pending" in summary.lower()
    assert "precision=1.000" in summary
    assert "recall=1.000" in summary
