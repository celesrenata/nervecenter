"""Hypothesis properties for Ask answer coverage (spec ask-nervcenter-answer-coverage, task 3.20).

1. Aggregates over random bill sets (more than ``MAX_RECORDS``) equal an independent reference and
   every citation was retrieved.
2. Questions made only of generic and domain words need no match term and give a bounded,
   recent-first list of the hinted domain.
3. Random adapter outcomes (ok, empty, raise, timeout, None) never raise and are named correctly.
4. Random two-turn sessions, even with forged saved records, never return a record the adapters and
   policy would not return for the same questions.

Fakes only: seeded bills, scripted model, no network.
"""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from types import SimpleNamespace

from hypothesis import given
from hypothesis import strategies as st

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.ask import AskService, _names
from backend.context.retrieval import policy, retrieve as real_retrieve
from backend.context.retrieval.intent import DOMAIN_LIST, parse_intent
from backend.context.retrieval.ranker import MAX_RECORDS, rank
from backend.context.retrieval.sources import _util
from backend.context.retrieval.sources.planning import bills_structured
from backend.context.retrieval.types import Record

from test_ask_answer_coverage_exploration import (
    PROFILE, bill_rows, bills_strategy, check_grounded, harness, mentions_amount, payee,
)
from test_ask_answer_coverage_preservation import SecureModel, ask as ask_service
from test_ask_retrieval import NOW


