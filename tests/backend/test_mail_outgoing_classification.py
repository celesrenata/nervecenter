"""Gmail label classification: sent mail is OUTGOING, drafts never ingest (NS-ADR-066).

Live defect: the Gmail history poll has no INBOX filter, so the owner's own sent
mail (and drafts) were ingested as INCOMING communications from the owner and
assessed as inbound threats (304 of 348 messages on one account were SENT).
"""

from __future__ import annotations

import base64
from typing import Any

import _mail_security_harness_helpers as H
import pytest
from test_mail_security_full_capture import RawFakeGmail
from test_mail_transport import (
    _ACCOUNT,
    _connector,
    _date,
    _gentry,
    _install_gmail,
    _plain_payload,
)

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.connectors.mail.mail_transport import (
    GmailClient,
    MailItem,
    StubMailTransport,
    is_outgoing_labels,
)
from backend.ingest.mail_worker import (
    DefaultIngestSeam,
    MailIngestWorker,
    _content_key,
    _parse_recipients,
    _raw_content_key,
)
from backend.mail_security import telemetry
from backend.mail_security.ingestion_seam import (
    ENV_MAIL_SECURITY_ENABLED,
    ENV_MAIL_SECURITY_FULL_CAPTURE,
    set_mail_security_repository,
)

OWNER = "owner@example.com"


def _resource(mid: str, labels: list[str], **hdrs: str) -> dict[str, Any]:
    headers = {
        "From": hdrs.get("From", f"Owner <{OWNER}>"),
        "To": hdrs.get("To", "Bob <bob@partner.example>"),
        "Subject": "subj",
        "Date": _date(1),
        "Message-ID": f"<{mid}@mail>",
        "References": "<root@partner.example>",
        "Cc": "carol@other.example",
        "X-Secret-Header": "dropped",
    }
    return {
        "id": mid,
        "labelIds": labels,
        "payload": {
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            **_plain_payload("I'll send it Friday."),
        },
    }


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


@pytest.mark.parametrize(
    ("labels", "outgoing"),
    [
        (("SENT",), True),
        (("SENT", "IMPORTANT"), True),
        (("SENT", "INBOX"), False),  # self-addressed copy stays incoming
        (("INBOX",), False),
        (("SPAM",), False),
        ((), False),
    ],
)
def test_outgoing_label_rule(labels, outgoing) -> None:
    assert is_outgoing_labels(labels) is outgoing


async def test_transport_classifies_by_labels(monkeypatch) -> None:
    fake = RawFakeGmail(
        raw={m: (200, {"raw": _b64(b"From: x\r\n\r\nbody")}) for m in ("in1", "sent1", "draft1")},
        entries=[_gentry(1, 5, "in1"), _gentry(2, 5, "sent1"), _gentry(3, 5, "draft1")],
        messages={
            "in1": (
                200,
                _resource("in1", ["INBOX", "UNREAD"], From="Bob <bob@partner.example>", To=OWNER),
            ),
            "sent1": (200, _resource("sent1", ["SENT"])),
            "draft1": (200, _resource("draft1", ["DRAFT"])),
        },
        head_history_id=5,
    )
    _install_gmail(monkeypatch, fake)
    items, cursor = await GmailClient(capture_raw=True).run(
        _connector(),
        {"history_id": 5, "account": _ACCOUNT.lower()},
        credentials={"access_token": "tok"},
    )
    by_id = {i.message_id: i for i in items}
    assert set(by_id) == {"gmail:in1", "gmail:sent1"}  # the draft is never delivered
    assert by_id["gmail:sent1"].outgoing and not by_id["gmail:in1"].outgoing
    # Outgoing mail gets no raw fetch; inbound does.
    assert fake.raw_calls == ["in1"]
    assert by_id["gmail:sent1"].raw_bytes is None
    # The draft is marked seen so it is never re-fetched.
    assert any(k.endswith(":gmail:draft1") for k in cursor["seen"])
    # Envelope threading/addressing headers are kept; unknown ones are not.
    hdrs = by_id["gmail:sent1"].headers
    assert hdrs["References"] == "<root@partner.example>"
    assert hdrs["Cc"] == "carol@other.example"
    assert "X-Secret-Header" not in hdrs


def test_parse_recipients_orders_and_dedupes() -> None:
    got = _parse_recipients(
        {
            "To": "Bob <BOB@partner.example>, undisclosed-recipients:;",
            "Cc": "bob@partner.example, Carol <carol@other.example>",
        }
    )
    assert got == [("BOB@partner.example", "Bob"), ("carol@other.example", "Carol")]


# --------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------
@pytest.fixture
async def harness(tmp_path):
    h = await H.make_harness(tmp_path / "outgoing.sqlite")
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


