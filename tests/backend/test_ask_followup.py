"""Follow-up resolution: carries the subject, shifts the selection, overrides on a new name or domain,
standalone with empty history. Fakes only."""
from __future__ import annotations
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.retrieval import retrieve
from backend.context.retrieval.aggregate import StructuredRow
from backend.context.retrieval.followup import MAX_CARRIED, MAX_TURNS, resolve_followup
from backend.context.retrieval.intent import parse_intent
from backend.context.retrieval.sources._util import make
NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
AMOUNTS = {"water": 5000, "power": 45000, "rent": 120000, "phone": 9900}
def _rows() -> list[StructuredRow]:
    out = []
    for i, (name, cents) in enumerate(AMOUNTS.items()):
        due = NOW + timedelta(days=3 + i)
        rec = make("bills", name, f"Bill: {name.title()} Co", f"{cents / 100:.2f} USD, pending, due {due.date()}", due)
        out.append(StructuredRow(rec, {"amount_cents": cents, "currency": "USD", "status": "pending",
                                       "due_at": due, "ts": due, "payee": name}))
    return out
async def bills(state, intent, limit):
    return [r.record for r in _rows()][:limit]
async def bills_structured(state, intent, cap):
    return _rows()[:cap]
bills.structured = bills_structured  # type: ignore[attr-defined]
ADAPTERS = {"bills": bills}
ID = {name: r.record.id for name, r in zip(AMOUNTS, _rows(), strict=True)}
def turns(question: str, cited: list[str]) -> list[dict]:
    return [{"role": "user", "content": question},
            {"role": "assistant", "content": "answer text that must never be used as evidence",
             "citations": [{"record_id": i} for i in cited]}]
STATE = SimpleNamespace()
# -- resolve_followup ----------------------------------------------------------------------------
def test_standalone_without_history_or_when_not_a_followup() -> None:
    q = "when is it due?"
    assert resolve_followup(q, None, parse_intent(q, NOW)) is None
    assert resolve_followup(q, [], parse_intent(q, NOW)) is None
    history = turns("what is my biggest bill?", [ID["rent"]])
    other = "find texts from Laura"
    assert resolve_followup(other, history, parse_intent(other, NOW)) is None
def test_new_domain_or_name_overrides_the_carried_subject() -> None:
    history = turns("what is my biggest bill?", [ID["rent"]])
    for q in ("what about my email from Marta", "when is my piano lesson with Marta"):
        assert resolve_followup(q, history, parse_intent(q, NOW)) is None
def test_carries_previous_question_and_cited_ids() -> None:
    q = "when is it due?"
    resolved = resolve_followup(q, turns("what is my biggest bill?", [ID["rent"]]), parse_intent(q, NOW))
    assert resolved is not None
    assert resolved.question == "what is my biggest bill?"
    assert resolved.carried_ids == (ID["rent"],) and resolved.shift == 0
def test_the_one_after_that_shifts_by_one() -> None:
    q = "and the one after that"
    resolved = resolve_followup(q, turns("top 3 bills", [ID["rent"]]), parse_intent(q, NOW))
    assert resolved is not None and resolved.shift == 1
def test_nothing_cited_means_standalone() -> None:
    q = "when is it due?"
    assert resolve_followup(q, turns("what is my biggest bill?", []), parse_intent(q, NOW)) is None
def test_malformed_ids_and_roles_are_ignored() -> None:
    q = "when is it due?"
    history = [{"role": "user", "content": "what is my biggest bill?"},
               {"role": "assistant", "content": "x", "citations": [{"record_id": "'; DROP TABLE"},
                                                                  {"record_id": "computed"}]},
               "not a mapping"]  # type: ignore[list-item]
    assert resolve_followup(q, history, parse_intent(q, NOW)) is None
def test_only_recent_turns_and_a_bounded_id_list_are_used() -> None:
    q = "when is it due?"
    many = [f"bill-{i:010x}" for i in range(MAX_CARRIED + 8)]
    history = turns("show my bills", many)
    resolved = resolve_followup(q, history, parse_intent(q, NOW))
    assert resolved is not None and len(resolved.carried_ids) == MAX_CARRIED
    old = turns("what is my biggest bill?", [ID["rent"]])
    filler = [{"role": "user", "content": "hi"}] * MAX_TURNS
    assert resolve_followup(q, old + filler, parse_intent(q, NOW)) is None
# -- through retrieve ----------------------------------------------------------------------------
async def test_followup_rereads_the_carried_record_through_the_adapters() -> None:
    history = turns("what is my biggest bill?", [ID["rent"]])
    result = await retrieve(STATE, "when is it due?", NOW, adapters=ADAPTERS, history=history)
    assert [r.id for r in result.records] == [ID["rent"]]
    assert result.previous_subject and "Rent Co" in result.previous_subject
    assert result.computation is None
    assert result.intent.kind == "followup"
    assert "due" in result.records[0].snippet  # fresh text from the adapter, not stored text
async def test_followup_never_returns_records_the_adapters_do_not_return() -> None:
    gone = "bill-" + "f" * 10
    history = turns("what is my biggest bill?", [gone])
    result = await retrieve(STATE, "when is it due?", NOW, adapters=ADAPTERS, history=history)
    assert result.records == []
async def test_the_one_after_that_selects_the_next_ranked_record() -> None:
    history = turns("show my bills", [ID["power"]])
    first = await retrieve(STATE, "show my bills", NOW, adapters=ADAPTERS)
    ranked = [r.id for r in first.records]
    result = await retrieve(STATE, "and the one after that", NOW, adapters=ADAPTERS, history=history)
    assert [r.id for r in result.records] == [ranked[ranked.index(ID["power"]) + 1]]
async def test_the_one_after_the_last_is_empty() -> None:
    first = await retrieve(STATE, "show my bills", NOW, adapters=ADAPTERS)
    last = first.records[-1].id
    result = await retrieve(STATE, "and the one after that", NOW, adapters=ADAPTERS,
                            history=turns("show my bills", [last]))
    assert result.records == []
async def test_explicit_new_subject_ignores_history() -> None:
    history = turns("what is my biggest bill?", [ID["rent"]])
    with_history = await retrieve(STATE, "show my bills", NOW, adapters=ADAPTERS, history=history)
    without = await retrieve(STATE, "show my bills", NOW, adapters=ADAPTERS)
    assert [r.id for r in with_history.records] == [r.id for r in without.records]
    assert with_history.previous_subject is None
async def test_standalone_with_empty_history_matches_no_history() -> None:
    a = await retrieve(STATE, "when is it due?", NOW, adapters=ADAPTERS, history=[])
    b = await retrieve(STATE, "when is it due?", NOW, adapters=ADAPTERS)
    assert [r.id for r in a.records] == [r.id for r in b.records] and a.previous_subject is None
async def test_followup_still_isolates_adapter_failure() -> None:
    async def broken(state, intent, limit):
        raise RuntimeError("boom")
    history = turns("what is my biggest bill?", [ID["rent"]])
    result = await retrieve(STATE, "when is it due?", NOW, adapters={"bills": broken}, history=history)
    assert result.records == [] and result.skipped == ["bills"]
