"""FEAT-002 — deterministic relevance classification + scoring.

Table-driven coverage of
:mod:`backend.communications.detection.relevance`:

- ``classify_sender`` sorts real senders into person / machine / bulk:
  no-reply/automated addresses (login.gov / apple / chase) are ``machine``;
  ESP / marketing-subdomain senders (walmart / shein / torrid / modcloth /
  golo / ccsend) are ``bulk``; a plain person is ``person``.
- Flags-only bulk (a ``List-Unsubscribe`` header with no marketing sender) is
  still bulk.
- ``relevance_importance`` is tiered, not flat: a VIP person request scores 9,
  a plain person deadline ~7, machine ≤ 2, bulk ≤ 1.
- The far-future deadline sanity (2027) is enforced at the mint layer
  (``wiring``); the scorer's ceiling for machine/bulk is enforced here.

All pure/deterministic — no DB, no network.
"""

from __future__ import annotations

import pytest

# Circular-import-safe entry (see test_detection.py): force backend.api.app to
# fully initialize before the detection package is imported directly.
from backend.api.app import create_app  # noqa: F401
from backend.communications.detection.relevance import (
    FAR_FUTURE_DAYS,
    NEEDS_YOU_MIN_IMPORTANCE,
    Classification,
    classify_sender,
    is_no_reply_sender,
    relevance_importance,
    relevance_reason,
)


# -- classify_sender: sender class ------------------------------------------


@pytest.mark.parametrize(
    ("sender", "expected_class"),
    [
        # no-reply / automated → machine
        ("no-reply@login.gov", "machine"),
        ("noreply@apple.com", "machine"),
        ("donotreply@chase.com", "machine"),
        ("alerts@chase.com", "machine"),
        ("notifications@github.com", "machine"),
        # ESP / marketing-subdomain → bulk
        ("news@em.walmart.com", "bulk"),
        ("deals@news.emailmarket.shein.com", "bulk"),
        ("sales@email.torrid.com", "bulk"),
        ("team@marketing.golo.com", "bulk"),
        ("campaign@customer.ccsend.com", "bulk"),
        ("newsletter@acme.com", "bulk"),
        # plain person → person
        ("dana@example.com", "person"),
        ("Alice <alice@example.com>", "person"),
        ("boss@corp.com", "person"),
    ],
)
def test_classify_sender_class(sender, expected_class):
    cls = classify_sender(sender)
    assert isinstance(cls, Classification)
    assert cls.sender_class == expected_class
    # Every verdict is explainable.
    assert cls.reasons, f"no reason recorded for {sender!r}"


def test_no_reply_sets_machine_and_no_reply_flag():
    cls = classify_sender("no-reply@login.gov")
    assert cls.sender_class == "machine"
    assert cls.no_reply is True
    assert cls.automated is True
    assert cls.bulk is False


def test_bulk_takes_precedence_over_no_reply():
    """A no-reply address on an ESP domain is bulk (bulk is the stronger
    class)."""
    cls = classify_sender("noreply@customer.ccsend.com")
    assert cls.sender_class == "bulk"
    assert cls.bulk is True
    assert cls.no_reply is True


def test_flags_only_bulk_via_list_unsubscribe():
    """A sender with no marketing name but a ``List-Unsubscribe`` header (the
    FEAT-001 ``bulk`` flag) is still bulk."""
    cls = classify_sender("hello@acme.com", flags={"bulk": True, "automated": True})
    assert cls.sender_class == "bulk"
    assert cls.bulk is True


def test_is_no_reply_sender_helper():
    assert is_no_reply_sender("no-reply", "login.gov") is True
    assert is_no_reply_sender("no.reply", "x.com") is True
    assert is_no_reply_sender("donotreply", "chase.com") is True
    assert is_no_reply_sender("mailer-daemon", "x.com") is True
    assert is_no_reply_sender("dana", "example.com") is False


# -- relevance_importance: tiered, not flat ---------------------------------


def test_vip_person_request_scores_high():
    """A genuine VIP person request scores 9 (base 6 + VIP 2 + actionable 1)."""
    person = classify_sender("vip@example.com")
    assert person.sender_class == "person"
    score = relevance_importance("request", 0.8, person, is_vip=True)
    assert score == 9


def test_plain_person_deadline_scores_mid_high():
    """A plain person deadline scores ~7 (base 6 + actionable 1)."""
    person = classify_sender("dana@example.com")
    score = relevance_importance("deadline", 0.8, person, is_vip=False)
    assert score == 7


def test_high_confidence_person_request_adds_a_point():
    person = classify_sender("dana@example.com")
    assert relevance_importance("request", 0.95, person, is_vip=False) == 8


def test_machine_request_is_capped_low():
    machine = classify_sender("no-reply@login.gov")
    # Even an actionable, high-confidence finding never climbs above 2.
    score = relevance_importance("request", 0.95, machine, is_vip=False)
    assert score <= 2
    assert score < NEEDS_YOU_MIN_IMPORTANCE


def test_bulk_is_capped_at_one():
    bulk = classify_sender("news@em.walmart.com")
    score = relevance_importance("request", 0.95, bulk, is_vip=True)
    assert score == 1
    assert score < NEEDS_YOU_MIN_IMPORTANCE


def test_vip_does_not_lift_machine_or_bulk():
    """The VIP bonus only applies to persons; a machine/bulk VIP stays low."""
    machine = classify_sender("alerts@chase.com")
    assert relevance_importance("request", 0.95, machine, is_vip=True) <= 2


# -- relevance_reason: short, honest, §7-safe --------------------------------


def test_relevance_reason_is_templated_and_body_free():
    person = classify_sender("dana@example.com")
    machine = classify_sender("no-reply@login.gov")
    bulk = classify_sender("news@em.walmart.com")
    assert "person" in relevance_reason("request", person, is_vip=False).lower()
    assert "vip" in relevance_reason("request", person, is_vip=True).lower()
    assert "no-reply" in relevance_reason("request", machine, is_vip=False).lower()
    assert "bulk" in relevance_reason("deadline", bulk, is_vip=False).lower()


def test_constants_are_the_documented_floors():
    assert NEEDS_YOU_MIN_IMPORTANCE == 4
    assert FAR_FUTURE_DAYS == 400