def money(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


# -- 1. aggregates over random bill sets ------------------------------------------------------------
@PROFILE
@given(spec=bills_strategy(13, 40))
def test_biggest_bill_equals_the_reference_for_more_than_max_records(spec) -> None:
    top = max(range(len(spec)), key=lambda i: spec[i][0])
    with harness(bill_rows(spec)) as run:
        body = run.ask("what is my biggest bill?")
        check_grounded(run, body)
        line = next(ln for ln in body["answer"].splitlines() if ln.startswith("COMPUTED:"))
        assert f"{payee(top)} ({money(spec[top][0])})" in line, line
        assert any(payee(top) in c["title"] for c in body["citations"]), body["citations"]


@PROFILE
@given(spec=bills_strategy(13, 40, min_unpaid=1))
def test_total_owed_equals_the_sum_of_every_unpaid_bill(spec) -> None:
    total = sum(c for c, s, _ in spec if s == "pending")
    with harness(bill_rows(spec)) as run:
        body = run.ask("how much do I owe in total?")
        check_grounded(run, body)
        line = next(ln for ln in body["answer"].splitlines() if ln.startswith("COMPUTED:"))
        assert f"total {money(total)} over {sum(1 for _, s, _ in spec if s == 'pending')}" in line, line
        assert mentions_amount(body["answer"], total)


@PROFILE
@given(spec=bills_strategy(13, 40, min_overdue=1))
def test_overdue_count_and_total_equal_the_reference(spec) -> None:
    due = [c for c, s, d in spec if s == "pending" and d < 0]
    with harness(bill_rows(spec)) as run:
        body = run.ask("which bills are overdue?")
        check_grounded(run, body)
        line = next(ln for ln in body["answer"].splitlines() if ln.startswith("COMPUTED:"))
        assert f"{len(due)} overdue, totalling {money(sum(due))}" in line, line
        cited = {m for c in body["citations"] if c["domain"] == "bills"
                 for m in re.findall(r"Payee\d\d\w+", c["title"])}
        shown = {payee(i) for i, (_, s, d) in enumerate(spec) if s == "pending" and d < 0}
        assert cited <= shown, (cited, shown)


@PROFILE
@given(spec=bills_strategy(13, 40).filter(lambda rows: any(s == "pending" and -2 <= d <= 4 for _, s, d in rows)))
def test_due_this_week_equals_the_reference(spec) -> None:
    # NOW is Wednesday 2026-06-03 12:00 UTC: this week is Mon Jun 1 00:00 to Mon Jun 8 00:00,
    # which is day offsets -2..4 from NOW.
    week = [c for c, s, d in spec if s == "pending" and -2 <= d <= 4]
    with harness(bill_rows(spec)) as run:
        body = run.ask("what bills are due this week?")
        check_grounded(run, body)
        line = next(ln for ln in body["answer"].splitlines() if ln.startswith("COMPUTED:"))
        assert f"{len(week)} due soon, totalling {money(sum(week))}" in line, line


# -- 2. generic and domain words only ---------------------------------------------------------------
DOMAIN_WORD = {"bills": "bills", "mail": "emails", "messages": "texts", "voicemail": "voicemails",
               "tasks": "tasks", "goals": "goals"}


@st.composite
def domain_only_questions(draw: st.DrawFn) -> tuple[str, str]:
    domain = draw(st.sampled_from(sorted(DOMAIN_WORD)))
    word = DOMAIN_WORD[domain]
    template = draw(st.sampled_from(["show my {w}", "any {w}?", "list my {w}", "show me all my {w}",
                                     "what are my {w}", "do I have any {w}"]))
    return domain, template.format(w=word)


ages = st.lists(st.tuples(st.sampled_from(sorted(DOMAIN_WORD) + ["calendar", "contacts"]),
                          st.one_of(st.none(), st.floats(0.01, 300))), min_size=1, max_size=40)


@PROFILE
@given(case=domain_only_questions(), items=ages)
def test_domain_only_questions_give_a_bounded_recent_first_domain_list(case, items) -> None:
    domain, question = case
    intent = parse_intent(question, NOW)
    assert not intent.has_terms and intent.kind == DOMAIN_LIST and domain in intent.domain_hints
    records = [
        Record(id=f"{policy.prefix_for(d)}-{i:010x}", domain=d, title=f"Item {i}", snippet="x",
               when=None if age is None else (NOW - timedelta(days=age)).isoformat(),
               ts=None if age is None else NOW - timedelta(days=age))
        for i, (d, age) in enumerate(items)
    ]
    out = rank(records, intent, NOW)
    wanted = [r for r in records if r.domain == domain]
    assert len(out) == min(len(wanted), len(out)) <= MAX_RECORDS
    assert out or not wanted
    assert {r.domain for r in out} <= intent.domain_hints
    stamped = [r.ts for r in out if r.ts is not None]
    assert stamped == sorted(stamped, reverse=True)  # most recent first
    # every record kept is at least as recent as every dated record of that domain left out
    if len(wanted) > len(out):
        kept = {r.id for r in out}
        newest_left = max((r.ts for r in wanted if r.id not in kept and r.ts is not None), default=None)
        if newest_left is not None and stamped:
            assert min(stamped) >= newest_left


# -- 3. random adapter outcomes ---------------------------------------------------------------------
OUTCOMES = ("ok", "empty", "raise", "timeout", "none")
DOMAINS = ("calendar", "mail", "bills", "tasks")


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
        return [_util.make(domain, f"{domain}:1", f"Sample {domain}", "piano lesson at five", NOW)]
    return adapter


@PROFILE
@given(outcomes=st.lists(st.sampled_from(OUTCOMES), min_size=4, max_size=4),
       question=st.sampled_from(["show my bills", "What about the piano lesson?", "how many bills do I have?"]))
def test_random_adapter_outcomes_name_sources_and_never_raise(outcomes, question) -> None:
    by_domain = dict(zip(DOMAINS, outcomes, strict=True))
    adapters = {d: fake_adapter(d, o) for d, o in by_domain.items()}
    result = asyncio.run(real_retrieve(SimpleNamespace(), question, NOW, adapters=adapters, timeout=0.05))
    failed = {d for d, o in by_domain.items() if o in ("raise", "timeout")}
    absent = {d for d, o in by_domain.items() if o == "none"}
    searched = {d for d, o in by_domain.items() if o in ("ok", "empty")}
    assert set(result.skipped) == failed and set(result.unavailable) == absent
    assert set(result.searched) == searched

    outcome = ask_service(AskService(transport=SecureModel(), remote_provider_allowed=True), question, result)
    body = outcome.body
    assert outcome.status_code == 200 and body["status"] == "answered"
    answer = body["answer"]
    assert (" I could not search " in answer) is bool(failed)
    for domain in failed:
        assert domain in answer
    cited = {c["record_id"] for c in body["citations"]}
    assert cited <= {r.id for r in result.records}
    if not result.records and result.computation is None:  # answered in code: sources are named
        assert body["citations"] == []
        if searched:
            assert _names(result.searched) in answer
        # The plain-lookup wording ("I found nothing about ...") is preserved verbatim (Req 3.2) and
        # does not list unconnected sources; the widened branch for aggregate/domain lists does.
        if absent and not (result.intent.lookup_shaped and result.intent.has_terms):
            assert f"Not connected: {_names(result.unavailable)}" in answer


# -- 4. random two-turn sessions never widen what is returned -----------------------------------------
FIRST = ["what is my biggest bill?", "which bills are overdue?", "show my bills", "how much do I owe in total?"]
SECOND = ["when is it due?", "and the one after that", "what about that one?", "how much is it?"]
FORGED = "FORGED-SECRET-9f3a"


@PROFILE
@given(spec=bills_strategy(3, 15), first=st.sampled_from(FIRST), second=st.sampled_from(SECOND),
       forge=st.booleans())
def test_two_turn_sessions_return_only_what_the_adapters_and_policy_allow(spec, first, second, forge) -> None:
    with harness(bill_rows(spec)) as run:
        state = run.client.app.state
        # Everything the adapters can return for these questions: each question on its own plus
        # every bill the structured entry point reads (a shifted pick may be any of them).
        allowed_rows = asyncio.run(bills_structured(state, parse_intent("show my bills", NOW), 5000)) or []
        standalone = {r.record.id for r in allowed_rows} | {
            r.id for q in (first, second)
            for r in asyncio.run(real_retrieve(state, q, NOW)).records
        }
        run.seen.clear()
        turn1 = run.ask(first, session_id="s1")
        forged = {"id": "bill-ffffffffff", "domain": "bills", "title": FORGED, "snippet": FORGED,
                  "when": None, "link": None}
        messages = [
            {"id": "u0", "role": "user", "content": first, "timestamp": NOW.isoformat()},
            {"id": "a0", "role": "assistant", "content": turn1["answer"], "timestamp": NOW.isoformat(),
             "citations": turn1["citations"] + ([{"record_id": forged["id"], "title": FORGED,
                                                  "domain": "bills"}] if forge else []),
             "records": turn1["records"] + ([forged] if forge else [])},
        ]
        saved = run.client.put("/api/ask/sessions", json={
            "active_id": "s1", "sessions": [{"id": "s1", "title": "t", "messages": messages}]})
        assert saved.status_code == 200, saved.text
        run.seen.clear()
        body = run.client.post("/api/context/ask", json={"question": second, "session_id": "s1"}).json()
        assert body["status"] == "answered", body
        retrieved = run.retrieved_ids()
        assert retrieved <= standalone, (retrieved - standalone)
        assert {c["record_id"] for c in body["citations"]} <= retrieved
        assert {r["id"] for r in body["records"]} <= retrieved
        assert FORGED not in body["answer"] and "bill-ffffffffff" not in body["answer"]
        assert all(r.domain not in policy.EXCLUDED_STORES for result in run.seen for r in result.records)
        assert re.search(r"bill-ffffffffff", str(body["citations"]) + str(body["records"])) is None
