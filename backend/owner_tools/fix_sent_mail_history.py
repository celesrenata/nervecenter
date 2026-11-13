"""Owner tool: correct historical sent mail and drafts (NS-ADR-068).

Owner decision (2026-11-13): "all in that order". Before the NS-ADR-066 fix,
the Gmail poll ingested the owner's sent mail and drafts as INCOMING
communications attributed to the owner. For every stored Gmail communication
whose person is one of the owner's own accounts, this asks Gmail for labels and
the To/Cc/From headers ONLY (``format=metadata``) and:

1. **FIX** (``SENT`` without ``INBOX``): direction -> ``outgoing``, person ->
   the first To/Cc recipient (deterministic person id, the same rule the live
   worker uses; created with its identity and conversation if absent).
2. **DROP** (``DRAFT``, or the message no longer exists in Gmail): delete the
   communication row. The encrypted body object is kept.
3. **CLEAN** both sets: delete their NerveShield jobs, assessments, evidence,
   findings, feedback, gate holds and ledger rows, and the attention (Today)
   items and follow-ups whose refs name them.

Self-addressed copies (``SENT`` + ``INBOX``) and everything else are kept.
Default is a dry run that prints counts only; ``--apply`` writes, in ONE
transaction. Take a database snapshot first. Run in the api pod right after a
successful poll (it reuses the stored access token)::

    python -m backend.owner_tools.fix_sent_mail_history            # dry run
    python -m backend.owner_tools.fix_sent_mail_history --apply
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Mapping
from typing import Any

import httpx

_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_HEADERS = ("From", "To", "Cc")


def _names(ref: Any, ids: set[str]) -> bool:
    """Same matching rule as the review-queue loader (string or mapping refs)."""
    if isinstance(ref, str):
        return ref in ids or any(ref.endswith(f":{i}") for i in ids)
    if isinstance(ref, Mapping):
        return any(isinstance(v, str) and v in ids for v in ref.values())
    return False


async def _classify(reg: Any, store: Any, conn: Any, owner: set[str]) -> list[dict[str, Any]]:
    from sqlalchemy import func, select

    from backend.persistence.models.personal import CommunicationRow, PersonIdentityRow

    token = await reg.connections.get_secret(
        kind="access_token", owner_kind="connection", owner_id=conn.id
    )
    auth = {"Authorization": f"Bearer {token}"}
    async with reg.session_factory() as s:
        rows = (
            await s.execute(
                select(CommunicationRow.id, CommunicationRow.content_ref)
                .join(PersonIdentityRow, PersonIdentityRow.person_id == CommunicationRow.person_id)
                .where(
                    CommunicationRow.content_ref.startswith(
                        f"content://mail/{conn.id}/", autoescape=True
                    ),
                    func.lower(PersonIdentityRow.handle).in_(sorted(owner)),
                )
                .distinct()
            )
        ).all()
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=15) as client:
        for cid, ref in rows:
            meta = await store.head(ref.removeprefix("content://"))
            mid = str(((meta and meta.metadata) or {}).get("message_id", "")).removeprefix(
                "gmail:"
            )
            plan: dict[str, Any] = {"cid": cid, "ref": ref, "action": "KEEP", "headers": {}}
            if not mid:
                plan["action"] = "KEEP_NO_ID"
                out.append(plan)
                continue
            r = None
            for attempt in range(6):
                r = await client.get(
                    f"{_API}/messages/{mid}",
                    headers=auth,
                    params=[("format", "metadata"), *(("metadataHeaders", h) for h in _HEADERS)],
                )
                if r.status_code == 429 or r.status_code >= 500 or (
                    r.status_code == 403 and "rateLimit" in r.text
                ):
                    await asyncio.sleep(min(30.0, 2.0**attempt))
                    continue
                break
            if r.status_code == 401:
                raise SystemExit("access token expired: run after a successful poll")
            if r.status_code == 404:
                plan["action"] = "DROP"
            elif r.status_code != 200:
                plan["action"] = f"KEEP_HTTP_{r.status_code}"
            else:
                body = r.json()
                labels = set(body.get("labelIds") or ())
                plan["headers"] = {
                    str(h.get("name")): str(h.get("value", ""))
                    for h in (body.get("payload") or {}).get("headers") or []
                }
                if "DRAFT" in labels:
                    plan["action"] = "DROP"
                elif "SENT" in labels and "INBOX" not in labels:
                    plan["action"] = "FIX"
            out.append(plan)
    return out


async def _apply(s: Any, plans: list[dict[str, Any]], scope: str) -> Counter:
    from sqlalchemy import delete, select, update

    from backend.communications.registry import normalize_identity_value
    from backend.ingest.mail_worker import _parse_recipients, _person_id_for_handle
    from backend.persistence.models.mail_security import (
        MailEvidenceRow,
        MailSecurityAssessmentRow,
        MailSecurityFeedbackRow,
        MailSecurityFindingRow,
        MailSecurityHeldPromotionRow,
        MailSecurityJobRow,
        MailThreadLedgerRow,
    )
    from backend.persistence.models.personal import (
        AttentionItemRow,
        CommunicationRow,
        ConversationRow,
        FollowupRow,
        PersonIdentityRow,
        PersonRow,
    )

    n: Counter = Counter()
    fix = [p for p in plans if p["action"] == "FIX"]
    drop = [p for p in plans if p["action"] == "DROP"]
    affected = fix + drop
    if not affected:
        return n
    refs = {p["ref"] for p in affected}
    cids = {p["cid"] for p in affected}

    # 1. FIX: outgoing, attributed to the first recipient.
    for p in fix:
        recipients = _parse_recipients(p["headers"])
        values: dict[str, Any] = {"direction": "outgoing"}
        if recipients:
            address, display = recipients[0]
            canonical = normalize_identity_value("email", address)
            pid = _person_id_for_handle("email", canonical)
            # Flush in FK order (no ORM relationships define one for us).
            if await s.get(PersonRow, pid) is None:
                s.add(PersonRow(id=pid, display_name=display or address))
                await s.flush()
                n["people_created"] += 1
            has_ident = (
                await s.execute(
                    select(PersonIdentityRow.id).where(
                        PersonIdentityRow.person_id == pid,
                        PersonIdentityRow.channel == "email",
                    )
                )
            ).first()
            if has_ident is None:
                s.add(PersonIdentityRow(person_id=pid, channel="email", handle=address,
                                        resolved=True))
                await s.flush()
            has_conv = (
                await s.execute(
                    select(ConversationRow.id).where(
                        ConversationRow.person_id == pid, ConversationRow.channel == "email"
                    )
                )
            ).first()
            if has_conv is None:
                s.add(ConversationRow(person_id=pid, channel="email"))
            await s.flush()
            values["person_id"] = pid
            n["fixed_reattributed"] += 1
        else:
            n["fixed_no_recipient"] += 1
        await s.execute(update(CommunicationRow).where(CommunicationRow.id == p["cid"])
                        .values(**values))

    # 2. DROP drafts / vanished messages.
    if drop:
        res = await s.execute(
            delete(CommunicationRow).where(CommunicationRow.id.in_([p["cid"] for p in drop]))
        )
        n["dropped"] += res.rowcount or 0

    # 3. CLEAN derived NerveShield + attention rows for both sets.
    source_keys = [
        sk
        for sk, meta in (
            await s.execute(
                select(MailSecurityJobRow.source_key, MailSecurityJobRow.meta).where(
                    MailSecurityJobRow.account_scope == scope
                )
            )
        ).all()
        if isinstance(meta, Mapping) and meta.get("content_ref") in refs
    ]
    if source_keys:
        aids = [
            r[0]
            for r in (
                await s.execute(
                    select(MailSecurityAssessmentRow.assessment_id).where(
                        MailSecurityAssessmentRow.account_scope == scope,
                        MailSecurityAssessmentRow.source_id.in_(source_keys),
                    )
                )
            ).all()
        ]
        for label, stmt in (
            ("feedback", delete(MailSecurityFeedbackRow).where(
                MailSecurityFeedbackRow.account_scope == scope,
                MailSecurityFeedbackRow.assessment_id.in_(aids))),
            ("findings", delete(MailSecurityFindingRow).where(
                MailSecurityFindingRow.account_scope == scope,
                MailSecurityFindingRow.assessment_id.in_(aids))),
            ("assessments", delete(MailSecurityAssessmentRow).where(
                MailSecurityAssessmentRow.account_scope == scope,
                MailSecurityAssessmentRow.source_id.in_(source_keys))),
            ("evidence", delete(MailEvidenceRow).where(
                MailEvidenceRow.account_scope == scope,
                MailEvidenceRow.source_id.in_(source_keys))),
            ("jobs", delete(MailSecurityJobRow).where(
                MailSecurityJobRow.account_scope == scope,
                MailSecurityJobRow.source_key.in_(source_keys))),
        ):
            n[label] += (await s.execute(stmt)).rowcount or 0
    n["holds"] += (
        await s.execute(
            delete(MailSecurityHeldPromotionRow).where(
                MailSecurityHeldPromotionRow.account_scope == scope,
                MailSecurityHeldPromotionRow.content_ref.in_(sorted(refs)),
            )
        )
    ).rowcount or 0
    n["ledger"] += (
        await s.execute(
            delete(MailThreadLedgerRow).where(
                MailThreadLedgerRow.account_scope == scope,
                MailThreadLedgerRow.content_ref.in_(sorted(refs)),
            )
        )
    ).rowcount or 0
    for label, model, col in (
        ("attention", AttentionItemRow, AttentionItemRow.source_refs),
        ("followups", FollowupRow, FollowupRow.linked_refs),
    ):
        ids = [
            rid
            for rid, rr in (await s.execute(select(model.id, col))).all()
            if any(_names(r, cids) for r in rr or [])
        ]
        if ids:
            n[label] += (await s.execute(delete(model).where(model.id.in_(ids)))).rowcount or 0
    return n


async def main(apply: bool) -> None:
    import backend.api.app  # noqa: F401 - import-order guard
    from backend.api.wiring import _load_secret_codec
    from backend.persistence.connection import create_engine, make_session_factory
    from backend.persistence.repositories import RepositoryRegistry
    from backend.storage.config import create_object_store_from_env

    engine = create_engine()
    try:
        reg = RepositoryRegistry(make_session_factory(engine), secret_codec=_load_secret_codec())
        store = create_object_store_from_env()
        conns = [c for c in await reg.connections.list_connections() if c.kind == "gmail"]
        owner = {
            str((c.config or {}).get("account") or "").strip().lower() for c in conns
        } - {""}
        plans_by_conn = {c.id: await _classify(reg, store, c, owner) for c in conns}
        for cid, plans in plans_by_conn.items():
            print("plan", cid[:8], dict(Counter(p["action"] for p in plans)))
        if not apply:
            print("dry run: nothing written")
            return
        async with reg.session_factory() as s:
            async with s.begin():
                for cid, plans in plans_by_conn.items():
                    print("applied", cid[:8], dict(await _apply(s, plans, f"conn:{cid}")))
        print("committed")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
