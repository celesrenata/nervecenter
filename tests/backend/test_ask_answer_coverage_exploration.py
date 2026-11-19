"""Bug condition exploration: Ask answers operator, domain-only, uncovered-domain,
follow-up and conversational questions.

Property 1 (spec ask-nervcenter-answer-coverage): for every question where the
data exists, the answer states the value computed independently from the seeded
data, cites only retrieved ids, and never says "records do not contain".

These tests encode the EXPECTED behaviour. They FAIL on the unfixed code, and the
failures are the counterexamples that prove the bug. Fakes only: seeded bills,
a scripted model that echoes the evidence it is given, no network.
"""
from __future__ import annotations

import contextlib
import inspect
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.api.endpoints import ask_sessions as sessions_ep
from backend.api.endpoints import context as context_endpoints
from backend.bills.service import BillService
from backend.context.ask import AskService
from backend.context.retrieval import retrieve as real_retrieve
from backend.wellness.service import WellnessService
from test_ask_retrieval import NOW, make_state

REFUSAL = "records do not contain"
PROFILE = settings(max_examples=12, deadline=None,
                   suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


# -- scripted model ------------------------------------------------------------------
class EchoModel:
    """Secure-transport fake (has ``check``). Echoes every evidence line with its
    citation. With no evidence it answers the way the real prompt makes a model
    answer ("say so plainly"): the refusal sentence. It never computes anything."""

    fixture_mode = True
    base_url = "http://omniroute.invalid"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def check(self, model: str) -> None:
        return None

    def _reply(self, prompt: str) -> str:
        self.prompts.append(prompt)
        lines = re.findall(r"^REF (\S+): (.*)$", prompt, re.MULTILINE)
        if not lines:
            return "The provided records do not contain information about that."
        return "\n".join(f"{summary} [{rid}]" for rid, summary in lines)

    def generate(self, model, prompt, parameters=None, timeout=None, **kw):
        return {"content": self._reply(prompt), "input_tokens": 1, "output_tokens": 1}

    def generate_stream(self, model, prompt, headers=None, max_tokens=None, timeout=None, **kw):
        yield self._reply(prompt)


# -- seeded bills ------------------------------------------------------------------------
def cents_text(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def bill_rows(spec: list[tuple[int, str, int]]) -> list[dict[str, Any]]:
    """spec = (amount_cents, status, due offset in days from NOW)."""
    return [
        {"id": f"b{i}", "payee": f"Payee{i:02d}Co", "amount_cents": str(cents), "currency": "USD",
         "status": status, "due_at": (NOW + timedelta(days=days)).isoformat()}
        for i, (cents, status, days) in enumerate(spec)
    ]


def payee(i: int) -> str:
    return f"Payee{i:02d}Co"


def mentions(text: str, token: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", text) is not None


def mentions_amount(text: str, cents: int) -> bool:
    flat = text.replace(",", "")
    return re.search(rf"(?<![\d.]){re.escape(cents_text(cents))}(?!\d)", flat) is not None


# -- harness: the real /api/context/ask endpoint, fixed clock ------------------------------
class Run:
    def __init__(self, client: TestClient, model: EchoModel, seen: list[Any]) -> None:
        self.client, self.model, self.seen = client, model, seen

    def ask(self, question: str, session_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"question": question}
        if session_id:
            payload["session_id"] = session_id
        response = self.client.post("/api/context/ask", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "answered", body
        return body

    def retrieved_ids(self) -> set[str]:
        return {r.id for result in self.seen for r in result.records}


@contextlib.contextmanager
def harness(bills: list[dict[str, Any]]) -> Iterator[Run]:
    model = EchoModel()
    seen: list[Any] = []

    async def clocked_retrieve(state, question, *args, **kwargs):
        kwargs.pop("now", None)
        result = await real_retrieve(state, question, NOW, **kwargs)
        seen.append(result)
        return result

    async def list_bills(self, *, status=None):
        return bills

    async def list_candidates(self, *, status=None):
        return []

    async def list_entries(self, *, from_dt=None, **kw):
        return [{"id": "w1", "mood": 6, "energy": 5, "stress": 3, "sleep_minutes": 400,
                 "journal_entry": "slept ok", "at": (NOW - timedelta(days=1)).isoformat()}]

    class FakeSystems:
        async def list_systems(self):
            return [{"name": "gateway", "status": "ok", "message": "healthy",
                     "checked_at": NOW.isoformat()}]

    async def fetch_knowledge(factory):
        note = SimpleNamespace(id="n1", author="me", body="The lease renews in March.",
                               created_at=NOW - timedelta(days=3))
        return [], [], [], [(note, "Home")]

    app = FastAPI()

    @app.middleware("http")
    async def principal(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.principal = {"sub": "alice"}
        return await call_next(request)

    app.include_router(context_endpoints.router)
    app.include_router(sessions_ep.router)
    state = make_state(NOW)
    state.repository_registry = SimpleNamespace(session_factory=lambda: None)
    state.services = {"systems": FakeSystems()}
    for key, value in vars(state).items():
        setattr(app.state, key, value)
    app.state.ask_sessions_repo = sessions_ep._MemoryPrefs()
    app.state.ask_service = AskService(transport=model, remote_provider_allowed=True)
    with patch.object(context_endpoints, "retrieve", clocked_retrieve), \
            patch.object(BillService, "list_bills", list_bills), \
            patch.object(BillService, "list_candidates", list_candidates), \
            patch.object(WellnessService, "list_entries", list_entries), \
            patch("backend.context.retrieval.sources.knowledge._fetch", fetch_knowledge):
        yield Run(TestClient(app), model, seen)


def check_grounded(run: Run, body: dict[str, Any]) -> None:
    """Shared assertions: no refusal, and every citation was actually retrieved."""
    answer = body["answer"]
    assert REFUSAL not in answer.lower(), f"refusal despite data: {answer!r}"
    cited = {c["record_id"] for c in body["citations"]}
    assert cited <= run.retrieved_ids(), (cited, run.retrieved_ids())


# -- strategies --------------------------------------------------------------------------
statuses = st.sampled_from(["pending", "confirmed_paid"])
bill_spec = st.tuples(st.integers(100, 900_000), statuses, st.integers(-60, 60))


def bills_strategy(min_size: int, max_size: int, min_unpaid: int = 0, min_overdue: int = 0):
    return (
        st.lists(bill_spec, min_size=min_size, max_size=max_size, unique_by=lambda b: b[0])
        .filter(lambda rows: sum(1 for c, s, d in rows if s == "pending") >= min_unpaid)
        .filter(lambda rows: sum(1 for c, s, d in rows if s == "pending" and d < 0) >= min_overdue)
    )


# -- 1. superlative: biggest bill ------------------------------------------------------------
@PROFILE
@given(spec=bills_strategy(5, 5))
@example(spec=[(5000, "pending", 3), (12000, "confirmed_paid", -5), (800, "pending", -2),
               (45000, "pending", 10), (9900, "pending", 25)])
def test_biggest_bill_names_the_highest_amount(spec) -> None:
    rows = bill_rows(spec)
    top_index = max(range(len(spec)), key=lambda i: spec[i][0])
    with harness(rows) as run:
        body = run.ask("what is my biggest bill?")
        check_grounded(run, body)
        assert mentions(body["answer"], payee(top_index)), body["answer"]
        assert mentions_amount(body["answer"], spec[top_index][0]), body["answer"]


# -- 2 + 6. total owed, including a domain larger than the evidence budget ----------------
@PROFILE
@given(spec=bills_strategy(2, 60, min_unpaid=2))
@example(spec=[(100 + i * 7, "pending" if i % 3 else "confirmed_paid", (i % 40) - 20) for i in range(40)])
def test_total_owed_sums_every_unpaid_bill(spec) -> None:
    total = sum(c for c, s, _ in spec if s == "pending")
    with harness(bill_rows(spec)) as run:
        body = run.ask("how much do I owe in total?")
        check_grounded(run, body)
        assert mentions_amount(body["answer"], total), (cents_text(total), body["answer"])


def test_forty_bills_total_covers_all_forty() -> None:
    spec = [(1000 + i, "pending", (i % 30) - 10) for i in range(40)]
    total = sum(c for c, _, _ in spec)
    with harness(bill_rows(spec)) as run:
        body = run.ask("how much do I owe in total?")
        check_grounded(run, body)
        assert mentions_amount(body["answer"], total), (cents_text(total), body["answer"])


# -- 3. overdue ----------------------------------------------------------------------------
@PROFILE
@given(spec=bills_strategy(4, 14, min_overdue=1))
def test_overdue_selects_only_unpaid_past_due_bills(spec) -> None:
    overdue = {i for i, (_, s, d) in enumerate(spec) if s == "pending" and d < 0}
    with harness(bill_rows(spec)) as run:
        body = run.ask("which bills are overdue?")
        check_grounded(run, body)
        for i in range(len(spec)):
            assert mentions(body["answer"], payee(i)) == (i in overdue), (i, spec[i], body["answer"])


# -- 4. domain-word-only questions -----------------------------------------------------------
@PROFILE
@given(spec=bills_strategy(1, 8))
def test_show_my_bills_lists_bills(spec) -> None:
    with harness(bill_rows(spec)) as run:
        body = run.ask("show my bills")
        check_grounded(run, body)
        assert any(mentions(body["answer"], payee(i)) for i in range(len(spec))), body["answer"]


def test_any_email_lists_recent_mail() -> None:
    with harness([]) as run:
        body = run.ask("any email?")
        check_grounded(run, body)
        assert "Water invoice for May" in body["answer"], body["answer"]


# -- 5. domains with no adapter ----------------------------------------------------------------
UNCOVERED = [
    ("how did I sleep this week?", {"health"}),
    ("is anything wrong right now?", {"status", "briefing"}),
    ("what do my notes say about the lease?", {"knowledge"}),
]


def test_uncovered_domains_are_searched_and_not_refused() -> None:
    failures: list[str] = []
    for question, wanted in UNCOVERED:
        with harness([]) as run:
            body = run.ask(question)
            searched = {d for result in run.seen for d in result.searched}
            if REFUSAL in body["answer"].lower():
                failures.append(f"{question!r}: refused ({body['answer']!r})")
            if not wanted & searched:
                failures.append(f"{question!r}: none of {sorted(wanted)} searched (searched={sorted(searched)})")
    assert not failures, "\n".join(failures)


# -- 7. follow-up with session_id --------------------------------------------------------------
@PROFILE
@given(spec=bills_strategy(5, 5))
def test_follow_up_resolves_the_previous_bill(spec) -> None:
    rows = bill_rows(spec)
    top = max(range(len(spec)), key=lambda i: spec[i][0])
    due = (NOW + timedelta(days=spec[top][2])).date().isoformat()
    with harness(rows) as run:
        first = run.ask("what is my biggest bill?", session_id="s1")
        turns = [
            {"id": "m1", "role": "user", "content": "what is my biggest bill?",
             "timestamp": NOW.isoformat()},
            {"id": "m2", "role": "assistant", "content": first["answer"], "timestamp": NOW.isoformat(),
             "citations": first["citations"], "records": first["records"]},
        ]
        saved = run.client.put("/api/ask/sessions", json={
            "active_id": "s1", "sessions": [{"id": "s1", "title": "bills", "messages": turns}]})
        assert saved.status_code == 200, saved.text
        run.seen.clear()
        second = run.ask("when is it due?", session_id="s1")
        check_grounded(run, second)
        bill_ids = {r.id for r in run.seen[-1].records if r.domain == "bills"}
        assert bill_ids, "turn 2 retrieved no bill: the follow-up lost its subject"
        assert mentions(second["answer"], payee(top)) or due in second["answer"], second["answer"]
        assert not mentions_other_bills(second["answer"], top, len(spec))


def mentions_other_bills(answer: str, keep: int, count: int) -> bool:
    return any(mentions(answer, payee(i)) for i in range(count) if i != keep)


# -- 8. conversational ---------------------------------------------------------------------------
def test_conversational_questions_are_not_refused() -> None:
    failures: list[str] = []
    for question in ("hello", "what can you do?"):
        with harness(bill_rows([(5000, "pending", 3)])) as run:
            body = run.ask(question)
            answer = body["answer"].lower()
            if REFUSAL in answer:
                failures.append(f"{question!r}: refused ({body['answer']!r})")
            elif not any(word in answer for word in ("calendar", "email", "mail", "bills", "search")):
                failures.append(f"{question!r}: no capability description ({body['answer']!r})")
    assert not failures, "\n".join(failures)


def test_retrieve_accepts_history_for_follow_ups() -> None:
    """Root cause 5: retrieval has no way to receive earlier turns."""
    assert "history" in inspect.signature(real_retrieve).parameters
