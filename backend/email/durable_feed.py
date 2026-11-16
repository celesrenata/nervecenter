"""Feed the in-memory Email screen from durable mail (owner: "email - yes").

The Email screen reads the in-process :class:`EmailService`, which nothing
filled: every polled email is stored durably (a ``communications`` row plus
the raw MIME in the object store) but never reached that service. This feed
replays the most recent stored emails into it, at startup and again on read
(throttled), so new mail appears without a restart.

Security comes first (NerveShield owns the verdict):

- incoming mail with a ``LOW_PRIORITY`` verdict shows as plain text (HTML
  removed, length capped); nothing is rendered as HTML;
- incoming mail with any other verdict (REVIEW, INCOMPLETE, ...) shows the
  sender and subject only, with a "held" notice instead of the body;
- incoming mail not assessed yet is skipped and picked up on a later refresh;
- the owner's own sent mail shows as plain text.

Replay never creates attention items: the security worker already decides
what reaches Now. Never raises; returns counts.
"""
from __future__ import annotations

import email.utils
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: How many recent stored emails the screen holds.
DEFAULT_LIMIT = 300
#: Minimum seconds between refreshes triggered by reads.
DEFAULT_MIN_INTERVAL = 30.0
#: Body characters kept per email.
BODY_CAP = 20_000
SHOWN_DISPOSITIONS = frozenset({"LOW_PRIORITY"})
_MAIL_SCHEME = "content://"


def _header(evidence: Any, name: str) -> str | None:
    for n, v in getattr(evidence, "headers", []) or []:
        if n.strip().lower() == name:
            return v
    return None


def _scope_of(content_ref: str) -> str | None:
    """``content://mail/<connection>/<digest>`` -> ``conn:<connection>``."""
    parts = content_ref.removeprefix(_MAIL_SCHEME).split("/")
    if len(parts) >= 3 and parts[0] == "mail" and parts[1]:
        return f"conn:{parts[1]}"
    return None


def _body_text(evidence: Any) -> str:
    from backend.bills.signal import plain_text

    parts = list(getattr(evidence, "text_parts", []) or [])
    plain = [p.normalized for p in parts if p.subtype == "plain" and p.normalized.strip()]
    if plain:
        text = "\n\n".join(plain).strip()
    else:
        text = plain_text("\n".join(p.normalized for p in parts))
    return text[:BODY_CAP]


