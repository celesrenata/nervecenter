"""SHADOW conversation-continuity ledger (NS-ADR-067, G8 item 2).

Pinned here:
- headers are attacker-controlled: only a parent in THIS account's ledger
  whose participants include the sender is CONTINUOUS;
- the owner's sent mail makes replies to it judgeable (recipients become
  participants);
- lookalike participants are BROKEN, new participants SUSPECT, unknown
  parents abstain (UNKNOWN), and nothing ever means "safe";
- per-account salting: one account's ledger never answers for another's;
- the ledger stores fingerprints and registrable domains only;
- SUSPECT/BROKEN only add a review-queue reason; the disposition is unchanged;
- without the ledger key the whole thing is inert.
"""
from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from typing import Any

import _mail_security_harness_helpers as H
import pytest
from sqlalchemy import select

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.connectors.mail.mail_transport import MailItem, StubMailTransport
from backend.ingest.mail_worker import DefaultIngestSeam, MailIngestWorker
from backend.mail_security import telemetry
from backend.mail_security.ingestion_seam import (
    ENV_MAIL_SECURITY_ENABLED,
    set_mail_security_repository,
)
from backend.mail_security.review_queue import ImpactTier, QueueReason, build_queue
from backend.mail_security.thread_ledger import (
    ENV_LEDGER_KEY,
    Entry,
    Signal,
    Status,
    account_salt,
    assess,
    fingerprint,
    ledger_key_from_env,
    observe,
    set_thread_ledger_repository,
)
from backend.persistence.models.mail_security import MailThreadLedgerRow
from backend.persistence.repositories.mail_security_review_repo import load_queue_candidates
from backend.persistence.repositories.mail_security_thread_repo import (
    MailThreadLedgerRepository,
)

KEY = base64.b64encode(os.urandom(32)).decode()
OWNER = H.ACCOUNT


def _hdrs(mid: str, frm: str, *, to: str = OWNER, refs: str = "", irt: str = "",
          reply_to: str = "", cc: str = "") -> dict[str, str]:
    h = {"Message-ID": f"<{mid}>", "From": frm, "To": to}
    for k, v in (("References", refs), ("In-Reply-To", irt), ("Reply-To", reply_to), ("Cc", cc)):
        if v:
            h[k] = v
    return h


# --------------------------------------------------------------------------
# pure
# --------------------------------------------------------------------------
def test_key_parsing_is_strict() -> None:
    assert ledger_key_from_env({}) is None
    assert ledger_key_from_env({ENV_LEDGER_KEY: "not base64!"}) is None
    assert ledger_key_from_env({ENV_LEDGER_KEY: base64.b64encode(b"short").decode()}) is None
    assert ledger_key_from_env({ENV_LEDGER_KEY: KEY}) == base64.b64decode(KEY)


def test_fingerprints_are_normalized_and_salted_per_account() -> None:
    key = base64.b64decode(KEY)
    a, b = account_salt(key, "conn:a"), account_salt(key, "conn:b")
    assert fingerprint(a, "<X@Host>") == fingerprint(a, " x@host ")
    assert fingerprint(a, "<x@host>") != fingerprint(b, "<x@host>")


def test_observe_reduces_to_domains_and_ids() -> None:
    assert observe({"From": "a@b.example"}, subject="hi", outgoing=False) is None
    o = observe(
        _hdrs("m1@x", f"Owner <{OWNER}>", to="Bob <bob@mail.partner.example>",
              cc="carol@other.example", refs="<r1@x> <r2@x>", irt="<r2@x>"),
        subject="Re: plan", outgoing=True,
    )
    assert o.message_id == "<m1@x>"
    assert o.parent_ids == ("<r1@x>", "<r2@x>")
    assert o.participant_domains == ("partner.example", "other.example")
    assert o.sender_domain == "example.com" and o.reply_subject


def _in(frm: str, **kw: Any):
    return observe(_hdrs("in@x", frm, refs="<p@x>", **kw), subject="Re: plan", outgoing=False)


