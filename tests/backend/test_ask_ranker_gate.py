"""Ranker keyword gate: unchanged for lookups, bypassed for aggregate, domain-list and overview."""
from __future__ import annotations
from datetime import UTC, datetime, timedelta
import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.retrieval.intent import parse_intent
from backend.context.retrieval.ranker import MAX_RECORDS, rank, score_record
from backend.context.retrieval.types import Record
NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
def rec(ref: str, domain: str, title: str, snippet: str = "", days_ago: float = 1.0) -> Record:
    return Record(id=f"{domain[:4]}-{ref:0>10}", domain=domain, title=title, snippet=snippet,
                  ts=NOW - timedelta(days=days_ago))
WATER = rec("1", "bills", "Bill: Water Co", "45.00 USD, pending")
POWER = rec("2", "bills", "Bill: Power Co", "120.00 USD, pending")
MAIL = rec("3", "mail", "Water invoice for May", "Your water invoice is ready.")
def test_lookup_gate_zeroes_records_with_no_term_hit() -> None:
    intent = parse_intent("find the water invoice", NOW)
    assert intent.kind == "lookup" and intent.has_terms
    assert score_record(POWER, intent, NOW) == 0.0
    assert score_record(MAIL, intent, NOW) > 0.0
    assert {r.id for r in rank([POWER, MAIL, WATER], intent, NOW)} == {MAIL.id, WATER.id}
def test_lookup_name_and_phrase_hits_still_score() -> None:
    texts = rec("4", "messages", "Laura", "dinner on friday")
    assert score_record(texts, parse_intent("find texts from Laura", NOW), NOW) >= 3.0
    quoted = parse_intent('find "water invoice"', NOW)
    assert score_record(MAIL, quoted, NOW) >= 3.0
    assert score_record(POWER, quoted, NOW) == 0.0
def test_lookup_without_terms_lists_recent_domain_records_only() -> None:
    intent = parse_intent("my voicemails", NOW)
    vm = rec("5", "voicemail", "Dentist office", "call back")
    assert intent.kind == "domain_list"
    assert score_record(vm, intent, NOW) > 0.5
    assert score_record(POWER, intent, NOW) == 0.0
def test_lookup_scoring_is_a_pure_function_of_the_record() -> None:
    intent = parse_intent("find the water invoice", NOW)
    assert score_record(MAIL, intent, NOW) == score_record(MAIL, intent, NOW)
def test_aggregate_bypasses_the_keyword_gate() -> None:
    intent = parse_intent("what is my biggest bill?", NOW)
    assert intent.kind == "aggregate" and not intent.has_terms
    assert score_record(WATER, intent, NOW) > 0.0
    assert score_record(POWER, intent, NOW) > 0.0
    assert {r.id for r in rank([WATER, POWER, MAIL], intent, NOW)} >= {WATER.id, POWER.id}
def test_aggregate_with_a_term_still_counts_the_match() -> None:
    intent = parse_intent("what is my biggest water bill", NOW)
    assert intent.kind == "aggregate" and intent.keywords == ("water",)
    assert score_record(WATER, intent, NOW) > score_record(POWER, intent, NOW) > 0.0
def test_domain_list_returns_the_domain_most_recent_first() -> None:
    intent = parse_intent("show my bills", NOW)
    assert intent.kind == "domain_list"
    older = rec("6", "bills", "Bill: Old", "x", days_ago=90)
    ranked = rank([older, WATER, MAIL], intent, NOW)
    assert [r.id for r in ranked] == [WATER.id, older.id]  # MAIL is another domain: 0
def test_domain_list_is_bounded_to_max_records() -> None:
    many = [rec(f"{i:02x}", "bills", f"Bill: P{i}", "x", days_ago=i) for i in range(MAX_RECORDS + 20)]
    ranked = rank(many, parse_intent("show my bills", NOW), NOW)
    assert len(ranked) == MAX_RECORDS
    assert ranked[0].ts >= ranked[-1].ts  # type: ignore[operator]
def test_overview_bypasses_the_gate() -> None:
    intent = parse_intent("show my briefing", NOW)
    assert intent.kind == "overview"
    brief = rec("7", "briefing", "Needs attention: reply to school", "needs action")
    assert score_record(brief, intent, NOW) > 0.0
    assert score_record(POWER, intent, NOW) == 0.0
def test_conversational_has_no_domain_records() -> None:
    intent = parse_intent("hello", NOW)
    assert rank([WATER, MAIL], intent, NOW) == []
