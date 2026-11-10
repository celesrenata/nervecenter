"""NerveShield Task 4.5: escalation policy (pure, fixtures only, NS-ADR-031)."""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

from backend.mail_security.escalation import (
    DEFAULT_CONFIG,
    ESCALATION_POLICY_VERSION,
    ArcOutcome,
    DeterministicSummary,
    EscalationConfig,
    EscalationInputs,
    GraniteOpinion,
    GraniteOutcome,
    HighImpactFlag,
    QwenOpinion,
    QwenOutcome,
    Stage,
    decide_escalation,
)
from backend.mail_security.escalation import (
    EscalationReason as R,
)
from backend.mail_security.escalation import (
    StageStatus as S,
)
from backend.mail_security.models import Disposition as D

RANK = {D.SAFE_TO_PROCESS: 0, D.LOW_PRIORITY: 1, D.INCOMPLETE: 2, D.REVIEW: 3, D.RESTRICTED: 4}
HI = frozenset({HighImpactFlag.PAYMENT_REQUEST})
QCFG = EscalationConfig(qwen_enabled=True)


def arc(score: float) -> ArcOutcome:
    return ArcOutcome(S.COMPLETED, score)


def gr(op: GraniteOpinion | None, status: S = S.COMPLETED, n: int = 1) -> GraniteOutcome:
    return GraniteOutcome(status, op, n)


def qw(op: QwenOpinion | None, status: S = S.COMPLETED, n: int = 1) -> QwenOutcome:
    return QwenOutcome(status, op, n)


def inp(floor: D = D.LOW_PRIORITY, **kw: object) -> EscalationInputs:
    det = DeterministicSummary(
        floor,
        kw.pop("complete", True),  # type: ignore[arg-type]
        kw.pop("hi", frozenset()),  # type: ignore[arg-type]
    )
    return EscalationInputs(det, **kw)  # type: ignore[arg-type]


CONSISTENT, CONCERN, UNCLEAR = (
    GraniteOpinion.CONSISTENT,
    GraniteOpinion.CONCERN,
    GraniteOpinion.UNCLEAR,
)