SENT = Entry("t", outgoing=True, sender_domain="example.com", reply_to_domain=None,
             participant_domains=("partner.example",))


def test_assess_outcomes() -> None:
    plain = observe(_hdrs("n@x", "a@partner.example"), subject="hello", outgoing=False)
    assert assess(plain, None, ledger_empty=False).status is Status.NOT_A_REPLY
    assert assess(_in("bob@partner.example"), None, ledger_empty=False).status is Status.UNKNOWN
    ok = assess(_in("bob@partner.example"), [SENT], ledger_empty=False)
    assert ok.status is Status.CONTINUOUS
    look = assess(_in("bob@partmer.example"), [SENT], ledger_empty=False)
    assert look.status is Status.BROKEN and Signal.LOOKALIKE_OF_PARTICIPANT in look.signals
    new = assess(_in("dave@unrelated.example"), [SENT], ledger_empty=False)
    assert new.status is Status.SUSPECT and new.signals == (Signal.NEW_PARTICIPANT_DOMAIN,)
    rt = assess(_in("bob@partner.example", reply_to="x@attacker.example"), [SENT],
                ledger_empty=False)
    assert rt.status is Status.SUSPECT and rt.signals == (Signal.REPLY_TO_DIVERGES,)
    both = assess(_in("dave@unrelated.example", reply_to="x@attacker.example"), [SENT],
                  ledger_empty=False)
    assert both.status is Status.BROKEN
    # "Re:" without references abstains; fabricated refs abstain.
    re_only = observe(_hdrs("r@x", "a@partner.example"), subject="RE: invoice", outgoing=False)
    assert assess(re_only, None, ledger_empty=False).status is Status.UNKNOWN


# --------------------------------------------------------------------------
# repository
# --------------------------------------------------------------------------
@pytest.fixture
async def harness(tmp_path):
    from backend.persistence.models.mail_security import (
        MailSecurityFeedbackRow,
        MailSecurityHeldPromotionRow,
    )
    from backend.persistence.models.personal import CommitmentRow, FollowupRow

    h = await H.make_harness(tmp_path / "ledger.sqlite")
    extra = [MailSecurityFeedbackRow.__table__, MailSecurityHeldPromotionRow.__table__,
             CommitmentRow.__table__, FollowupRow.__table__]
    async with h.engine.begin() as conn:
        await conn.run_sync(lambda sc: extra[0].metadata.create_all(sc, tables=extra))
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


async def test_repo_threads_isolation_and_replay(harness) -> None:
    repo = MailThreadLedgerRepository(harness.registry.session_factory)
    key = base64.b64decode(KEY)
    a, b = "conn:a", "conn:b"
    sent = observe(_hdrs("s1@x", f"Owner <{OWNER}>", to="bob@partner.example"),
                   subject="plan", outgoing=True)
    assert await repo.record(a, account_salt(key, a), sent) is None  # outgoing: no verdict
    reply = observe(_hdrs("r1@x", "bob@partner.example", irt="<s1@x>"),
                    subject="Re: plan", outgoing=False)
    res = await repo.record(a, account_salt(key, a), reply, content_ref="content://mail/a/1")
    assert res.status is Status.CONTINUOUS
    # Replay is idempotent: no verdict, no second row.
    assert await repo.record(a, account_salt(key, a), reply) is None
    # Account b never saw s1: the same reply abstains there.
    res_b = await repo.record(b, account_salt(key, b), reply, content_ref="content://mail/b/1")
    assert res_b.status is Status.UNKNOWN
    counts = await repo.status_counts(a)
    assert counts == {"OUT:-": 1, "IN:CONTINUOUS": 1}

    # Privacy: no local parts, addresses, subjects or raw ids are stored.
    async with harness.registry.session_factory() as s:
        rows = (await s.execute(select(MailThreadLedgerRow))).scalars().all()
    blob = repr([(r.message_fp, r.thread_fp, r.sender_domain, r.reply_to_domain,
                  r.participant_domains, r.signals) for r in rows])
    for needle in ("bob", "owner", "user@", "plan", "s1@x", "r1@x"):
        assert needle not in blob


