"""Live-data calibration after full capture (NS-ADR-072).

With raw headers visible, REVIEW jumped from ~12 to 102 of 342 messages. The
drivers were (a) a full-hostname Reply-To/Return-Path comparison that flagged
``news.brand.example`` vs ``brand.example``, (b) Reply-To on another
organization for DMARC-aligned senders (ticketing / ESPs), and (c) Amazon's
own CDN / portal domains read as typosquats. Each fix is narrow; the BEC
shapes keep their REVIEW.
"""
from __future__ import annotations

from datetime import UTC, datetime

from backend.mail_security.auth_evidence import (
    SenderAddresses,
    TrustedReceiverBoundary,
    build_auth_evidence,
    inspect_sender_provenance,
    is_freemail_address,
)
from backend.mail_security.inspections import _is_ascii_typosquat
from backend.mail_security.models import Disposition
from backend.mail_security.policy import PolicyEvidence, decide_policy

GMAIL = TrustedReceiverBoundary(["mx.google.com"], topmost_only=True)
NOW = datetime(2026, 11, 13, tzinfo=UTC)


def _codes(findings) -> set[str]:
    return {f.explanation_code for f in findings}


def test_subdomain_reply_to_and_return_path_are_same_organization() -> None:
    same = inspect_sender_provenance(SenderAddresses(
        from_address="News <news@mail.brand.example>",
        reply_to="help@brand.example", return_path="<bounce@em.brand.example>",
    ))
    assert not _codes(same) & {"REPLY_TO_MISMATCH", "RETURN_PATH_MISMATCH"}
    other = inspect_sender_provenance(SenderAddresses(
        from_address="news@brand.example", reply_to="x@other.example",
    ))
    assert "REPLY_TO_MISMATCH" in _codes(other)


def test_amazon_first_party_domains_are_not_typosquats() -> None:
    for host in ("m.media-amazon.com", "images-na.ssl-images-amazon.com", "atoz.amazon.work"):
        assert not _is_ascii_typosquat(host), host
    for host in ("amazon-login.com", "amaz0n.com", "amazon-security.work"):
        assert _is_ascii_typosquat(host), host


def test_freemail_detection() -> None:
    assert is_freemail_address("Bob <bob@gmail.com>")
    assert is_freemail_address("x@mail.yahoo.com")
    assert not is_freemail_address("support@zendesk.example")
    assert not is_freemail_address(None)


def _decide(*, ar: str | None, reply_to: str, freemail: bool | None = None):
    headers = [("Authentication-Results", ar)] if ar else []
    addresses = SenderAddresses(from_address="Shop <orders@shop.example>", reply_to=reply_to)
    auth = build_auth_evidence(headers, addresses=addresses, boundary=GMAIL)
    return decide_policy(
        PolicyEvidence(
            authentication=auth.authentication,
            findings=list(auth.findings),
            inspection_complete=True,
            reply_to_freemail=is_freemail_address(reply_to) if freemail is None else freemail,
        ),
        clock=lambda: NOW,
    ).policy


PASS = "mx.google.com; dkim=pass header.i=@shop.example; spf=pass; dmarc=pass"


def test_dmarc_aligned_org_reply_to_is_not_impersonation() -> None:
    res = _decide(ar=PASS, reply_to="support@helpdesk.example")
    assert res.disposition is Disposition.LOW_PRIORITY
    assert "REPLY_TO_MISMATCH_DMARC_ALIGNED" in res.reason_codes


def test_bec_shapes_keep_review() -> None:
    # Free-mail Reply-To on an authenticated From: the classic BEC shape.
    assert _decide(ar=PASS, reply_to="ceo.urgent@gmail.com").disposition is Disposition.REVIEW
    # No attested receiver result at all.
    assert _decide(ar=None, reply_to="support@helpdesk.example").disposition is Disposition.REVIEW
    # Provider-reported DMARC fail.
    fail = "mx.google.com; dkim=fail; spf=fail; dmarc=fail"
    assert _decide(ar=fail, reply_to="support@helpdesk.example").disposition is Disposition.REVIEW
    # A forged lower mx.google.com "pass" under a real failing one does not help.
    res = _decide(ar=fail, reply_to="support@helpdesk.example")
    assert "REPLY_TO_MISMATCH_DMARC_ALIGNED" not in res.reason_codes