# (id, inputs, config, disposition, next_stage, required reasons)
TABLE = [
    (
        "restricted floor, no calls",
        inp(D.RESTRICTED),
        DEFAULT_CONFIG,
        D.RESTRICTED,
        None,
        [R.FLOOR_ALREADY_CAUTIOUS],
    ),
    (
        "review floor ignores models",
        inp(D.REVIEW, arc=arc(0.0), granite=gr(CONSISTENT)),
        DEFAULT_CONFIG,
        D.REVIEW,
        None,
        [R.FLOOR_ALREADY_CAUTIOUS],
    ),
    (
        "incomplete floor stays",
        inp(D.INCOMPLETE, hi=HI),
        DEFAULT_CONFIG,
        D.INCOMPLETE,
        None,
        [R.FLOOR_ALREADY_CAUTIOUS],
    ),
    (
        "incomplete inspection",
        inp(complete=False),
        DEFAULT_CONFIG,
        D.INCOMPLETE,
        None,
        [R.DETERMINISTIC_INCOMPLETE],
    ),
    ("arc due first", inp(), DEFAULT_CONFIG, D.LOW_PRIORITY, Stage.ARC, [R.ARC_STAGE_DUE]),
    (
        "arc due even with high impact",
        inp(hi=HI),
        DEFAULT_CONFIG,
        D.LOW_PRIORITY,
        Stage.ARC,
        [R.ARC_STAGE_DUE],
    ),
    (
        "arc disabled, no trigger",
        inp(),
        EscalationConfig(arc_enabled=False),
        D.LOW_PRIORITY,
        None,
        [R.NO_TRIGGER],
    ),
    (
        "arc disabled, high impact -> granite",
        inp(hi=HI),
        EscalationConfig(arc_enabled=False),
        D.REVIEW,
        Stage.GRANITE,
        [R.HIGH_IMPACT_REQUEST],
    ),
    (
        "arc low, no trigger",
        inp(arc=arc(0.05)),
        DEFAULT_CONFIG,
        D.LOW_PRIORITY,
        None,
        [R.NO_TRIGGER],
    ),
    (
        "arc low edge, no trigger",
        inp(arc=arc(0.2)),
        DEFAULT_CONFIG,
        D.LOW_PRIORITY,
        None,
        [R.NO_TRIGGER],
    ),
    (
        "arc band -> granite",
        inp(arc=arc(0.5)),
        DEFAULT_CONFIG,
        D.LOW_PRIORITY,
        Stage.GRANITE,
        [R.ARC_UNCERTAIN_BAND],
    ),
    (
        "arc high -> granite, review so far",
        inp(arc=arc(0.9)),
        DEFAULT_CONFIG,
        D.REVIEW,
        Stage.GRANITE,
        [R.ARC_HIGH_SCORE],
    ),
    (
        "arc high edge",
        inp(arc=arc(0.8)),
        DEFAULT_CONFIG,
        D.REVIEW,
        Stage.GRANITE,
        [R.ARC_HIGH_SCORE],
    ),
    (
        "arc unavailable -> granite",
        inp(arc=ArcOutcome(S.UNAVAILABLE)),
        DEFAULT_CONFIG,
        D.LOW_PRIORITY,
        Stage.GRANITE,
        [R.ARC_UNAVAILABLE],
    ),
    (
        "arc nan score treated unavailable",
        inp(arc=arc(float("nan"))),
        DEFAULT_CONFIG,
        D.LOW_PRIORITY,
        Stage.GRANITE,
        [R.ARC_UNAVAILABLE],
    ),
    (
        "arc out of range treated unavailable",
        inp(arc=arc(1.5)),
        DEFAULT_CONFIG,
        D.LOW_PRIORITY,
        Stage.GRANITE,
        [R.ARC_UNAVAILABLE],
    ),
    (
        "arc completed without score",
        inp(arc=ArcOutcome(S.COMPLETED, None)),
        DEFAULT_CONFIG,
        D.LOW_PRIORITY,
        Stage.GRANITE,
        [R.ARC_UNAVAILABLE],
    ),
    (
        "granite concern -> review",
        inp(arc=arc(0.5), granite=gr(CONCERN)),
        DEFAULT_CONFIG,
        D.REVIEW,
        None,
        [R.GRANITE_CONCERN, R.QWEN_NOT_NEEDED],
    ),
    (
        "granite unclear -> incomplete",
        inp(arc=arc(0.5), granite=gr(UNCLEAR)),
        DEFAULT_CONFIG,
        D.INCOMPLETE,
        None,
        [R.GRANITE_UNCLEAR, R.QWEN_DISABLED],
    ),
    (
        "granite consistent keeps floor",
        inp(arc=arc(0.5), granite=gr(CONSISTENT)),
        DEFAULT_CONFIG,
        D.LOW_PRIORITY,
        None,
        [R.GRANITE_CONSISTENT, R.QWEN_DISABLED],
    ),
    (
        "conflict: arc high, granite consistent stays review",
        inp(arc=arc(0.95), granite=gr(CONSISTENT)),
        DEFAULT_CONFIG,
        D.REVIEW,
        None,
        [R.CONFLICTING_SIGNALS],
    ),
    (
        "high impact, granite consistent stays review",
        inp(hi=HI, arc=arc(0.0), granite=gr(CONSISTENT)),
        DEFAULT_CONFIG,
        D.REVIEW,
        None,
        [R.HIGH_IMPACT_REQUEST],
    ),
    (
        "granite outage, band -> incomplete",
        inp(arc=arc(0.5), granite=gr(None, S.UNAVAILABLE, 0)),
        DEFAULT_CONFIG,
        D.INCOMPLETE,
        None,
        [R.GRANITE_FAILED],
    ),
    (
        "granite denied route, band -> incomplete",
        inp(arc=arc(0.5), granite=gr(None, S.DENIED, 0)),
        DEFAULT_CONFIG,
        D.INCOMPLETE,
        None,
        [R.GRANITE_FAILED],
    ),
    (
        "granite timeout, high impact -> review",
        inp(hi=HI, arc=arc(0.0), granite=gr(None, S.FAILED, 2)),
        DEFAULT_CONFIG,
        D.REVIEW,
        None,
        [R.GRANITE_FAILED],
    ),
    (
        "granite rate limited, arc high -> review",
        inp(arc=arc(0.9), granite=gr(None, S.RATE_LIMITED, 0)),
        DEFAULT_CONFIG,
        D.REVIEW,
        None,
        [R.GRANITE_FAILED],
    ),
    (
        "granite completed w/o opinion fails closed",
        inp(arc=arc(0.5), granite=gr(None, S.COMPLETED)),
        DEFAULT_CONFIG,
        D.INCOMPLETE,
        None,
        [R.GRANITE_FAILED],
    ),
    (
        "granite disabled fails closed",
        inp(arc=arc(0.5)),
        EscalationConfig(granite_enabled=False),
        D.INCOMPLETE,
        None,
        [R.GRANITE_DISABLED],
    ),
    (
        "evidence over budget",
        inp(arc=arc(0.5), evidence_chars=9000),
        DEFAULT_CONFIG,
        D.INCOMPLETE,
        None,
        [R.EVIDENCE_OVER_BUDGET],
    ),
    (
        "evidence over budget, high impact",
        inp(hi=HI, arc=arc(0.0), evidence_chars=9000),
        DEFAULT_CONFIG,
        D.REVIEW,
        None,
        [R.EVIDENCE_OVER_BUDGET],
    ),
    (
        "evidence at budget runs",
        inp(arc=arc(0.5), evidence_chars=8192),
        DEFAULT_CONFIG,
        D.LOW_PRIORITY,
        Stage.GRANITE,
        [R.GRANITE_STAGE_DUE],
    ),
    (
        "zero call budget",
        inp(arc=arc(0.5)),
        EscalationConfig(max_model_calls=0),
        D.INCOMPLETE,
        None,
        [R.MODEL_BUDGET_EXHAUSTED],
    ),
    (
        "rate window empty",
        inp(arc=arc(0.5), window_calls_remaining=0),
        DEFAULT_CONFIG,
        D.INCOMPLETE,
        None,
        [R.RATE_BUDGET_EXHAUSTED],
    ),
    (
        "rate window ok",
        inp(arc=arc(0.5), window_calls_remaining=1),
        DEFAULT_CONFIG,
        D.LOW_PRIORITY,
        Stage.GRANITE,
        [R.GRANITE_STAGE_DUE],
    ),
    (
        "qwen due after unclear",
        inp(arc=arc(0.5), granite=gr(UNCLEAR)),
        QCFG,
        D.INCOMPLETE,
        Stage.QWEN,
        [R.QWEN_STAGE_DUE],
    ),
    (
        "qwen due after consistent in band",
        inp(arc=arc(0.5), granite=gr(CONSISTENT)),
        QCFG,
        D.LOW_PRIORITY,
        Stage.QWEN,
        [R.QWEN_STAGE_DUE],
    ),
    (
        "qwen not needed when already review",
        inp(arc=arc(0.9), granite=gr(CONSISTENT)),
        QCFG,
        D.REVIEW,
        None,
        [R.QWEN_NOT_NEEDED],
    ),
    (
        "qwen skipped, call budget used",
        inp(arc=arc(0.5), granite=gr(CONSISTENT, n=3)),
        EscalationConfig(qwen_enabled=True, max_model_calls=3),
        D.LOW_PRIORITY,
        None,
        [R.QWEN_SKIPPED_BUDGET],
    ),
    (
        "qwen skipped, rate window empty",
        inp(arc=arc(0.5), granite=gr(CONSISTENT), window_calls_remaining=0),
        QCFG,
        D.LOW_PRIORITY,
        None,
        [R.QWEN_SKIPPED_BUDGET],
    ),
    (
        "qwen agree",
        inp(arc=arc(0.5), granite=gr(CONSISTENT), qwen=qw(QwenOpinion.AGREE)),
        QCFG,
        D.LOW_PRIORITY,
        None,
        [R.QWEN_AGREE],
    ),
    (
        "qwen disagree raises to review",
        inp(arc=arc(0.5), granite=gr(CONSISTENT), qwen=qw(QwenOpinion.DISAGREE)),
        QCFG,
        D.REVIEW,
        None,
        [R.QWEN_DISAGREE],
    ),
    (
        "qwen insufficient raises low to incomplete",
        inp(arc=arc(0.5), granite=gr(CONSISTENT), qwen=qw(QwenOpinion.INSUFFICIENT)),
        QCFG,
        D.INCOMPLETE,
        None,
        [R.QWEN_INSUFFICIENT],
    ),
    (
        "qwen agree does not clear unclear",
        inp(arc=arc(0.5), granite=gr(UNCLEAR), qwen=qw(QwenOpinion.AGREE)),
        QCFG,
        D.INCOMPLETE,
        None,
        [R.QWEN_AGREE],
    ),
    (
        "qwen disagree raises unclear to review",
        inp(arc=arc(0.5), granite=gr(UNCLEAR), qwen=qw(QwenOpinion.DISAGREE)),
        QCFG,
        D.REVIEW,
        None,
        [R.QWEN_DISAGREE],
    ),
]


