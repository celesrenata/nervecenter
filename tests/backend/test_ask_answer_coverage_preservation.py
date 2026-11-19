"""Preservation: Ask behaviour that must not change when answer coverage is fixed.

Properties 3 and 4 of spec ask-nervcenter-answer-coverage. Everything here was
observed on the UNFIXED code and recorded as golden results, so these tests PASS
before the fix and must keep passing after it.

Property 3: plain keyword/name/window lookups, the "I found nothing about ..."
answer, calendar create/delete proposals, saved sessions and the response shape
are unchanged.
Property 4: ``policy_denied`` on non-local transports, scrubbing and clipping,
excluded stores without adapters, adapter failure isolation, citation ids.

Fakes only: seeded services, scripted models, no network.
"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.api.endpoints import ask_sessions as sessions_ep
from backend.api.endpoints import context as context_endpoints
from backend.context.ask import AskRequest, AskService
from backend.context.retrieval import policy, retrieve as real_retrieve
from backend.context.retrieval.intent import STOPWORDS, Intent, _stem, parse_intent
from backend.context.retrieval.ranker import rank, score_record
from backend.context.retrieval.sources import ADAPTERS, _util
from backend.context.retrieval.types import Record

# Fixtures and constants reused from the existing Ask suites (same fakes, no copies).
from test_ask_calendar_create import SENTENCE as CREATE_SENTENCE
from test_ask_calendar_create import world as create_world  # noqa: F401  (pytest fixture)
from test_ask_delete_flow import SENTENCE as DELETE_SENTENCE
from test_ask_delete_flow import world as delete_world  # noqa: F401  (pytest fixture)
from test_ask_retrieval import FAKE_TOKEN, NOW, make_state
from test_ask_sessions_api import _client as sessions_client
from test_ask_sessions_api import _doc as sessions_doc

PROFILE = settings(max_examples=25, deadline=None,
                   suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])

# -- observed on the unfixed code (NOW = Wed 2026-06-03 12:00 UTC, seeded() below) ---------
GOLDEN_IDS = {
    "find texts from Laura": ["msg-3b218080b3", "msg-56bb5e7592"],
    "what did the school send about Friday": ["mail-c0171412ce"],
    "what is on my calendar tomorrow": ["cal-4afc5ed875"],
    "what is on my calendar next week": ["cal-473b3f7fa9"],
}
GOLDEN_SEARCHED = ["calendar", "mail", "messages", "voicemail", "contacts", "waiting_on"]
ANSWERED_KEYS = {
    "accepted": bool, "executed": bool, "service": str, "request_id": str, "timestamp": str,
    "status": str, "request_status": str, "answer": str, "grounded": bool, "citations": list,
    "records": list, "ungrounded_claims": list, "model": str, "provider": str, "inference": dict,
    "route": dict, "usage": dict, "policy": dict, "provenance": dict,
}
NOT_FOUND_KEYS = {k: v for k, v in ANSWERED_KEYS.items()
                  if k not in ("model", "provider", "route", "usage")}


def seeded() -> SimpleNamespace:
    state = make_state(NOW)
    texts = state.messages_service
    texts.ingest({"message_id": "t-laura-1", "peer_identifier": "+15550200", "peer_name": "Laura",
                  "body": "Running late, see you at six", "timestamp": (NOW - timedelta(hours=5)).isoformat()})
    texts.ingest({"message_id": "t-laura-2", "peer_identifier": "+15550200", "peer_name": "Laura",
                  "body": "Can you grab milk?", "timestamp": (NOW - timedelta(days=1)).isoformat()})
    state.email_service.ingest({
        "message_id": "m-school", "from_address": "office@school.example", "to_address": "me@example.test",
        "subject": "Friday field trip permission slip",
        "body": "The school needs the signed slip by Friday.", "timestamp": (NOW - timedelta(days=1)).isoformat(),
    })
    state.calendar_service.ingest({
        "event_id": "ev-2", "title": "Dentist checkup", "start": (NOW + timedelta(days=7)).isoformat(),
        "end": (NOW + timedelta(days=7, hours=1)).isoformat(), "source": "test",
    })
    return state


class SecureModel:
    """Secure-transport fake (has ``check``). Echoes each evidence line with its
    citation, counts calls, and can be told to add an invented citation id."""

    fixture_mode = True
    base_url = "http://omniroute.invalid"

    def __init__(self, extra_cite: str | None = None) -> None:
        self.calls = 0
        self.extra_cite = extra_cite

    def check(self, model: str) -> None:
        return None

    def generate(self, model, prompt, parameters=None, timeout=None, **kw):
        self.calls += 1
        lines = re.findall(r"^REF (\S+): (.*)$", prompt, re.MULTILINE)
        text = "\n".join(f"{summary} [{rid}]" for rid, summary in lines) or "No evidence."
        if self.extra_cite:
            text += f" [{self.extra_cite}]"
        return {"content": text, "input_tokens": 1, "output_tokens": 1}


class PlainTransport:
    """A transport that is not the secure local route: no ``check``."""

    fixture_mode = True
    base_url = "http://omniroute.invalid"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, model, prompt, parameters=None, timeout=None, **kw):
        self.calls += 1
        return {"content": "remote answer", "input_tokens": 1, "output_tokens": 1}


def ask(service: AskService, question: str, retrieval: Any, **kw: Any):
    return service.ask(AskRequest(question=question, **kw), retrieval=retrieval)


def check_shape(body: dict[str, Any], expected: dict[str, type]) -> None:
    for key, kind in expected.items():
        assert key in body, key
        assert isinstance(body[key], kind), (key, type(body[key]))


# -- golden lookups: same ordered ids and cited answer ------------------------------------------
async def test_keyword_name_and_window_lookups_return_the_recorded_ordered_ids() -> None:
    state = seeded()
    for question, ids in GOLDEN_IDS.items():
        result = await real_retrieve(state, question, NOW)
        assert [r.id for r in result.records] == ids, question
        assert result.searched == GOLDEN_SEARCHED and result.skipped == []


async def test_lookups_give_the_same_cited_answer() -> None:
    state = seeded()
    for question in ("find texts from Laura", "what did the school send about Friday"):
        model = SecureModel()
        body = ask(AskService(transport=model, remote_provider_allowed=True), question,
                   await real_retrieve(state, question, NOW)).body
        ids = GOLDEN_IDS[question]
        assert body["status"] == "answered" and body["grounded"] is True
        assert [c["record_id"] for c in body["citations"]] == ids
        assert [r["id"] for r in body["records"]] == ids
        assert body["ungrounded_claims"] == [] and body["provenance"]["context_records"] == sorted(ids)
        assert all(f"[{rid}]" in body["answer"] for rid in ids)
        assert model.calls == 1


# -- "I found nothing about ..." in code, no model call --------------------------------------------
async def test_lookup_with_terms_and_no_match_is_answered_in_code() -> None:
    question = "What did Zebulon say about the glacier?"
    result = await real_retrieve(seeded(), question, NOW)
    assert result.records == [] and result.searched == GOLDEN_SEARCHED
    model = SecureModel()
    outcome = ask(AskService(transport=model, remote_provider_allowed=True), question, result)
    body = outcome.body
    assert outcome.status_code == 200 and model.calls == 0
    assert body["answer"] == ("I found nothing about zebulon, glacier in "
                              "calendar, mail, messages, voicemail, contacts, waiting on.")
    assert body["grounded"] is False and body["citations"] == [] and body["records"] == []
    assert body["provenance"]["searched"] == GOLDEN_SEARCHED and body["provenance"]["context_records"] == []
    check_shape(body, NOT_FOUND_KEYS)


# -- policy_denied for a non-local transport, no retrieval ------------------------------------------
async def test_non_local_transport_is_policy_denied_and_sees_no_personal_records() -> None:
    question = "find texts from Laura"
    result = await real_retrieve(seeded(), question, NOW)
    assert result.records
    transport = PlainTransport()
    outcome = ask(AskService(transport=transport, remote_provider_allowed=False), question, result)
    body = outcome.body
    assert outcome.status_code == 200 and body["status"] == "policy_denied"
    assert body["request_status"] == "denied" and body["executed"] is False
    assert body["policy"]["denied"] is True and body["policy"]["reason_code"] == "infer_route_denied"
    assert "No answer is fabricated" in body["answer"] and body["citations"] == []
    assert body["provenance"]["context_records"] == [] and transport.calls == 0


async def test_non_local_transport_never_receives_retrieved_records() -> None:
    question = "find texts from Laura"
    result = await real_retrieve(seeded(), question, NOW)
    transport = PlainTransport()
    body = ask(AskService(transport=transport, remote_provider_allowed=True), question, result).body
    assert body["status"] == "answered" and body["records"] == []
    assert body["provenance"]["context_records"] == []


async def test_confidential_is_denied_even_on_the_secure_route() -> None:
    model = SecureModel()
    body = ask(AskService(transport=model, remote_provider_allowed=True), "hello world", None,
               context={"sensitivity": "confidential"}).body
    assert body["status"] == "policy_denied" and body["policy"]["denied"] is True and model.calls == 0


# -- excluded stores have no adapter -------------------------------------------------------------
def test_excluded_stores_have_no_adapter_and_every_adapter_is_allowlisted() -> None:
    assert not set(ADAPTERS) & set(policy.EXCLUDED_STORES)
    assert set(ADAPTERS) <= set(policy.DOMAINS)
    for store in ("secrets", "auth_sessions", "mh_forms", "mail_security", "mail_evidence",
                  "mail_intel", "finance_ledger", "personal_api_tokens"):
        assert store in policy.EXCLUDED_STORES and store not in ADAPTERS


# -- adapter failure isolation ----------------------------------------------------------------------
async def test_raising_and_slow_adapters_land_in_skipped_and_others_still_answer() -> None:
    async def boom(state, intent, limit):
        raise RuntimeError("down")

    async def slow(state, intent, limit):
        await asyncio.sleep(1)
        return []

    state = seeded()
    result = await real_retrieve(state, "find texts from Laura", NOW, timeout=0.1,
                                 adapters={**ADAPTERS, "mail": boom, "voicemail": slow})
    assert sorted(result.skipped) == ["mail", "voicemail"]
    assert "mail" not in result.searched and "voicemail" not in result.searched
    assert [r.id for r in result.records] == GOLDEN_IDS["find texts from Laura"]
    body = ask(AskService(transport=SecureModel(), remote_provider_allowed=True),
               "find texts from Laura", result).body
    assert body["status"] == "answered" and body["grounded"] is True
    assert body["answer"].endswith(" I could not search mail, voicemail just now.")


async def test_a_missing_service_is_neither_searched_nor_skipped() -> None:
    result = await real_retrieve(seeded(), "find texts from Laura", NOW)
    for domain in ("tasks", "bills", "goals", "wellness"):
        assert domain not in result.searched and domain not in result.skipped


# -- citation ids -----------------------------------------------------------------------------------
async def test_record_ids_use_prefix_dash_ten_hex_and_unresolved_ids_are_ungrounded() -> None:
    state = seeded()
    seen: list[Record] = []
    for question in (*GOLDEN_IDS, "piano lesson", "dentist cleaning", "Marta Kowal email"):
        seen += (await real_retrieve(state, question, NOW)).records
    assert len(seen) >= 6
    for rec in seen:
        assert re.fullmatch(rf"{policy.prefix_for(rec.domain)}-[0-9a-f]{{10}}", rec.id), rec.id
        assert policy.citation_safe(rec.id)

    question = "find texts from Laura"
    model = SecureModel(extra_cite="msg-0123456789")
    body = ask(AskService(transport=model, remote_provider_allowed=True), question,
               await real_retrieve(state, question, NOW)).body
    assert body["ungrounded_claims"] == ["msg-0123456789"] and body["grounded"] is False
    assert [c["record_id"] for c in body["citations"]] == GOLDEN_IDS[question]
    assert "msg-0123456789" not in {r["id"] for r in body["records"]}


# -- response keys and types through the real endpoint, saved sessions ---------------------------------
def endpoint_client(model: SecureModel) -> TestClient:
    async def clocked_retrieve(state, question, *args, **kwargs):
        kwargs.pop("now", None)
        return await real_retrieve(state, question, NOW, **kwargs)

    app = FastAPI()

    @app.middleware("http")
    async def principal(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.principal = {"sub": "alice"}
        return await call_next(request)

    app.include_router(context_endpoints.router)
    app.include_router(sessions_ep.router)
    for key, value in vars(seeded()).items():
        setattr(app.state, key, value)
    app.state.ask_sessions_repo = sessions_ep._MemoryPrefs()
    app.state.ask_service = AskService(transport=model, remote_provider_allowed=True)
    client = TestClient(app)
    client._patch = patch.object(context_endpoints, "retrieve", clocked_retrieve)  # type: ignore[attr-defined]
    return client


def test_endpoint_response_keeps_old_keys_and_types_and_session_id_is_accepted() -> None:
    client = endpoint_client(SecureModel())
    with client._patch:  # type: ignore[attr-defined]
        response = client.post("/api/context/ask",
                               json={"question": "find texts from Laura", "session_id": "s1"})
        assert response.status_code == 200, response.text
        body = response.json()
        check_shape(body, ANSWERED_KEYS)
        assert body["status"] == "answered" and body["request_status"] == "answered"
        assert [c["record_id"] for c in body["citations"]] == GOLDEN_IDS["find texts from Laura"]
        assert {"record_id", "ref", "kind", "title", "domain", "link", "when"} <= set(body["citations"][0])
        assert {"id", "domain", "title", "when", "snippet", "link"} == set(body["records"][0])
        assert {"context_package_id", "context_records", "policy_decision"} <= set(body["provenance"])
        none = client.post("/api/context/ask", json={"question": "What did Zebulon say about the glacier?"})
        check_shape(none.json(), NOT_FOUND_KEYS)
        assert client.post("/api/context/ask", json={"question": ""}).status_code == 422


def test_saved_sessions_round_trip_is_unchanged() -> None:
    client, _ = sessions_client("alice")
    assert client.get("/api/ask/sessions").json() == {"sessions": [], "active_id": None, "saved_at": None}
    assert client.put("/api/ask/sessions", json=sessions_doc("first", "second")).json()["sessions"] == 2
    got = client.get("/api/ask/sessions").json()
    assert [s["title"] for s in got["sessions"]] == ["first", "second"] and got["active_id"] == "s1"
    assert got["saved_at"] and got["sessions"][0]["messages"][1]["provenance"] == {"facts": [], "hypotheses": []}
    assert sessions_client(None)[0].get("/api/ask/sessions").status_code == 401


# -- calendar create and delete: confirm-first flow (reuses the existing fakes) ----------------------
async def test_calendar_create_is_a_proposal_and_nothing_is_written_until_confirm(create_world) -> None:
    body = create_world.propose()
    assert body["status"] == "proposal" and body["executed"] is False
    assert body["proposal"]["requires_confirmation"] is True
    assert create_world.google.writes() == []
    confirmed = create_world.client.post(f"/api/calendar/proposals/{body['proposal']['id']}/confirm")
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "created"
    assert len(create_world.google.writes()) == 1


async def test_calendar_delete_is_a_proposal_and_nothing_is_deleted_until_confirm(delete_world) -> None:
    body = delete_world.ask(DELETE_SENTENCE)
    assert body["status"] == "delete_proposal" and "Nothing is deleted until you press Confirm" in body["answer"]
    for method in ("DELETE", "PATCH", "POST"):
        assert delete_world.google.writes(method) == []
    assert delete_world.confirm(body["delete_proposal"]["id"]).status_code == 200
    assert delete_world.google.writes("DELETE")


# =====================================================================================================
# Property tests
# =====================================================================================================

# -- frozen copy of the original ranker (observed on the unfixed code) ----------------------------------
def _ref_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _ref_words(text: str) -> set[str]:
    return {_stem(t) for t in re.findall(r"[a-z0-9][a-z0-9_\-']*", text.lower())}


def ref_score(record: Record, intent: Intent, now: datetime) -> float:
    hay = f"{record.title} {record.snippet}".lower()
    words = _ref_words(hay)
    score = 0.0
    hits = 0
    for kw in intent.keywords:
        if kw in words or (len(kw) > 4 and kw in hay):
            score += 1.0
            hits += 1
    for name in intent.names:
        if name in words:
            score += 3.0
            hits += 1
    for phrase in intent.phrases:
        if phrase in hay:
            score += 3.0
            hits += 1
    in_window = False
    ts = _ref_aware(record.ts)
    if intent.window and ts is not None:
        start, end = _ref_aware(intent.window[0]), _ref_aware(intent.window[1])
        in_window = start <= ts < end  # type: ignore[operator]
        if in_window:
            score += 2.0
    if intent.has_terms:
        if hits == 0:
            return 0.0
    elif intent.window is not None:
        if not in_window:
            return 0.0
    elif record.domain in intent.domain_hints:
        score = 0.5
    else:
        return 0.0
    if record.domain in intent.domain_hints:
        score += 1.0
    if ts is not None:
        age_days = abs((_ref_aware(now) - ts).total_seconds()) / 86400  # type: ignore[operator]
        score += max(0.0, 1.0 - min(age_days, 365.0) / 365.0) * 0.5
    return round(score, 4)


def ref_rank(records: list[Record], intent: Intent, now: datetime) -> list[Record]:
    scored = []
    for rec in records:
        s = ref_score(rec, intent, now)
        if s > 0:
            scored.append(rec.with_score(s))
    scored.sort(key=lambda r: (-r.score, r.domain, r.id))
    out: list[Record] = []
    used = 0
    for rec in scored:
        size = len(rec.title) + len(rec.snippet) + 60
        if out and (len(out) >= 12 or used + size > 3600):
            break
        out.append(rec)
        used += size
    return out


KEYWORDS = ["piano", "lesson", "water", "dinner", "lake", "cleaning", "dentist", "school", "garden",
            "passport", "glacier", "budget", "insurance", "lease", "mortgage", "kitchen"]
NAMES = ["Laura", "Marta", "Henderson", "Zebulon"]
assert not {_stem(w) for w in KEYWORDS} & STOPWORDS


def _rec(i: int, domain: str, title: str, snippet: str, days: float | None) -> Record:
    ts = None if days is None else NOW + timedelta(days=days)
    return Record(id=f"{policy.prefix_for(domain)}-{i:010x}", domain=domain, title=title,
                  snippet=snippet, when=None if ts is None else ts.isoformat(), ts=ts)


#: More than MAX_RECORDS records so the cap and the character budget both apply.
RECORDS: list[Record] = [
    _rec(1, "calendar", "Piano lesson with Marta", "bring music kitchen", 1),
    _rec(2, "mail", "Water bill reminder", "garden water budget " + "z" * 250, -2),
    _rec(3, "messages", "Text from Laura", "dinner at the lake house", -0.2),
    _rec(4, "voicemail", "Dentist office", "cleaning appointment passport", -1),
    _rec(5, "contacts", "Marta Kowal", "marta@example.test", None),
    _rec(6, "bills", "Henderson insurance", "mortgage lease due", 12),
    _rec(7, "tasks", "Fix the kitchen", "school garden", 0.5),
    _rec(8, "goals", "Glacier trip", "budget passport", 40),
    _rec(9, "mail", "Zebulon wrote", "lease insurance " + "y" * 250, -9),
] + [_rec(100 + n, "mail", f"Newsletter {n} piano", "lesson water dinner " + "q" * 200, -n) for n in range(14)]


@st.composite
def non_operator_questions(draw: st.DrawFn) -> str:
    words = draw(st.lists(st.sampled_from(KEYWORDS), min_size=0, max_size=4, unique=True))
    names = draw(st.lists(st.sampled_from(NAMES), min_size=0, max_size=2, unique=True))
    if not words and not names:
        words = [draw(st.sampled_from(KEYWORDS))]
    terms = draw(st.permutations(words + names))
    prefix = draw(st.sampled_from(["find", "show me", "what did", "tell me about", "is there"]))
    window = draw(st.sampled_from(["", " tomorrow", " today", " yesterday"]))
    domain = draw(st.sampled_from(["", " in my calendar", " in my email", " from my texts"]))
    return f"{prefix} {' '.join(terms)}{domain}{window}"


@PROFILE
@given(question=non_operator_questions())
def test_keyword_questions_score_and_rank_exactly_as_the_original(question: str) -> None:
    intent = parse_intent(question, NOW)
    assert intent.has_terms and intent.lookup_shaped is parse_intent(question, NOW).lookup_shaped
    mentioned = sorted((w for w in KEYWORDS if re.search(rf"\b{w}\b", question)), key=question.index)
    assert intent.keywords == tuple(dict.fromkeys(_stem(w) for w in mentioned)), question
    assert set(intent.names) == {n.lower() for n in NAMES if n in question}
    # Intended behavior change (ask-next-month-meeting requirement 2.2): an in-window calendar record in a
    # calendar-hinted question with zero term hits now scores a 0.25 term score instead of 0.0. Exclude
    # those records from the reference comparison; everything else must match the original exactly.
    def _intended_change(rec: Record) -> bool:
        if rec.domain != "calendar" or "calendar" not in intent.domain_hints or intent.window is None:
            return False
        if rec.ts is None or ref_score(rec, intent, NOW) != 0.0:
            return False
        return _ref_aware(intent.window[0]) <= _ref_aware(rec.ts) < _ref_aware(intent.window[1])  # type: ignore[operator]

    records = [r for r in RECORDS if not _intended_change(r)]
    for rec in records:
        assert score_record(rec, intent, NOW) == ref_score(rec, intent, NOW), (question, rec.id)
    got = rank(records, intent, NOW)
    want = ref_rank(records, intent, NOW)
    assert [(r.id, r.score) for r in got] == [(r.id, r.score) for r in want], question
    assert rank(records, intent, NOW) == got


# -- random adapter outcomes: never raise, failures land in skipped ----------------------------------------
OUTCOMES = ("ok", "empty", "raise", "timeout", "none")
FAKE_DOMAINS = ("calendar", "mail", "messages", "tasks")


def fake_adapter(domain: str, outcome: str):
    async def adapter(state, intent, limit):
        if outcome == "raise":
            raise RuntimeError("boom")
        if outcome == "timeout":
            await asyncio.sleep(1)
        if outcome == "none":
            return None
        if outcome == "empty":
            return []
        return [_util.make(domain, f"{domain}:1", "Piano lesson", "piano lesson at five", NOW)]
    return adapter


@PROFILE
@given(outcomes=st.lists(st.sampled_from(OUTCOMES), min_size=4, max_size=4))
def test_random_adapter_outcomes_never_raise_and_failures_are_skipped(outcomes: list[str]) -> None:
    by_domain = dict(zip(FAKE_DOMAINS, outcomes, strict=True))
    adapters = {d: fake_adapter(d, o) for d, o in by_domain.items()}
    question = "What about the piano lesson?"
    result = asyncio.run(real_retrieve(SimpleNamespace(), question, NOW, adapters=adapters, timeout=0.05))
    failed = {d for d, o in by_domain.items() if o in ("raise", "timeout")}
    ok = {d for d, o in by_domain.items() if o in ("ok", "empty")}
    assert set(result.skipped) == failed and len(result.skipped) == len(failed)
    assert set(result.searched) == ok
    assert {r.domain for r in result.records} == {d for d, o in by_domain.items() if o == "ok"}

    body = ask(AskService(transport=SecureModel(), remote_provider_allowed=True), question, result).body
    assert body["status"] == "answered"
    for domain in failed:
        assert domain in body["answer"]
    assert (" I could not search " in body["answer"]) is bool(failed)
    cited = {c["record_id"] for c in body["citations"]}
    assert cited <= {r.id for r in result.records}


# -- random secrets and long strings: scrubbed and clipped -----------------------------------------------------
secret_tokens = st.text(alphabet="0123456789abcdef", min_size=24, max_size=40).map(lambda h: "sk-" + h)
noise = st.text(alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=0x24F),
                max_size=120)
long_run = st.integers(0, 900).map(lambda n: "x" * n)


@PROFILE
@given(token=secret_tokens, before=noise, after=noise, run=long_run)
def test_text_with_secrets_and_long_strings_is_always_scrubbed_and_clipped(
    token: str, before: str, after: str, run: str
) -> None:
    text = f"{before} {token} {after} {run} {token}"
    cleaned = policy.scrub(text)
    assert token not in cleaned and len(cleaned) <= policy.SNIPPET_CHARS and "\n" not in cleaned
    rec = _util.make("calendar", "x:1", text, text, NOW)
    assert token not in rec.title and token not in rec.snippet
    assert len(rec.title) <= policy.TITLE_CHARS and len(rec.snippet) <= policy.SNIPPET_CHARS

    state = make_state(NOW)
    state.calendar_service.ingest({
        "event_id": "ev-secret", "title": f"Piano {token} {run}",
        "start": (NOW + timedelta(days=1, hours=2)).isoformat(),
        "end": (NOW + timedelta(days=1, hours=3)).isoformat(), "source": "test",
        "description": text, "location": f"{token} {run}",
    })
    result = asyncio.run(real_retrieve(state, "what is on my calendar tomorrow", NOW))
    assert any("Piano" in r.title for r in result.records)
    for r in result.records:
        assert token not in r.title and token not in r.snippet
        assert len(r.title) <= policy.TITLE_CHARS and len(r.snippet) <= policy.SNIPPET_CHARS
    assert FAKE_TOKEN not in " ".join(r.snippet + r.title for r in result.records)
