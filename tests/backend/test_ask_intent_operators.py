"""Intent parsing: operators, operator words kept out of keywords, question kind, new domain hints."""
from __future__ import annotations
from datetime import UTC, datetime
import pytest
import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.retrieval.intent import (
    AGGREGATE,
    CONVERSATIONAL,
    DOMAIN_LIST,
    FOLLOWUP,
    LOOKUP,
    OVERVIEW,
    Operator,
    parse_intent,
    parse_operator,
)
NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)  # a Wednesday
@pytest.mark.parametrize(("question", "kind", "fld"), [
    ("what is my biggest bill?", "max", "amount"),
    ("what is my largest bill", "max", "amount"),
    ("what is my most expensive bill", "max", "amount"),
    ("what is my highest bill", "max", "amount"),
    ("what is my cheapest bill", "min", "amount"),
    ("what is my smallest bill", "min", "amount"),
    ("what is my lowest bill", "min", "amount"),
    ("what is my highest priority task", "max", "priority"),
    ("which bills are overdue?", "overdue", "due"),
    ("which bills are past due", "overdue", "due"),
    ("bills due this week", "due_soon", "due"),
    ("what is due soon", "due_soon", "due"),
    ("what is my average bill", "avg", "amount"),
    ("how many bills do I have", "count", None),
    ("how many unread emails", "count", None),
    ("how much do I owe in total?", "sum", "amount"),
    ("what is the total of my bills", "sum", "amount"),
    ("what is my earliest bill", "earliest", "ts"),
    ("what is my oldest bill", "earliest", "ts"),
    ("what is my latest email", "latest", "ts"),
    ("what is my most recent text", "latest", "ts"),
    ("what is my next appointment", "next", "ts"),
])
def test_operator_extraction(question: str, kind: str, fld: str | None) -> None:
    op = parse_intent(question, NOW).operator
    assert op == Operator(kind=kind, n=None, field=fld)
    assert parse_intent(question, NOW).kind == AGGREGATE
@pytest.mark.parametrize(("question", "n"), [
    ("top 3 bills", 3), ("top five bills", 5), ("top ten bills", 10), ("top 25 bills", 25),
])
def test_top_n_carries_its_count(question: str, n: int) -> None:
    assert parse_intent(question, NOW).operator == Operator(kind="top_n", n=n, field="amount")
def test_no_operator_for_plain_lookup_and_next_week_window() -> None:
    assert parse_operator("find texts from Laura") is None
    assert parse_operator("what meetings do I have next week") is None
    assert parse_operator("what is on my calendar next monday") is None
    assert parse_intent("find texts from Laura", NOW).operator is None
@pytest.mark.parametrize("question", [
    "what is my biggest bill?", "which bills are overdue?", "what is my cheapest bill",
    "what is my average bill", "what is my earliest bill", "what is my latest email",
    "how much do I owe in total?", "top 3 bills",
])
def test_operator_words_are_not_match_terms(question: str) -> None:
    intent = parse_intent(question, NOW)
    assert intent.keywords == ()
    assert not intent.has_terms
def test_real_terms_survive_next_to_an_operator() -> None:
    intent = parse_intent("what is my biggest water bill", NOW)
    assert intent.operator and intent.operator.kind == "max"
    assert intent.keywords == ("water",)
def test_filters_status_payee_and_name() -> None:
    assert parse_intent("which bills are overdue?", NOW).filters == {"status": "unpaid"}
    assert parse_intent("how much do I owe in total?", NOW).filters.get("status") == "unpaid"
    assert parse_intent("which bills are paid", NOW).filters.get("status") == "paid"
    assert parse_intent("how much did I pay to Acme", NOW).filters.get("payee") == "acme"
    assert parse_intent("latest email from Sam", NOW).filters.get("name") == "sam"
@pytest.mark.parametrize(("question", "kind"), [
    ("what is my biggest bill?", AGGREGATE),
    ("show my bills", DOMAIN_LIST),
    ("any email?", DOMAIN_LIST),
    ("is anything wrong right now?", DOMAIN_LIST),
    ("what do I owe", DOMAIN_LIST),
    ("hello", CONVERSATIONAL),
    ("Hi", CONVERSATIONAL),
    ("thanks", CONVERSATIONAL),
    ("what can you do?", CONVERSATIONAL),
    ("help", CONVERSATIONAL),
    ("when is it due?", FOLLOWUP),
    ("and the one after that", FOLLOWUP),
    ("what about the other one", FOLLOWUP),
    ("find texts from Laura", LOOKUP),
    ("what did the school send about Friday", LOOKUP),
    ("what do my notes say about the lease?", LOOKUP),
    ("When is my piano lesson with Marta tomorrow?", LOOKUP),
])
def test_kind_classification(question: str, kind: str) -> None:
    assert parse_intent(question, NOW).kind == kind
def test_domain_hint_blocks_conversational_and_names_block_followup() -> None:
    assert parse_intent("hello bills", NOW).kind != CONVERSATIONAL
    assert parse_intent("when is it Marta", NOW).kind != FOLLOWUP
def test_briefing_domain_only_question_is_an_overview() -> None:
    intent = parse_intent("show my briefing", NOW)
    assert intent.kind == OVERVIEW and "briefing" in intent.domain_hints
def test_domain_only_question_with_window_stays_a_lookup() -> None:
    intent = parse_intent("what meetings do I have next week", NOW)
    assert intent.kind == LOOKUP and intent.window is not None
@pytest.mark.parametrize(("question", "domain"), [
    ("how much did I spend", "finance"),
    ("what are my commitments", "obligations"),
    ("what do my notes say", "knowledge"),
    ("how did I sleep", "health"),
    ("did I do my morning routine", "routines"),
    ("show my daily briefing", "briefing"),
    ("any phishing threats", "security"),
    ("is the kubernetes cluster ok", "systems"),
    ("show my bills", "bills"),
    ("what do i owe", "obligations"),
    ("what do i owe", "finance"),
    ("is anything wrong", "incidents"),
    ("is anything wrong", "security"),
    ("is anything wrong", "systems"),
    ("what needs my attention", "briefing"),
])
def test_new_domain_hints(question: str, domain: str) -> None:
    assert domain in parse_intent(question, NOW).domain_hints
def test_domain_only_question_has_no_required_terms() -> None:
    for q in ("show my bills", "any email?", "is anything wrong right now?", "show my notes"):
        intent = parse_intent(q, NOW)
        assert not intent.has_terms, (q, intent.keywords)
        assert intent.kind == DOMAIN_LIST
def test_conversational_question_has_no_keywords() -> None:
    assert parse_intent("what can you do?", NOW).keywords == ()
def test_lookup_keywords_unchanged() -> None:
    intent = parse_intent("find texts from Laura", NOW)
    assert intent.names == ("laura",) and intent.keywords == () and intent.kind == LOOKUP
    intent = parse_intent("what did the school send about Friday", NOW)
    assert "school" in intent.keywords and "send" in intent.keywords and intent.kind == LOOKUP
