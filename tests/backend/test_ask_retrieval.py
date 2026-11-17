"""Ask retrieval: intent, ranking, ids, scrubbing, adapters, exclusions. Fakes only."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.attention.service import AttentionService
from backend.calendar.service import CalendarService
from backend.contacts.service import ContactsService
from backend.context.retrieval import retrieve
from backend.context.retrieval import policy
from backend.context.retrieval.intent import parse_intent
from backend.context.retrieval.ranker import rank
from backend.context.retrieval.sources import ADAPTERS
from backend.context.retrieval.sources.logs import logs as logs_adapter
from backend.context.retrieval.types import Record
from backend.email.service import EmailService
from backend.messages.service import MessagesService
from backend.voicemail.service import VoicemailService
from backend.waiting_on.service import WaitingOnService

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)  # a Wednesday
FAKE_TOKEN = "sk-" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def make_state(NOW: datetime = NOW) -> SimpleNamespace:
    attention = AttentionService()
    state = SimpleNamespace(
        calendar_service=CalendarService(),
        email_service=EmailService(attention),
        messages_service=MessagesService(attention=attention),
        voicemail_service=VoicemailService(attention=attention),
        contacts_service=ContactsService(),
        waiting_on_service=WaitingOnService(attention=attention),
    )
    state.calendar_service.ingest({
        "event_id": "ev-1", "title": "Piano lesson with Marta",
        "start": (NOW + timedelta(days=1)).isoformat(),
        "end": (NOW + timedelta(days=1, hours=1)).isoformat(), "source": "test",
        "description": f"bring music {FAKE_TOKEN}",
    })
    state.email_service.ingest({
        "message_id": "m-1", "from_address": "billing@example.test", "to_address": "me@example.test",
        "subject": "Water invoice for May", "body": "Your water invoice is ready.",
        "timestamp": (NOW - timedelta(days=2)).isoformat(),
    })
    state.messages_service.ingest({
        "message_id": "t-1", "peer_identifier": "+15550100", "peer_name": "Sam",
        "body": "Dinner on Friday at the lake house?", "timestamp": (NOW - timedelta(hours=3)).isoformat(),
    })
    state.voicemail_service.ingest({
        "voicemail_id": "v-1", "caller_phone": "+15550111", "caller_name": "Dentist office",
        "transcript": "Calling to confirm your cleaning appointment.",
        "received_at": (NOW - timedelta(days=1)).isoformat(), "duration_seconds": 20,
    })
    state.contacts_service.ingest({
        "person_id": "p-1", "display_name": "Marta Kowal", "emails": ["marta@example.test"],
    })
    return state


def test_intent_extracts_names_terms_windows_and_lookup_shape() -> None:
    intent = parse_intent("When is my piano lesson with Marta tomorrow?", NOW)
    assert "marta" in intent.names and "piano" in intent.keywords and "lesson" in intent.keywords
    assert intent.lookup_shaped and intent.has_terms
    assert intent.window == (NOW.replace(hour=0) + timedelta(days=1), NOW.replace(hour=0) + timedelta(days=2))
    assert "calendar" not in intent.domain_hints
    assert "calendar" in parse_intent("what meetings do I have next week", NOW).domain_hints
    assert parse_intent('find "water invoice"', NOW).phrases == ("water invoice",)


def test_intent_windows() -> None:
    start, end = parse_intent("anything last week", NOW).window  # type: ignore[misc]
    assert start == datetime(2026, 5, 25, tzinfo=UTC) and end == datetime(2026, 6, 1, tzinfo=UTC)
    start, end = parse_intent("what is due in August", NOW).window  # type: ignore[misc]
    assert (start.month, end.month) == (8, 9)
    assert parse_intent("hello", NOW).window is None


def test_ids_are_stable_and_citation_safe() -> None:
    a = policy.record_id("calendar", "acct:uid-1")
    assert a == policy.record_id("calendar", "acct:uid-1") != policy.record_id("calendar", "acct:uid-2")
    assert re.fullmatch(r"cal-[0-9a-f]{10}", a) and policy.citation_safe(a)
    for domain in policy.DOMAINS:
        assert policy.citation_safe(policy.record_id(domain, "x:y/z"))


def test_scrub_removes_a_planted_token_and_clips() -> None:
    text = policy.scrub(f"hello {FAKE_TOKEN} " + "x" * 600)
    assert FAKE_TOKEN not in text and len(text) <= policy.SNIPPET_CHARS


def test_ranking_prefers_name_and_phrase_matches_deterministically() -> None:
    recs = [
        Record(id="a-1", domain="mail", title="Water invoice", snippet="due soon", ts=NOW),
        Record(id="b-2", domain="calendar", title="Lunch with Laura", snippet="", ts=NOW),
        Record(id="c-3", domain="mail", title="Newsletter", snippet="nothing", ts=NOW),
    ]
    intent = parse_intent("Do I have anything with Laura?", NOW)
    ranked = rank(recs, intent, NOW)
    assert [r.id for r in ranked] == ["b-2"]
    assert rank(recs, intent, NOW) == ranked


async def test_every_adapter_finds_its_seeded_record_with_real_ids() -> None:
    state = make_state()
    cases = {
        "calendar": "piano lesson tomorrow",
        "mail": "water invoice",
        "messages": "dinner lake house",
        "voicemail": "dentist cleaning",
        "contacts": "Marta Kowal email",
    }
    for domain, question in cases.items():
        result = await retrieve(state, question, NOW)
        assert domain in result.searched, domain
        assert any(r.domain == domain and re.fullmatch(rf"{policy.prefix_for(domain)}-[0-9a-f]{{10}}", r.id)
                   for r in result.records), (domain, [r.domain for r in result.records])
        assert not any("evidence-0000" in r.id for r in result.records)


async def test_snippets_are_scrubbed_before_leaving_retrieval() -> None:
    result = await retrieve(make_state(), "piano lesson", NOW)
    blob = " ".join(r.snippet + r.title for r in result.records)
    assert result.records and FAKE_TOKEN not in blob


async def test_missing_thing_returns_no_records_and_lists_searched_domains() -> None:
    result = await retrieve(make_state(), "What did Zebulon say about the glacier?", NOW)
    assert result.records == [] and "calendar" in result.searched and "mail" in result.searched


async def test_a_failing_adapter_is_skipped_never_raised() -> None:
    async def boom(state, intent, limit):
        raise RuntimeError("down")

    result = await retrieve(make_state(), "piano lesson", NOW, adapters={**ADAPTERS, "mail": boom})
    assert "mail" in result.skipped and "mail" not in result.searched
    assert any(r.domain == "calendar" for r in result.records)


async def test_a_slow_adapter_times_out_into_skipped() -> None:
    import asyncio

    async def slow(state, intent, limit):
        await asyncio.sleep(1)
        return []

    result = await retrieve(make_state(), "piano", NOW, adapters={"mail": slow}, timeout=0.05)
    assert result.skipped == ["mail"]


def test_allowlist_and_excluded_list_never_overlap() -> None:
    assert set(ADAPTERS) == set(policy.DOMAINS)
    assert not set(ADAPTERS) & set(policy.EXCLUDED_STORES)
    for needed in ("secrets", "auth_sessions", "mh_forms", "mail_security", "personal_api_tokens"):
        assert needed in policy.EXCLUDED_STORES


# -- optional log source ---------------------------------------------------------------
def _es(handler):
    return httpx.MockTransport(handler)


async def test_logs_off_without_env_and_without_log_words(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NERVE_LOGS_ES_URL", raising=False)
    calls: list[str] = []
    intent = parse_intent("why did the pod crash?", NOW)
    assert await logs_adapter(SimpleNamespace(), intent, 10, es_transport=_es(lambda r: calls.append("x"))) is None
    assert calls == []
    monkeypatch.setenv("NERVE_LOGS_ES_URL", "http://logs.invalid")
    assert await logs_adapter(SimpleNamespace(), parse_intent("piano lesson", NOW), 10) is None


async def test_logs_returns_scrubbed_records_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NERVE_LOGS_ES_URL", "http://logs.invalid")

    def handler(request: httpx.Request) -> httpx.Response:
        hit = {"_source": {"@timestamp": NOW.isoformat(), "log": f"crash loop token {FAKE_TOKEN}",
                           "kubernetes": {"namespace_name": "ns", "pod_name": "web-1"}}}
        return httpx.Response(200, json={"hits": {"hits": [hit], "total": {"value": 1}}})

    rows = await logs_adapter(SimpleNamespace(), parse_intent("why did the web pod crash?", NOW), 10,
                              es_transport=_es(handler))
    assert rows and rows[0].id.startswith("log-") and FAKE_TOKEN not in rows[0].snippet


async def test_unreachable_log_store_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NERVE_LOGS_ES_URL", "http://logs.invalid")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    async def adapter(state, intent, limit):
        return await logs_adapter(state, intent, limit, es_transport=_es(handler))

    result = await retrieve(SimpleNamespace(), "why did the web pod crash?", NOW, adapters={"logs": adapter})
    assert result.skipped == ["logs"] and result.records == []


async def test_cpu_bound_scrubbing_never_blocks_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: mail scrubbing ran on the event loop for seconds, so every
    database-backed adapter awaiting beside it timed out at the same instant."""
    import asyncio
    import threading
    import time
    from backend.context.retrieval.sources import _util
    real_make = _util.make
    threads: list[str] = []

    def slow_make(*args, **kwargs):
        threads.append(threading.current_thread().name)
        time.sleep(0.02)
        return real_make(*args, **kwargs)
    monkeypatch.setattr(_util, "make", slow_make)
    state = make_state()
    for i in range(40):  # 40 threads x 20 ms of scrubbing = 0.8 s of blocking work
        state.email_service.ingest({
            "message_id": f"bulk-{i}", "from_address": f"s{i}@example.test", "to_address": "me@example.test",
            "subject": f"Bulk {i}", "body": "x", "timestamp": (NOW - timedelta(days=3, minutes=i)).isoformat(),
        })

    gaps: list[float] = []

    async def heartbeat() -> None:  # any other coroutine on the loop, e.g. a database await
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
    ticker = asyncio.create_task(heartbeat())
    result = await retrieve(state, "bulk", NOW, adapters={"mail": ADAPTERS["mail"]}, timeout=10)
    ticker.cancel()
    assert result.skipped == [] and len(result.records) > 5
    assert max(gaps) < 0.25, max(gaps)  # blocking on the loop would show one gap of ~0.8 s
    assert threads and threading.main_thread().name not in threads


def test_url_userinfo_redaction_is_linear_on_long_tokens() -> None:
    import time
    from backend.connectors.redact import redact_message
    blob = ("a" * 20000) + " me@example.test"
    started = time.perf_counter()
    redact_message(blob, limit=30000)
    assert time.perf_counter() - started < 0.5
    assert redact_message("see https://bob:hunter2@host.test/x", limit=100) == "see https://bob@host.test/x"