class DurableEmailFeed:
    """Replays stored emails into an :class:`EmailService` (idempotent)."""

    def __init__(
        self,
        registry: Any,
        object_store: Any,
        service: Any,
        *,
        limit: int = DEFAULT_LIMIT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        clock: Any = time.monotonic,
    ) -> None:
        self._registry = registry
        self._store = object_store
        self._service = service
        self._limit = limit
        self._min_interval = min_interval
        self._clock = clock
        self._fed: set[str] = set()
        self._last: float | None = None

    async def refresh(self, *, force: bool = False) -> dict[str, int]:
        out = {"fed": 0, "held": 0, "pending": 0, "no_content": 0, "failed": 0}
        if self._registry is None or self._store is None or self._service is None:
            return out
        now = self._clock()
        if not force and self._last is not None and now - self._last < self._min_interval:
            return out
        self._last = now
        try:
            rows, verdicts, handles, owners = await self._load()
        except Exception as exc:  # noqa: BLE001 - the screen must not fail on this
            log.warning("email.feed_load_failed", error_type=type(exc).__name__)
            out["failed"] += 1
            return out
        for row in reversed(rows):  # oldest first, as the mail arrived
            if row.id in self._fed:
                continue
            try:
                status = await self._feed_one(row, verdicts, handles, owners)
            except Exception as exc:  # noqa: BLE001 - one bad email must not stop the rest
                log.warning("email.feed_failed", error_type=type(exc).__name__)
                status = "failed"
            out[status] += 1
        return out

    async def _load(self) -> tuple[list[Any], dict[str, str], dict[str, str], dict[str, str]]:
        from sqlalchemy import select

        from backend.persistence.models.mail_security import (
            MailSecurityAssessmentRow,
            MailSecurityJobRow,
        )
        from backend.persistence.models.personal import (
            CommunicationRow,
            ConnectionRow,
            PersonIdentityRow,
        )

        async with self._registry.session_factory() as session:
            rows = (
                await session.execute(
                    select(CommunicationRow)
                    .where(CommunicationRow.channel == "email")
                    .order_by(CommunicationRow.received_at.desc())
                    .limit(self._limit)
                )
            ).scalars().all()
            rows = [r for r in rows if r.id not in self._fed]
            refs = {r.content_ref for r in rows if r.content_ref}
            verdicts: dict[str, str] = {}
            if refs:
                jobs = (
                    await session.execute(
                        select(MailSecurityJobRow).where(
                            MailSecurityJobRow.meta["content_ref"].as_string().in_(refs)
                        )
                    )
                ).scalars().all()
                keys = {(j.account_scope, j.source_key): j.meta["content_ref"] for j in jobs}
                if keys:
                    latest: dict[tuple[str, str], Any] = {}
                    for a in (
                        await session.execute(
                            select(MailSecurityAssessmentRow).where(
                                MailSecurityAssessmentRow.source_id.in_({k[1] for k in keys})
                            )
                        )
                    ).scalars():
                        key = (a.account_scope, a.source_id)
                        if key in keys and (key not in latest or a.revision > latest[key].revision):
                            latest[key] = a
                    for key, asmt in latest.items():
                        verdicts[keys[key]] = str(asmt.disposition or "")
            handles: dict[str, str] = {}
            person_ids = {r.person_id for r in rows if r.person_id}
            if person_ids:
                for ident in (
                    await session.execute(
                        select(PersonIdentityRow).where(PersonIdentityRow.person_id.in_(person_ids))
                    )
                ).scalars():
                    handles.setdefault(ident.person_id, ident.handle)
            owners: dict[str, str] = {}
            for conn in (
                await session.execute(select(ConnectionRow).where(ConnectionRow.kind == "gmail"))
            ).scalars():
                account = (conn.config or {}).get("account")
                if account:
                    owners[f"conn:{conn.id}"] = str(account)
        return list(rows), verdicts, handles, owners

    async def _feed_one(
        self,
        row: Any,
        verdicts: dict[str, str],
        handles: dict[str, str],
        owners: dict[str, str],
    ) -> str:
        from backend.mail_security.parser import parse_mime_evidence

        incoming = row.direction != "outgoing"
        ref = row.content_ref or ""
        verdict = verdicts.get(ref)
        if incoming and not verdict:
            return "pending"  # not assessed yet: retry on a later refresh
        held = incoming and verdict not in SHOWN_DISPOSITIONS
        evidence = None
        if ref.startswith(_MAIL_SCHEME):
            raw = await self._store.get(ref.removeprefix(_MAIL_SCHEME))
            if raw:
                evidence = parse_mime_evidence(raw)
        if evidence is None:
            self._fed.add(row.id)
            return "no_content"
        owner = owners.get(_scope_of(ref) or "", "me")
        counterpart = handles.get(row.person_id or "", "unknown")
        name, from_addr = email.utils.parseaddr(_header(evidence, "from") or "")
        _, to_addr = email.utils.parseaddr(_header(evidence, "to") or "")
        if incoming:
            sender, recipient = from_addr or counterpart, to_addr or owner
        else:
            sender, recipient = from_addr or owner, to_addr or counterpart
        if held:
            body = f"Held by NerveShield ({verdict}). Open Security Review to inspect it."
        else:
            body = _body_text(evidence)
        self._service.ingest(
            {
                "message_id": f"durable:{row.id}",
                "direction": "RECEIVED" if incoming else "SENT",
                "from_address": sender,
                "to_address": recipient,
                "sender_name": (name or None) if incoming else None,
                "subject": row.subject or _header(evidence, "subject") or "",
                "body": body,
                "timestamp": row.received_at,
                "source": "mail",
            },
            generate_attention=False,
        )
        self._fed.add(row.id)
        return "held" if held else "fed"


__all__ = ["DurableEmailFeed"]