# --------------------------------------------------------------------------
# worker -> ledger -> review queue
# --------------------------------------------------------------------------
def _item(mid: str, headers: dict[str, str], labels: tuple[str, ...], subject: str) -> MailItem:
    return MailItem(
        message_id=f"gmail:{mid}", account=OWNER, subject=subject,
        content_bytes=b"Please use the new account.\n", timestamp=H.TS,
        headers={**headers, "Date": "Mon, 27 Oct 2026 09:00:00 +0000"},
        source="gmail", labels=labels,
    )


async def _ingest(harness, monkeypatch, items: list[MailItem], *, key: bool = True) -> None:
    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
    if key:
        monkeypatch.setenv(ENV_LEDGER_KEY, KEY)
    else:
        monkeypatch.delenv(ENV_LEDGER_KEY, raising=False)
    set_mail_security_repository(harness.registry.mail_security)
    set_thread_ledger_repository(MailThreadLedgerRepository(harness.registry.session_factory))
    for item in items:
        await MailIngestWorker(
            connection_id=harness.conn.id,
            cursor_kind="gmail_history",
            connections=harness.registry.connections,
            imports=harness.registry.imports,
            object_store=harness.store,
            transport=StubMailTransport(items=[item], next_cursor={"history_id": 9}),
            seam=DefaultIngestSeam(communications_store=harness.registry.communications),
            poll_interval_s=30.0,
        ).poll_once()


def _thread(lookalike: str) -> list[MailItem]:
    sent = _item("s1", _hdrs("s1@mail.example.com", f"Owner <{OWNER}>",
                             to="Bob <bob@partner.example>"), ("SENT",), "Invoice")
    reply = _item("r1", _hdrs("r1@mx", f"Bob <bob@{lookalike}>", irt="<s1@mail.example.com>",
                              refs="<s1@mail.example.com>"), ("INBOX",), "Re: Invoice")
    return [sent, reply]


async def test_broken_reply_reaches_review_queue_without_changing_disposition(
    harness, monkeypatch
) -> None:
    await _ingest(harness, monkeypatch, _thread("partmer.example"))
    async with harness.registry.session_factory() as s:
        rows = (await s.execute(select(MailThreadLedgerRow))).scalars().all()
    assert sorted((r.direction, r.status) for r in rows) == [("IN", "BROKEN"), ("OUT", None)]
    await H.security_worker(harness).process_one()
    (asmt,) = await harness.registry.mail_security.list_assessments(harness.scope)
    candidates = await load_queue_candidates(harness.registry.session_factory, harness.scope)
    (cand,) = candidates
    assert cand.thread_continuity == "BROKEN"
    assert cand.disposition == asmt.disposition  # SHADOW: unchanged
    result = build_queue(candidates, now=datetime(2026, 10, 27, 12, tzinfo=UTC))
    (item,) = result.items
    assert QueueReason.THREAD_BROKEN in item.reasons
    assert item.tier in (ImpactTier.MEDIUM, ImpactTier.HIGH, ImpactTier.CRITICAL)


async def test_genuine_reply_is_continuous_and_not_queued(harness, monkeypatch) -> None:
    await _ingest(harness, monkeypatch, _thread("partner.example"))
    async with harness.registry.session_factory() as s:
        statuses = [r.status for r in (await s.execute(select(MailThreadLedgerRow))).scalars()]
    assert "CONTINUOUS" in statuses
    await H.security_worker(harness).process_one()
    candidates = await load_queue_candidates(harness.registry.session_factory, harness.scope)
    assert all(c.thread_continuity is None for c in candidates)


async def test_without_key_the_ledger_is_inert(harness, monkeypatch) -> None:
    await _ingest(harness, monkeypatch, _thread("partmer.example"), key=False)
    async with harness.registry.session_factory() as s:
        assert (await s.execute(select(MailThreadLedgerRow))).first() is None
