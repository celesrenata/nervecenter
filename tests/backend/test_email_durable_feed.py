"""Email screen fed from durable mail (backend.email.durable_feed).

Real ORM rows on SQLite, a fake object store holding MIME bytes, and the real
EmailService. Checks the security rules: LOW_PRIORITY shows text, any other
verdict shows a held notice and no body, unassessed mail waits, sent mail
shows, nothing creates attention, and refresh is idempotent and throttled.
"""
from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import backend.api.app  # noqa: F401 - import-order guard
from backend.attention.service import AttentionService
from backend.email.durable_feed import DurableEmailFeed
from backend.email.service import EmailService
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailSecurityAssessmentRow,
    MailSecurityJobRow,
)
from backend.persistence.models.personal import (
    CommunicationRow,
    ConnectionRow,
    PersonIdentityRow,
    PersonRow,
)
from backend.persistence.repositories import RepositoryRegistry

CONN = "11111111-2222-3333-4444-555555555555"
SCOPE = f"conn:{CONN}"
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
TABLES = [
    CommunicationRow.__table__, ConnectionRow.__table__, PersonIdentityRow.__table__,
    PersonRow.__table__, MailSecurityJobRow.__table__, MailSecurityAssessmentRow.__table__,
]


def _mime(sender: str, subject: str, body: str, *, html: bool = False) -> bytes:
    ctype = "text/html" if html else "text/plain"
    return (
        f"From: {sender}\r\nTo: owner@example.test\r\nSubject: {subject}\r\n"
        f"MIME-Version: 1.0\r\nContent-Type: {ctype}; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.gets = 0

    async def get(self, key: str) -> bytes:
        self.gets += 1
        return self.objects[key]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def world(tmp_path: pathlib.Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail.db'}",
                                 poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(PersonRow.__table__.metadata.create_all, tables=TABLES)
    registry = RepositoryRegistry(make_session_factory(engine))
    store = FakeStore()
    async with registry.session_factory() as s:
        s.add(ConnectionRow(id=CONN, kind="gmail", config={"account": "owner@example.test"}))
        await s.commit()
    yield registry, store
    await engine.dispose()


async def _add_mail(registry: Any, store: FakeStore, n: int, *, direction: str = "incoming",
                    verdict: str | None = "LOW_PRIORITY", mime: bytes | None = None) -> None:
    ref = f"content://mail/{CONN}/digest{n}"
    store.objects[ref.removeprefix("content://")] = mime or _mime(
        f"Alice <alice{n}@example.com>", f"Subject {n}", f"Body text {n}")
    async with registry.session_factory() as s:
        s.add(PersonRow(id=f"p{n}", display_name=f"alice{n}"))
        s.add(PersonIdentityRow(person_id=f"p{n}", channel="email",
                                handle=f"alice{n}@example.com"))
        s.add(CommunicationRow(id=f"c{n}", person_id=f"p{n}", channel="email",
                               direction=direction, subject=f"Subject {n}", content_ref=ref,
                               received_at=T0 + timedelta(minutes=n)))
        if verdict is not None:
            s.add(MailSecurityJobRow(account_scope=SCOPE, outbox_job_key=f"j{n}",
                                     source_key=f"src{n}", analysis_profile_version="v",
                                     meta={"content_ref": ref}))
            s.add(MailSecurityAssessmentRow(
                assessment_id=f"a{n}", account_scope=SCOPE, source_id=f"src{n}",
                analysis_profile_version="v", policy_version="p", processing_state="DONE",
                evidence_fidelity="FULL", disposition=verdict, revision=1))
        await s.commit()


def _feed(registry: Any, store: FakeStore, clock: Clock | None = None):
    attention = AttentionService()
    service = EmailService(attention=attention)
    feed = DurableEmailFeed(registry, store, service, clock=clock or Clock())
    return feed, service, attention


def _bodies(service: EmailService) -> dict[str, str]:
    return {m.subject: m.body for t in service.list_threads()
            for m in service.get_messages(t.thread_id)}


async def test_security_verdict_decides_what_the_screen_shows(world) -> None:
    registry, store = world
    await _add_mail(registry, store, 1, verdict="LOW_PRIORITY")
    await _add_mail(registry, store, 2, verdict="REVIEW")
    await _add_mail(registry, store, 3, verdict=None)  # not assessed yet
    await _add_mail(registry, store, 4, direction="outgoing", verdict=None)
    feed, service, attention = _feed(registry, store)

    counts = await feed.refresh(force=True)

    assert counts == {"fed": 2, "held": 1, "pending": 1, "no_content": 0, "failed": 0}
    bodies = _bodies(service)
    assert bodies["Subject 1"] == "Body text 1"
    assert "Held by NerveShield (REVIEW)" in bodies["Subject 2"]
    assert "Body text 2" not in bodies["Subject 2"]
    assert "Subject 3" not in bodies
    assert bodies["Subject 4"] == "Body text 4"
    thread1 = next(t for t in service.list_threads() if t.subject == "Subject 1")
    assert thread1.from_address == "alice1@example.com"
    assert thread1.sender_name == "Alice"
    assert attention.list() == []  # replay never raises attention


async def test_refresh_is_idempotent_throttled_and_picks_up_new_verdicts(world) -> None:
    registry, store = world
    await _add_mail(registry, store, 1)
    await _add_mail(registry, store, 2, verdict=None)
    clock = Clock()
    feed, service, _ = _feed(registry, store, clock)
    await feed.refresh(force=True)
    gets = store.gets

    # Within the throttle window: nothing happens.
    assert await feed.refresh() == {"fed": 0, "held": 0, "pending": 0,
                                    "no_content": 0, "failed": 0}
    # Mail 2 gets its verdict; after the window it appears, mail 1 is not refetched.
    async with registry.session_factory() as s:
        ref = f"content://mail/{CONN}/digest2"
        s.add(MailSecurityJobRow(account_scope=SCOPE, outbox_job_key="j2", source_key="src2",
                                 analysis_profile_version="v", meta={"content_ref": ref}))
        s.add(MailSecurityAssessmentRow(
            assessment_id="a2", account_scope=SCOPE, source_id="src2",
            analysis_profile_version="v", policy_version="p", processing_state="DONE",
            evidence_fidelity="FULL", disposition="LOW_PRIORITY", revision=1))
        await s.commit()
    clock.t = 31.0
    counts = await feed.refresh()
    assert counts["fed"] == 1
    assert store.gets == gets + 1
    assert sum(t.message_count for t in service.list_threads()) == 2


async def test_html_only_mail_shows_as_plain_text(world) -> None:
    registry, store = world
    await _add_mail(registry, store, 1, mime=_mime(
        "bank@example.com", "Statement", "<p>Your <b>statement</b> is ready</p>", html=True))
    feed, service, _ = _feed(registry, store)
    await feed.refresh(force=True)
    body = _bodies(service)["Subject 1"]
    assert "<" not in body and "statement is ready" in body
async def test_html_mail_exposes_sanitisable_html_alternative(world) -> None:
    registry, store = world
    await _add_mail(registry, store, 1, mime=_mime(
        "bank@example.com", "Statement", "<p>Your <b>statement</b> is ready</p>", html=True))
    await _add_mail(registry, store, 2, mime=_mime(
        "shop@example.com", "Promo", "<html><body><p>Sale <b>now</b></p></body></html>"))
    feed, service, _ = _feed(registry, store)
    await feed.refresh(force=True)
    msgs = {m.subject: m for t in service.list_threads() for m in service.get_messages(t.thread_id)}
    assert "<b>statement</b>" in (msgs["Subject 1"].body_html or "")
    # HTML mislabelled as text/plain is still treated as HTML, never shown as markup.
    assert "<" not in msgs["Subject 2"].body and msgs["Subject 2"].body_html
