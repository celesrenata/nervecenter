"""Ask wording: no-data, not-connected and skipped answers, no refusal sentence when evidence exists,
the conversational capability answer. Fakes only."""
from __future__ import annotations
import re
from datetime import timedelta
import pytest
import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.ask import CAPABILITY_ANSWER, AskService
from backend.context.retrieval.sources import ADAPTERS
from test_ask_answer_coverage_exploration import NOW, REFUSAL, bill_rows, harness
def test_prompt_no_longer_says_to_state_the_records_do_not_contain_the_answer() -> None:
    prompt = AskService._prompt("what is my biggest bill?", {
        "evidence": [{"id": "bill-0123456789", "summary": "Bill: Acme"}],
        "computed": "COMPUTED: The highest is Acme ($450.00).", "searched": ["bills"],
        "previous_subject": None,
    })
    assert "say so plainly" not in prompt
    assert "Never write that the records do not contain the answer when evidence is listed" in prompt
    assert "COMPUTED line" in prompt and "do not recompute" in prompt
    assert "REF computed: COMPUTED: The highest is Acme ($450.00)." in prompt
    assert "Sources searched: bills." in prompt
def test_prompt_with_no_evidence_says_no_records_were_found() -> None:
    prompt = AskService._prompt("anything", {"evidence": [], "computed": None, "searched": [],
                                             "previous_subject": None})
    assert "No records were found for this question." in prompt
def test_prompt_carries_the_previous_subject_line() -> None:
    prompt = AskService._prompt("when is it due?", {
        "evidence": [], "computed": None, "searched": [],
        "previous_subject": 'Previous subject: bills "Bill: Acme"'})
    assert 'Previous subject: bills "Bill: Acme"' in prompt
    assert "Question: when is it due?" in prompt
def test_no_data_names_searched_sources_and_skips_the_model() -> None:
    with harness([]) as run:
        body = run.ask("which bills are overdue?")
    assert run.model.prompts == []  # answered in code
    assert body["status"] == "answered" and body["grounded"] is False
    assert body["citations"] == [] and body["records"] == []
    assert "I searched" in body["answer"] and "bills" in body["answer"]
    assert "found no matching data" in body["answer"]
    assert REFUSAL not in body["answer"].lower()
def test_not_connected_sources_are_named() -> None:
    with harness([]) as run:
        body = run.ask("which bills are overdue?")
    assert "Not connected:" in body["answer"]
    assert body["provenance"]["unavailable"], body["provenance"]
    for domain in body["provenance"]["unavailable"]:
        assert domain.replace("_", " ") in body["answer"]
def test_skipped_source_is_reported_as_could_not_search(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(state, intent, limit):
        raise RuntimeError("boom")
    monkeypatch.setitem(ADAPTERS, "bills", broken)
    with harness([]) as run:
        body = run.ask("which bills are overdue?")
    assert re.search(r"I could not search [^.]*\bbills\b", body["answer"])
    assert run.model.prompts == []
def test_old_lookup_not_found_wording_is_preserved() -> None:
    with harness([]) as run:
        body = run.ask("find the zebrafish invoice")
    assert body["answer"].startswith("I found nothing about zebrafish")
    assert run.model.prompts == []
def test_domain_list_with_no_data_is_answered_in_code() -> None:
    with harness([]) as run:
        body = run.ask("show my bills")
    assert run.model.prompts == [] and "bills" in body["answer"]
    assert "found no matching data" in body["answer"]
def test_evidence_present_never_yields_the_refusal_sentence() -> None:
    rows = bill_rows([(5000, "pending", 3), (45000, "pending", 10), (800, "pending", -2)])
    with harness(rows) as run:
        body = run.ask("what is my biggest bill?")
    assert REFUSAL not in body["answer"].lower()
    assert run.model.prompts, "evidence exists, so the model is asked"
    prompt = run.model.prompts[-1]
    assert "REF computed: COMPUTED:" in prompt and "450.00" in prompt
    assert "Never write that the records do not contain the answer" in prompt
    assert body["provenance"]["computation"]["value"] == 45000
    assert {c["record_id"] for c in body["citations"]} <= run.retrieved_ids()
def test_computed_pseudo_reference_is_not_a_citation_or_ungrounded_claim() -> None:
    rows = bill_rows([(5000, "pending", 3), (45000, "pending", 10)])
    with harness(rows) as run:
        body = run.ask("what is my biggest bill?")
    assert all(c["record_id"] != "computed" for c in body["citations"])
    assert "computed" not in body["ungrounded_claims"]
@pytest.mark.parametrize("question", ["hello", "what can you do?", "thanks", "help"])
def test_conversational_gets_the_capability_answer_without_lookup_or_model(question: str) -> None:
    with harness(bill_rows([(5000, "pending", 3)])) as run:
        body = run.ask(question)
    assert body["answer"] == CAPABILITY_ANSWER
    assert run.model.prompts == []
    assert body["records"] == [] and body["citations"] == [] and body["ungrounded_claims"] == []
    assert body["provenance"]["searched"] == [] and body["provenance"]["computation"] is None
    assert REFUSAL not in body["answer"].lower()
def test_capability_answer_names_the_searchable_domains_with_examples() -> None:
    low = CAPABILITY_ANSWER.lower()
    for word in ("calendar", "email", "messages", "voicemail", "contacts", "tasks", "bills", "notes",
                 "health", "routines", "briefing", "security"):
        assert word in low, word
    assert "What is my biggest bill?" in CAPABILITY_ANSWER
def test_response_keeps_its_original_keys_and_adds_provenance_fields() -> None:
    rows = bill_rows([(5000, "pending", 3)])
    with harness(rows) as run:
        body = run.ask("what is my biggest bill?")
    for key in ("status", "answer", "citations", "records", "ungrounded_claims", "provenance"):
        assert key in body
    for key in ("computation", "truncated", "unavailable"):
        assert key in body["provenance"]
def test_rows_due_in_the_future_are_not_overdue_in_the_answer() -> None:
    rows = bill_rows([(5000, "pending", 3)])
    assert rows[0]["due_at"] > NOW.isoformat()
    with harness(rows) as run:
        body = run.ask("which bills are overdue?")
    assert body["provenance"]["computation"]["value"] == {"count": 0, "total_cents": 0}
    assert NOW + timedelta(days=3) > NOW

def test_scope_line_names_only_the_computed_domain() -> None:
    rows = bill_rows([(5000, "pending", 3), (45000, "pending", 10), (800, "pending", -2)])
    with harness(rows) as run:
        body = run.ask("what is my biggest bill?")
    text = body["provenance"]["computation"]["text"]
    assert "Based on 3 of 3 bills" in text
    assert "of 1 calendar" not in text and "health" not in text
