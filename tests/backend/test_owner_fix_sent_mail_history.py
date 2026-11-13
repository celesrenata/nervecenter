"""Owner tool fix_sent_mail_history._apply (NS-ADR-068) on a durable harness."""
from __future__ import annotations

from typing import Any

import _mail_security_harness_helpers as H
import pytest
from sqlalchemy import select

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.connectors.mail.mail_transport import MailItem
from backend.ingest.mail_worker import _communication_id, _content_key
from backend.mail_security import telemetry
from backend.owner_tools.fix_sent_mail_history import _apply
from backend.persistence.models.mail_security import (
    MailSecurityAssessmentRow,
    MailSecurityJobRow,
)
from backend.persistence.models.personal import (
    AttentionItemRow,
    CommunicationRow,
    PersonIdentityRow,
)


@pytest.fixture
async def harness(tmp_path):
    from backend.persistence.models.mail_security import (
        MailSecurityFeedbackRow,
        MailSecurityHeldPromotionRow,
    )
    from backend.persistence.models.personal import FollowupRow

    from sqlalchemy import event

    h = await H.make_harness(tmp_path / "fix.sqlite")
    extra = [MailSecurityFeedbackRow.__table__, MailSecurityHeldPromotionRow.__table__,
             FollowupRow.__table__]
    async with h.engine.begin() as conn:
        await conn.run_sync(lambda sc: extra[0].metadata.create_all(sc, tables=extra))

    # Enforce foreign keys like Postgres does (SQLite is off by default); the
    # first live apply failed on FK flush order, which this would have caught.
    @event.listens_for(h.engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with h.engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


def _owner_item(mid: str) -> MailItem:
    # The pre-fix shape: owner-sent mail, no labels -> ingested as incoming.
    return MailItem(
        message_id=mid, account=H.ACCOUNT, subject="Can you review this?",
        content_bytes=b"Can you review this by Friday?\n", timestamp=H.TS,
        headers={"From": f"Owner <{H.ACCOUNT}>", "To": "Bob <bob@partner.example>",
                 "Message-ID": mid},
    )


async def _all(harness, model) -> list[Any]:
    async with harness.registry.session_factory() as s:
        return list((await s.execute(select(model))).scalars())


async def test_fix_drop_and_clean(harness, monkeypatch) -> None:
    for mid in ("<sent-1@x>", "<draft-1@x>", "<keep-1@x>"):
        await H.ingest(harness, monkeypatch, _owner_item(mid))
    for _ in range(3):
        await H.security_worker(harness).process_one()
    conn = harness.conn.id
    ref = {m: f"content://{_content_key(conn, m)}" for m in ("<sent-1@x>", "<draft-1@x>",
                                                            "<keep-1@x>")}
    cid = {m: _communication_id(conn, m) for m in ref}
    async with harness.registry.session_factory() as s:
        async with s.begin():
            s.add(AttentionItemRow(id="at-1", title="t", source_refs=[cid["<sent-1@x>"]]))
            s.add(AttentionItemRow(id="at-keep", title="k", source_refs=[cid["<keep-1@x>"]]))
    assert len(await _all(harness, MailSecurityJobRow)) == 3

    plans = [
        {"cid": cid["<sent-1@x>"], "ref": ref["<sent-1@x>"], "action": "FIX",
         "headers": {"To": "Bob <bob@partner.example>", "Cc": "c@other.example"}},
        {"cid": cid["<draft-1@x>"], "ref": ref["<draft-1@x>"], "action": "DROP",
         "headers": {}},
        {"cid": cid["<keep-1@x>"], "ref": ref["<keep-1@x>"], "action": "KEEP",
         "headers": {}},
    ]
    async with harness.registry.session_factory() as s:
        async with s.begin():
            n = await _apply(s, plans, harness.scope)
    assert n["fixed_reattributed"] == 1 and n["dropped"] == 1
    assert n["jobs"] == 2 and n["assessments"] == 2 and n["attention"] == 1

    comms = {c.id: c for c in await _all(harness, CommunicationRow)}
    assert cid["<draft-1@x>"] not in comms
    sent = comms[cid["<sent-1@x>"]]
    assert sent.direction == "outgoing"
    handles = [i.handle for i in await _all(harness, PersonIdentityRow)
               if i.person_id == sent.person_id]
    assert handles == ["bob@partner.example"]
    keep = comms[cid["<keep-1@x>"]]
    assert keep.direction == "incoming"
    # The kept message keeps its job/assessment and its Today item.
    jobs = await _all(harness, MailSecurityJobRow)
    assert [j.meta["content_ref"] for j in jobs] == [ref["<keep-1@x>"]]
    assert len(await _all(harness, MailSecurityAssessmentRow)) == 1
    assert [a.id for a in await _all(harness, AttentionItemRow)] == ["at-keep"]

    # Idempotent: a second run with the same plan changes nothing destructive.
    async with harness.registry.session_factory() as s:
        async with s.begin():
            n2 = await _apply(s, plans, harness.scope)
    assert n2["dropped"] == 0 and n2["jobs"] == 0 and n2["attention"] == 0