@pytest.mark.parametrize(
    ("case", "inputs", "config", "disp", "nxt", "reasons"), TABLE, ids=[t[0] for t in TABLE]
)
def test_transition_table(case, inputs, config, disp, nxt, reasons) -> None:  # noqa: ANN001
    decision = decide_escalation(inputs, config)
    assert decision.disposition is disp
    assert decision.next_stage is nxt
    assert decision.final is (nxt is None)
    for reason in reasons:
        assert reason in decision.reasons
    assert decision.policy_version == ESCALATION_POLICY_VERSION


def test_policy_version_in_log_fields_and_log_safe() -> None:
    decision = decide_escalation(inp(arc=arc(0.9), granite=gr(CONSISTENT)))
    fields = decision.log_fields()
    assert fields["escalation_policy_version"] == "mail-escalation-policy@1"
    assert all(isinstance(v, (str, int)) for v in fields.values())
    assert not re.search(r"\d\.\d", " ".join(str(v) for v in fields.values()))  # no scores


@pytest.mark.parametrize("status", [S.UNAVAILABLE, S.FAILED, S.DENIED, S.RATE_LIMITED, S.NOT_RUN])
@pytest.mark.parametrize("granite_op", [CONSISTENT, UNCLEAR])
def test_qwen_absence_never_changes_result(status: S, granite_op: GraniteOpinion) -> None:
    base = inp(arc=arc(0.5), granite=gr(granite_op))
    absent = decide_escalation(base, EscalationConfig(qwen_enabled=False))
    for cfg in (EscalationConfig(qwen_enabled=False), QCFG):
        got = decide_escalation(
            inp(
                arc=arc(0.5),
                granite=gr(granite_op),
                qwen=QwenOutcome(status, None, 0 if status is S.NOT_RUN else 1),
            ),
            cfg,
        )
        if status is S.NOT_RUN and cfg.qwen_enabled:
            assert got.next_stage is Stage.QWEN  # asks for it; disposition unchanged
        assert got.disposition is absent.disposition