def _item(mid: str, labels: tuple[str, ...], raw: bytes | None = None) -> MailItem:
    return MailItem(
        message_id=mid,
        account=H.ACCOUNT,
        subject="Re: plan",
        content_bytes=b"I'll send it Friday.\n",
        timestamp=H.TS,
        headers={
            "From": f"Owner <{H.ACCOUNT}>",
            "To": "Bob <bob@partner.example>",
            "Message-ID": mid,
            "Date": "Mon, 27 Oct 2026 09:00:00 +0000",
        },
        source="gmail",
        raw_bytes=raw,
        labels=labels,
    )


async def _ingest(harness, monkeypatch, item: MailItem) -> None:
    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
    monkeypatch.setenv(ENV_MAIL_SECURITY_FULL_CAPTURE, "1")
    set_mail_security_repository(harness.registry.mail_security)
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


async def _rows(harness) -> list[Any]:
    return list(await harness.registry.communications.list_communications())


async def test_sent_mail_is_outgoing_to_the_recipient(harness, monkeypatch) -> None:
    mid = "gmail:sent-1"
    # Even if raw bytes were somehow present, outgoing mail is never raw-stored.
    await _ingest(harness, monkeypatch, _item(mid, ("SENT",), raw=b"From: x\r\n\r\nb"))
    (row,) = await _rows(harness)
    assert row.direction == "outgoing"
    people = {p.id: p for p in await harness.registry.communications.list_people()}
    idents = await harness.registry.communications.list_identities(row.person_id)
    assert [i.handle.lower() for i in idents] == ["bob@partner.example"]
    assert people[row.person_id].display_name == "Bob"
    # No inbound security job and no raw object for the owner's own mail.
    assert await harness.registry.mail_security.list_jobs(harness.scope) == []
    assert not await harness.store.exists(_raw_content_key(harness.conn.id, mid))
    assert await harness.store.exists(_content_key(harness.conn.id, mid))


async def test_inbox_mail_is_unchanged(harness, monkeypatch) -> None:
    item = MailItem(
        message_id="gmail:in-1",
        account=H.ACCOUNT,
        subject="Hello",
        content_bytes=b"hi\n",
        timestamp=H.TS,
        headers={"From": "Bob <bob@partner.example>", "To": H.ACCOUNT, "Message-ID": "gmail:in-1"},
        source="gmail",
        labels=("INBOX",),
    )
    await _ingest(harness, monkeypatch, item)
    (row,) = await _rows(harness)
    assert row.direction == "incoming"
    assert len(await harness.registry.mail_security.list_jobs(harness.scope)) == 1


async def test_self_addressed_copy_stays_incoming(harness, monkeypatch) -> None:
    await _ingest(harness, monkeypatch, _item("gmail:self-1", ("SENT", "INBOX")))
    (row,) = await _rows(harness)
    assert row.direction == "incoming"
    assert len(await harness.registry.mail_security.list_jobs(harness.scope)) == 1


async def test_backfill_never_schedules_outgoing(harness, monkeypatch) -> None:
    await _ingest(harness, monkeypatch, _item("gmail:sent-2", ("SENT",)))
    rows = await harness.registry.communications.list_ref_identifiers(
        ref_prefix=f"content://mail/{harness.conn.id}/", limit=50
    )
    # The backfill provider reads only this query, so outgoing is never scheduled.
    assert rows == []


async def test_sent_promise_becomes_an_outgoing_commitment(harness, monkeypatch) -> None:
    """End to end through the real worker + detection pipeline: a promise in
    the owner's sent mail is a COMMITMENT on an OUTGOING communication
    attributed to the recipient (it was a misread incoming request before
    NS-ADR-066)."""
    seam = DefaultIngestSeam(communications_store=harness.registry.communications)
    results: list[Any] = []
    run = seam.run_pipeline
    monkeypatch.setattr(
        seam,
        "run_pipeline",
        lambda c, *, now: results.append((c, run(c, now=now))) or results[-1][1],
    )
    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
    set_mail_security_repository(harness.registry.mail_security)
    await MailIngestWorker(
        connection_id=harness.conn.id,
        cursor_kind="gmail_history",
        connections=harness.registry.connections,
        imports=harness.registry.imports,
        object_store=harness.store,
        transport=StubMailTransport(
            items=[_item("gmail:promise-1", ("SENT",))], next_cursor={"history_id": 9}
        ),
        seam=seam,
        poll_interval_s=30.0,
    ).poll_once()
    ((comm, result),) = results
    assert comm.direction.value == "outgoing"
    report = seam.pipeline.detection.report_for(comm.communication_id)
    kinds = sorted({f.kind for f in report.findings})
    assert "commitment" in kinds and "request" not in kinds
    assert result.finding_ids
