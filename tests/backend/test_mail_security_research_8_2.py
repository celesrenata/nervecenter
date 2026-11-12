"""Spike 8.2: thread continuity — synthetic high-signal tests + FP analysis guards."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.mail_security.research import thread_continuity as tc
from backend.mail_security.research.thread_continuity_eval import evaluate

ROOT = Path(__file__).resolve().parents[2] / "backend"


def _ledger_with_thread(*, auth: bool | None = True, payee: str | None = None):
    led = tc.Ledger(salt=b"s" * 16)
    led.add(
        tc.Observed(
            "<a@acme-corp.com>", "pat@acme-corp.com", "project", auth_pass=auth, payee_fp=payee
        )
    )
    led.add(
        tc.Observed(
            "<b@acme-corp.com>",
            "pat@acme-corp.com",
            "Re: project",
            in_reply_to="<a@acme-corp.com>",
            references=("<a@acme-corp.com>",),
            auth_pass=auth,
            payee_fp=payee,
        )
    )
    return led


CITE = dict(
    subject="Re: project",
    in_reply_to="<b@acme-corp.com>",
    references=("<a@acme-corp.com>", "<b@acme-corp.com>"),
)


def test_genuine_reply_is_continuous_and_never_called_safe():
    r = tc.assess(
        tc.Observed("<c@x>", "pat@acme-corp.com", auth_pass=True, **CITE), _ledger_with_thread()
    )
    assert r.status is tc.Status.CONTINUOUS and r.signals == ()
    assert "SAFE" not in {s.value for s in tc.Status}


def test_cited_real_thread_from_lookalike_is_broken():
    r = tc.assess(
        tc.Observed("<c@x>", "pat@acme-c0rp.com", auth_pass=True, **CITE), _ledger_with_thread()
    )
    assert r.status is tc.Status.BROKEN and tc.Signal.LOOKALIKE_OF_PARTICIPANT in r.signals


def test_compromised_authenticated_sender_changing_payee_is_broken():
    r = tc.assess(
        tc.Observed("<c@x>", "pat@acme-corp.com", auth_pass=True, payee_fp="NEW", **CITE),
        _ledger_with_thread(payee="OLD"),
    )
    assert r.status is tc.Status.BROKEN and r.signals == (tc.Signal.PAYMENT_DETAILS_CHANGED,)


def test_forged_participant_with_attacker_reply_to_is_broken():
    r = tc.assess(
        tc.Observed(
            "<c@x>", "pat@acme-corp.com", auth_pass=False, reply_to="pat@invoice-portal.net", **CITE
        ),
        _ledger_with_thread(),
    )
    assert r.status is tc.Status.BROKEN
    assert set(r.signals) == {tc.Signal.AUTH_DOWNGRADE, tc.Signal.REPLY_TO_DIVERGES}


def test_forwarding_does_not_count_as_downgrade():
    r = tc.assess(
        tc.Observed("<c@x>", "pat@acme-corp.com", auth_pass=False, forwarded=True, **CITE),
        _ledger_with_thread(),
    )
    assert r.status is tc.Status.CONTINUOUS


def test_fabricated_headers_never_prove_continuity():
    led = _ledger_with_thread()
    fake = tc.Observed(
        "<c@x>",
        "pat@acme-corp.com",
        subject="Re: project",
        in_reply_to="<never@acme-corp.com>",
        references=("<never@acme-corp.com>",),
        auth_pass=True,
    )
    r = tc.assess(fake, led)
    assert r.status is tc.Status.UNKNOWN and tc.Signal.UNKNOWN_PARENT in r.signals
    assert tc.assess(fake, tc.Ledger(salt=b"t" * 16)).status is tc.Status.UNKNOWN  # cold


def test_ledger_stores_fingerprints_not_ids_or_addresses():
    led = _ledger_with_thread()
    blob = repr(led._by_fp) + repr(led._threads)
    for needle in ("<a@acme-corp.com>", "a@acme-corp.com", "pat@", "project"):
        assert needle not in blob
    assert tc.fingerprint(b"s" * 16, "<A@ACME-corp.com>") == tc.fingerprint(
        b"s" * 16, "a@acme-corp.com"
    )
    assert tc.fingerprint(b"x" * 16, "a@b") != tc.fingerprint(b"y" * 16, "a@b")


def test_synthetic_evaluation_targets():
    rep = evaluate(seed=7, per_kind=200)
    c = rep["categories"]
    for kind in ("A_LOOKALIKE", "A_COMPROMISED_PAYMENT"):
        assert c[kind]["broken_rate"] >= 0.95, kind
    assert c["A_SPOOFED_FROM_REPLY_TO"]["flagged_rate"] == 1.0
    for kind in ("L_REPLY", "L_LIST_FORWARD", "L_NEVER_AUTH", "L_TICKET_REPLY_TO"):
        assert c[kind]["flagged_rate"] == 0.0, kind
    assert c["L_NEW_COLLEAGUE"]["broken_rate"] == 0.0
    assert rep["legit_broken_rate_excl_vendor_change"] == 0.0
    # Documented limits (must stay visible, not silently "fixed").
    assert c["A_FABRICATED_REFS"]["distribution"] == {"UNKNOWN": 200}
    assert c["L_NEW_COLLEAGUE"]["distribution"] == c["A_UNRELATED_INJECT"]["distribution"]


def test_evaluation_is_deterministic():
    assert evaluate(seed=3, per_kind=50) == evaluate(seed=3, per_kind=50)


def test_research_package_is_not_imported_by_runtime():
    offenders = []
    for path in ROOT.rglob("*.py"):
        if "research" in path.parts or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            mod = node.module if isinstance(node, ast.ImportFrom) else None
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if (mod and "mail_security.research" in mod) or any(
                "mail_security.research" in n for n in names
            ):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


@pytest.mark.parametrize("subject", ["Re: x", "RE: x", "AW: x", "Fwd: x", "Re[2]: x"])
def test_reply_subject_forms(subject):
    r = tc.assess(tc.Observed("<c@x>", "a@b.com", subject=subject), _ledger_with_thread())
    assert tc.Signal.REPLY_SUBJECT_WITHOUT_REFERENCES in r.signals