@pytest.mark.parametrize("qwen_opinion", [None, *QwenOpinion])
@pytest.mark.parametrize("floor", [D.REVIEW, D.RESTRICTED, D.INCOMPLETE])
def test_qwen_cannot_change_cautious_floor(floor: D, qwen_opinion: QwenOpinion | None) -> None:
    got = decide_escalation(
        inp(floor, arc=arc(0.5), granite=gr(CONSISTENT), qwen=qw(qwen_opinion)), QCFG
    )
    assert got.disposition is floor and got.final


def test_exhaustive_invariants_never_lower_never_restrict_never_allow_on_failure() -> None:
    statuses = list(S)
    for floor, hi, a, g, gop, q, qop, cfg in itertools.product(
        D,
        [frozenset(), HI],
        [ArcOutcome(), *(ArcOutcome(s, x) for s in statuses for x in (None, 0.1, 0.5, 0.9))],
        statuses,
        [None, *GraniteOpinion],
        statuses,
        [None, *QwenOpinion],
        [DEFAULT_CONFIG, QCFG, EscalationConfig(arc_enabled=False, qwen_enabled=True)],
    ):
        gi = inp(
            floor, hi=hi, arc=a, granite=GraniteOutcome(g, gop, 1), qwen=QwenOutcome(q, qop, 1)
        )
        got = decide_escalation(gi, cfg)
        assert RANK[got.disposition] >= RANK[floor]
        if floor not in (D.SAFE_TO_PROCESS, D.LOW_PRIORITY):
            assert got.disposition is floor and got.final
        elif got.disposition is not floor:
            assert got.disposition in (D.REVIEW, D.INCOMPLETE)
        assert got.disposition is not D.RESTRICTED or floor is D.RESTRICTED
        assert got.disposition is not D.SAFE_TO_PROCESS or floor is D.SAFE_TO_PROCESS
        if got.next_stage is not None:
            assert got.next_stage is not Stage.QWEN or cfg.qwen_enabled


