"""Integration: ``POST /api/context/ask`` and ``/api/context/ask/stream`` (spec
ask-nervcenter-answer-coverage, task 3.20).

Each flow runs through both endpoints: the stream's ``done`` event must carry the same answer
facts as the plain endpoint. Fakes only: the seeded world of ``test_ask_categories``, a scripted
model, no network.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.ask import AskService
from backend.context.retrieval.sources import ADAPTERS

from test_ask_answer_coverage_exploration import Run, bill_rows, check_grounded, harness, mentions_amount
from test_ask_categories import cites, computed, save_turns, world

ENDPOINTS = ("ask", "stream")


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for frame in text.split("\n\n"):
        lines = [ln for ln in frame.splitlines() if not ln.startswith(":")]
        if not lines:
            continue
        name = next(ln[7:] for ln in lines if ln.startswith("event: "))
        data = json.loads(next(ln[6:] for ln in lines if ln.startswith("data: ")))
        events.append((name, data))
    return events


def post(run: Run, endpoint: str, question: str, **kw: Any) -> dict[str, Any]:
    """The answer body from either endpoint (the stream's ``done`` event)."""
    payload = {"question": question, **kw}
    if endpoint == "ask":
        response = run.client.post("/api/context/ask", json=payload)
        assert response.status_code == 200, response.text
        return response.json()
    response = run.client.post("/api/context/ask/stream", json=payload)
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    names = [n for n, _ in events]
    assert names[0] == "meta" and names[-1] == "done" and "error" not in names, names
    return events[-1][1]


def grounded(run: Run, body: dict[str, Any]) -> dict[str, Any]:
    check_grounded(run, body)
    assert body["citations"] and all(c.get("title") for c in body["citations"])
    return body


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_biggest_bill(endpoint: str) -> None:
    with world() as run:
        body = grounded(run, post(run, endpoint, "what is my biggest bill?"))
        assert body["status"] == "answered"
        assert "Cedar Rent ($1,500.00)" in computed(body) and cites(body, "Cedar Rent")


def test_stream_emits_the_answer_as_deltas_before_done() -> None:
    with world() as run:
        events = parse_sse(run.client.post(
            "/api/context/ask/stream", json={"question": "what is my biggest bill?"}).text)
        deltas = "".join(d["text"] for n, d in events if n == "delta")
        assert "Cedar Rent" in deltas and "Cedar Rent" in events[-1][1]["answer"]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_two_turn_session_follows_up_on_the_previous_bill(endpoint: str) -> None:
    with world() as run:
        first = grounded(run, post(run, endpoint, "what is my biggest bill?", session_id="s1"))
        save_turns(run, "s1", [("what is my biggest bill?", first)])
        due = grounded(run, post(run, endpoint, "when is it due?", session_id="s1"))
        assert "2026-06-23" in due["answer"] and cites(due, "Cedar Rent") and not cites(due, "Acme Power")
        save_turns(run, "s1", [("what is my biggest bill?", first)])
        after = grounded(run, post(run, endpoint, "and the one after that", session_id="s1"))
        assert cites(after, "Acme Power") and not cites(after, "Cedar Rent")


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_follow_up_without_a_session_is_standalone(endpoint: str) -> None:
    with world() as run:
        body = post(run, endpoint, "when is it due?")
        assert run.seen[-1].previous_subject is None  # nothing carried over
        assert sum(1 for c in body["citations"] if c["domain"] == "bills") > 1  # not narrowed to one bill


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_cross_domain_taxes_question_returns_several_domains(endpoint: str) -> None:
    from backend.context.retrieval.ranker import MAX_RECORDS
    with world() as run:
        body = grounded(run, post(run, endpoint, "what is going on with my taxes?"))
        domains = {c["domain"] for c in body["citations"]}
        assert {"mail", "obligations", "tasks"} <= domains and len(body["citations"]) <= MAX_RECORDS


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_briefing_and_status_questions(endpoint: str) -> None:
    with world() as run:
        body = grounded(run, post(run, endpoint, "what do I need to know today?"))
        assert cites(body, "Needs attention: Call Pat Rivera back") and cites(body, "Open incidents")
        body = grounded(run, post(run, endpoint, "is anything wrong right now?"))
        assert "2 open (1 high, 1 low)" in body["answer"] and cites(body, "System: nas")
        body = grounded(run, post(run, endpoint, "how many emails were held today?"))
        assert "gate holds: 4 held" in body["answer"] and "COMPUTED" not in body["answer"]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_domain_larger_than_the_evidence_budget_gives_a_correct_total_and_scope(endpoint: str) -> None:
    spec = [(1000 + 7 * i, "pending" if i % 4 else "confirmed_paid", (i % 30) - 10) for i in range(45)]
    total = sum(c for c, s, _ in spec if s == "pending")
    with harness(bill_rows(spec)) as run:
        body = post(run, endpoint, "how much do I owe in total?")
        check_grounded(run, body)
        line = next(ln for ln in body["answer"].splitlines() if ln.startswith("COMPUTED:"))
        assert mentions_amount(line, total), (total, line)
        assert f"over {sum(1 for _, s, _ in spec if s == 'pending')}" in line
        assert "Based on" in line and "of 45 bills" in line  # the scope covers the whole domain
        assert "Showing" in line and "of 45 bills as sources" in line  # and says it shows fewer


class Plain:
    """A transport that is not the secure local route (no ``check``)."""

    fixture_mode = True
    base_url = "http://omniroute.invalid"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, model, prompt, parameters=None, timeout=None, **kw):
        self.calls += 1
        return {"content": "remote answer", "input_tokens": 1, "output_tokens": 1}

    def generate_stream(self, model, prompt, headers=None, max_tokens=None, timeout=None, **kw):
        self.calls += 1
        yield "remote answer"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_non_local_transport_is_policy_denied_with_no_retrieval(endpoint: str) -> None:
    with world() as run:
        transport = Plain()
        run.client.app.state.ask_service = AskService(transport=transport, remote_provider_allowed=False)
        body = post(run, endpoint, "what is my biggest bill?")
        assert body["status"] == "policy_denied" and body["policy"]["denied"] is True
        assert body["citations"] == [] and not body.get("records") and body["executed"] is False
        assert transport.calls == 0 and run.model.prompts == [] and run.seen == []  # nothing retrieved


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_failing_adapter_is_skipped_and_the_others_still_answer(endpoint: str) -> None:
    async def boom(state, intent, limit):
        raise RuntimeError("down")

    with world() as run, patch.dict(ADAPTERS, {"mail": boom, "contacts": boom}):
        body = grounded(run, post(run, endpoint, "any voicemail today?"))
        assert cites(body, "Voicemail from Pat Rivera")
        assert body["answer"].endswith(" I could not search mail, contacts just now.")
        assert sorted(run.seen[-1].skipped) == ["contacts", "mail"]
        assert "mail" not in run.seen[-1].searched and "voicemail" in run.seen[-1].searched


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_empty_domain_is_answered_in_code_naming_the_sources(endpoint: str) -> None:
    with harness([]) as run:
        body = post(run, endpoint, "which bills are overdue?")
        assert body["citations"] == [] and run.model.prompts == []  # no model call
        assert "records do not contain" not in body["answer"].lower()
        assert "bills" in body["answer"] or "matching data" in body["answer"]
