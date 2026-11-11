"""NerveShield task 5.5: threat intel vs personal memory vs training boundaries.

Central property: feedback never changes model weights, global indicators or
any store automatically. Plus provenance, scope and consent rules, and static
import boundaries between the three stores.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.mail_security.feedback import (
    Destination as D,
)
from backend.mail_security.feedback import (
    FeedbackError,
    FeedbackLedger,
    FeedbackRecord,
    Scope,
)
from backend.mail_security.feedback import (
    FeedbackKind as K,
)
from backend.mail_security.feedback import (
    FeedbackRefusal as R,
)
from backend.mail_security.feedback import (
    FeedbackSource as S,
)
from backend.mail_security.feedback import (
    ProposalStatus as P,
)
from backend.mail_security.trust_index import TrustIndex

ROOT = Path(__file__).resolve().parents[2]
MS = ROOT / "backend" / "mail_security"
NOW = datetime(2026, 11, 11, tzinfo=UTC)


def _fb(
    n: int = 1, *, kind: K = K.CONFIRMED_THREAT, source: S = S.ACCOUNT_OWNER, **kw: Any
) -> FeedbackRecord:
    args: dict[str, Any] = {
        "feedback_id": f"fb-{n}",
        "account_scope": "gmail:a",
        "assessment_ref": "asm-1",
        "kind": kind,
        "source": source,
        "reviewer_ref": "rev-1",
        "created_at": NOW,
        "indicator_refs": ("finding:link:1",),
    }
    args.update(kw)
    return FeedbackRecord(**args)


# -- routing and provenance --------------------------------------------------------------


def test_confirmed_threat_routes_to_account_intel_and_blocked_training() -> None:
    proposals = FeedbackLedger().record(_fb())
    by_dest = {p.destination: p for p in proposals}
    assert set(by_dest) == {D.THREAT_INTEL, D.TRAINING}
    assert by_dest[D.THREAT_INTEL].scope is Scope.ACCOUNT
    assert by_dest[D.THREAT_INTEL].status is P.PENDING_REVIEW
    assert by_dest[D.TRAINING].status is P.BLOCKED_NO_CONSENT
    assert all(p.feedback_id == "fb-1" for p in proposals)


def test_training_needs_consent_reference() -> None:
    proposals = FeedbackLedger().record(_fb(consent_ref="consent-77"))
    assert {p.destination: p.status for p in proposals}[D.TRAINING] is P.PENDING_REVIEW


def test_sender_known_routes_only_to_personal_memory() -> None:
    proposals = FeedbackLedger().record(_fb(kind=K.SENDER_KNOWN, indicator_refs=()))
    assert [(p.destination, p.scope) for p in proposals] == [(D.PERSONAL_MEMORY, Scope.ACCOUNT)]


def test_false_positive_never_creates_an_intel_proposal() -> None:
    proposals = FeedbackLedger().record(_fb(kind=K.FALSE_POSITIVE))
    assert D.THREAT_INTEL not in {p.destination for p in proposals}


def test_not_sure_routes_nowhere() -> None:
    assert FeedbackLedger().record(_fb(kind=K.NOT_SURE)) == []


def test_intel_needs_indicator_refs() -> None:
    proposals = FeedbackLedger().record(_fb(indicator_refs=()))
    assert D.THREAT_INTEL not in {p.destination for p in proposals}


@pytest.mark.parametrize("source", [S.ACCOUNT_OWNER, S.DELEGATED_REVIEWER])
def test_only_analysts_can_request_global_scope(source: S) -> None:
    proposals = FeedbackLedger().record(_fb(source=source, request_global=True))
    assert {p.destination: p.scope for p in proposals}[D.THREAT_INTEL] is Scope.ACCOUNT


def test_analyst_global_request_still_needs_review() -> None:
    proposals = FeedbackLedger().record(_fb(source=S.ANALYST, request_global=True))
    intel = next(p for p in proposals if p.destination is D.THREAT_INTEL)
    assert intel.scope is Scope.GLOBAL and intel.status is P.PENDING_REVIEW


@pytest.mark.parametrize("source", [S.PROVIDER_LABEL, S.MODEL_OBSERVATION])
def test_weak_signals_are_refused_as_feedback(source: S) -> None:
    with pytest.raises(FeedbackError) as info:
        FeedbackLedger().record(_fb(source=source))
    assert info.value.code is R.WEAK_SOURCE_NOT_FEEDBACK


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"account_scope": "bad scope"}, R.INVALID_ACCOUNT),
        ({"assessment_ref": "has space"}, R.INVALID_REFERENCE),
        ({"indicator_refs": ("http://evil.example/x?y",)}, R.INVALID_REFERENCE),
        ({"indicator_refs": tuple(f"r{i}" for i in range(20))}, R.INVALID_REFERENCE),
        ({"reason_codes": ("free text reason",)}, R.INVALID_CODE),
        ({"created_at": datetime(2026, 1, 1)}, R.NAIVE_TIMESTAMP),
    ],
)
def test_records_hold_codes_and_opaque_refs_only(over: dict[str, Any], code: R) -> None:
    with pytest.raises(FeedbackError) as info:
        FeedbackLedger().record(_fb(**over))
    assert info.value.code is code


def test_same_feedback_id_is_idempotent() -> None:
    ledger = FeedbackLedger()
    first = ledger.record(_fb())
    assert ledger.record(_fb()) == first
    assert len(ledger.records("gmail:a")) == 1


def test_newer_feedback_supersedes_pending_and_keeps_provenance() -> None:
    ledger = FeedbackLedger()
    ledger.record(_fb(1))
    ledger.record(_fb(2, kind=K.FALSE_NEGATIVE))
    intel = ledger.proposals("gmail:a", D.THREAT_INTEL)
    assert [(p.feedback_id, p.status) for p in intel] == [
        ("fb-1", P.SUPERSEDED),
        ("fb-2", P.PENDING_REVIEW),
    ]
    assert intel[0].history == ("PENDING_REVIEW", "SUPERSEDED")
    assert len(ledger.records("gmail:a")) == 2  # append-only


# -- decisions ---------------------------------------------------------------------------


def _intel_id(ledger: FeedbackLedger) -> str:
    return next(p.proposal_id for p in ledger.proposals("gmail:a", D.THREAT_INTEL))


def test_approval_needs_eligible_human_and_reference() -> None:
    ledger = FeedbackLedger()
    ledger.record(_fb())
    pid = _intel_id(ledger)
    for approver in (S.PROVIDER_LABEL, S.MODEL_OBSERVATION):
        with pytest.raises(FeedbackError) as info:
            ledger.decide(
                pid, account_scope="gmail:a", approve=True, approver=approver, approval_ref="ok-1"
            )
        assert info.value.code is R.APPROVER_NOT_ELIGIBLE
    with pytest.raises(FeedbackError):
        ledger.decide(
            pid, account_scope="gmail:a", approve=True, approver=S.ACCOUNT_OWNER, approval_ref=""
        )
    done = ledger.decide(
        pid, account_scope="gmail:a", approve=True, approver=S.ACCOUNT_OWNER, approval_ref="ok-1"
    )
    assert done.status is P.APPROVED and done.decided_by is S.ACCOUNT_OWNER
    with pytest.raises(FeedbackError) as info:
        ledger.decide(
            pid,
            account_scope="gmail:a",
            approve=False,
            approver=S.ACCOUNT_OWNER,
            approval_ref="ok-2",
        )
    assert info.value.code is R.NOT_PENDING


def test_global_intel_needs_an_analyst_approver() -> None:
    ledger = FeedbackLedger()
    ledger.record(_fb(source=S.ANALYST, request_global=True))
    pid = _intel_id(ledger)
    with pytest.raises(FeedbackError) as info:
        ledger.decide(
            pid, account_scope="gmail:a", approve=True, approver=S.ACCOUNT_OWNER, approval_ref="a"
        )
    assert info.value.code is R.APPROVER_NOT_ELIGIBLE
    assert (
        ledger.decide(
            pid, account_scope="gmail:a", approve=True, approver=S.ANALYST, approval_ref="a"
        ).status
        is P.APPROVED
    )


def test_blocked_training_cannot_be_approved() -> None:
    ledger = FeedbackLedger()
    ledger.record(_fb())
    pid = next(p.proposal_id for p in ledger.proposals("gmail:a", D.TRAINING))
    with pytest.raises(FeedbackError) as info:
        ledger.decide(
            pid, account_scope="gmail:a", approve=True, approver=S.ACCOUNT_OWNER, approval_ref="a"
        )
    assert info.value.code is R.NOT_PENDING


def test_accounts_cannot_see_or_decide_each_others_proposals() -> None:
    ledger = FeedbackLedger()
    ledger.record(_fb())
    assert ledger.proposals("gmail:b") == [] and ledger.records("gmail:b") == []
    with pytest.raises(FeedbackError) as info:
        ledger.decide(
            _intel_id(ledger),
            account_scope="gmail:b",
            approve=True,
            approver=S.ANALYST,
            approval_ref="a",
        )
    assert info.value.code is R.WRONG_ACCOUNT


# -- feedback changes nothing automatically ----------------------------------------------


def _tree_digest(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def test_feedback_and_approval_do_not_touch_weights_or_trust() -> None:
    """Runtime half; the intel half is the static import test below (no reference exists)."""
    model_files = [
        p for p in (MS / "classifier").rglob("*") if p.is_file() and "__pycache__" not in p.parts
    ]
    before = _tree_digest(model_files)
    trust = TrustIndex(b"s" * 32, allowed_accounts=["gmail:a"])
    trust_before = trust.export_state()

    ledger = FeedbackLedger()
    for n, kind in enumerate(K, 1):
        for p in ledger.record(
            _fb(n, kind=kind, source=S.ANALYST, request_global=True, consent_ref="c1")
        ):
            if p.status is P.PENDING_REVIEW:
                ledger.decide(
                    p.proposal_id,
                    account_scope="gmail:a",
                    approve=True,
                    approver=S.ANALYST,
                    approval_ref=f"ok-{n}",
                )
    assert trust.export_state() == trust_before
    assert _tree_digest(model_files) == before
    assert any(p.status is P.APPROVED for p in ledger.proposals("gmail:a"))


# -- static store boundaries -------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names |= {f"{node.module}.{a.name}" for a in node.names}
    return names


INTEL = {
    "backend.mail_security.intel",
    "backend.mail_security.intel_wiring",
    "backend.mail_security.feeds",
    "backend.persistence.repositories.mail_intel_repo",
}
MEMORY = {"backend.mail_security.trust_index", "backend.knowledge", "backend.communications"}
TRAINING = {"backend.mail_security.classifier"}
MODELS = {
    "backend.mail_security.granite_observer",
    "backend.mail_security.qwen_challenger",
    "backend.mail_security.classifier",
}


def _touches(path: Path, forbidden: set[str]) -> set[str]:
    return {i for i in _imports(path) if any(i == f or i.startswith(f + ".") for f in forbidden)}


def test_feedback_module_imports_no_store_or_model() -> None:
    assert _touches(MS / "feedback.py", INTEL | MEMORY | TRAINING | MODELS) == set()


def test_trust_index_imports_no_intel_training_or_model_code() -> None:
    assert (
        _touches(MS / "trust_index.py", INTEL | TRAINING | MODELS | {"backend.knowledge"}) == set()
    )


def test_intel_store_imports_no_personal_memory_or_feedback() -> None:
    for name in ("intel.py", "intel_wiring.py", "feeds.py"):
        assert _touches(MS / name, MEMORY | {"backend.mail_security.feedback"}) == set(), name
    repo = ROOT / "backend/persistence/repositories/mail_intel_repo.py"
    assert _touches(repo, MEMORY | {"backend.mail_security.feedback"}) == set()


def test_classifier_imports_no_feedback_memory_or_intel() -> None:
    for path in (MS / "classifier").rglob("*.py"):
        assert (
            _touches(
                path,
                INTEL
                | MEMORY
                | {"backend.mail_security.feedback", "backend.mail_security.takeout_labels"},
            )
            == set()
        ), path


def test_only_training_selection_reads_feedback() -> None:
    """6.1 reads APPROVED training proposals; nothing else imports feedback yet."""
    allowed = {"feedback.py", "training_selection.py"}
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "backend").rglob("*.py")
        if p.name not in allowed and _touches(p, {"backend.mail_security.feedback"})
    ]
    assert offenders == []


def test_training_selection_imports_no_store_or_model() -> None:
    forbidden = (
        INTEL
        | MEMORY
        | MODELS
        | {"backend.mail_security.takeout_labels", "backend.persistence", "backend.storage"}
    )
    assert _touches(MS / "training_selection.py", forbidden) == set()