@pytest.mark.parametrize("floor", [D.SAFE_TO_PROCESS, D.LOW_PRIORITY])
@pytest.mark.parametrize("failure", [S.UNAVAILABLE, S.FAILED, S.DENIED, S.RATE_LIMITED])
def test_model_failure_when_triggered_is_never_allow(floor: D, failure: S) -> None:
    for arc_out in (ArcOutcome(failure), arc(0.5), arc(0.9)):
        got = decide_escalation(inp(floor, arc=arc_out, granite=GraniteOutcome(failure, None, 1)))
        assert got.disposition in (D.REVIEW, D.INCOMPLETE)


def test_driver_loop_budget_bounded_and_terminates() -> None:
    state = inp(arc=ArcOutcome(), granite=GraniteOutcome(), qwen=QwenOutcome())
    calls = 0
    for _ in range(6):
        step = decide_escalation(state, QCFG)
        if step.final:
            break
        calls += 1
        if step.next_stage is Stage.ARC:
            state = inp(arc=arc(0.5))
        elif step.next_stage is Stage.GRANITE:
            state = inp(arc=arc(0.5), granite=gr(CONSISTENT, n=2))
        else:
            state = inp(
                arc=arc(0.5), granite=gr(CONSISTENT, n=2), qwen=qw(QwenOpinion.DISAGREE, n=2)
            )
    assert step.final and calls == 3
    assert step.disposition is D.REVIEW and step.model_calls_used == 4


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "x",
        EscalationInputs(DeterministicSummary("REVIEW")),  # type: ignore[arg-type]
        EscalationInputs(DeterministicSummary(D.LOW_PRIORITY), evidence_chars=-1),
        EscalationInputs(DeterministicSummary(D.LOW_PRIORITY), evidence_chars=True),  # type: ignore[arg-type]
        EscalationInputs(
            DeterministicSummary(D.LOW_PRIORITY),
            granite=GraniteOutcome(S.COMPLETED, CONSISTENT, -1),
        ),
        EscalationInputs(DeterministicSummary(D.LOW_PRIORITY), window_calls_remaining=-1),
        EscalationInputs(DeterministicSummary(D.LOW_PRIORITY, high_impact=frozenset({"x"}))),  # type: ignore[arg-type]
    ],
)
def test_invalid_input_fails_closed(bad: object) -> None:
    got = decide_escalation(bad)  # type: ignore[arg-type]
    assert got.disposition is D.INCOMPLETE and got.final and R.INPUT_INVALID in got.reasons


def test_invalid_config_rejected() -> None:
    for kw in (
        {"arc_low": 0.9, "arc_high": 0.1},
        {"arc_high": 1.5},
        {"max_model_calls": -1},
        {"arc_low": float("nan")},
    ):
        with pytest.raises(ValueError):
            EscalationConfig(**kw)  # type: ignore[arg-type]
    assert decide_escalation(inp(), "cfg").disposition is D.INCOMPLETE  # type: ignore[arg-type]


def test_module_is_pure_and_not_wired() -> None:
    root = Path(__file__).resolve().parents[2] / "backend"
    source = (root / "mail_security" / "escalation.py").read_text()
    banned = (
        r"^\s*(import|from)\s+"
        r"(socket|ssl|http|requests|httpx|aiohttp|subprocess|urllib|asyncio|logging|os)\b"
    )
    assert not re.search(banned, source, re.MULTILINE)
    for path in root.rglob("*.py"):
        if path.name == "escalation.py":
            continue
        assert "mail_security.escalation" not in path.read_text(), f"{path} wires escalation"
