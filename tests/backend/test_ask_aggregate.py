"""aggregate.compute: every operator, empty input, ties, cents and currency, filters, scope text."""
from __future__ import annotations
from datetime import UTC, datetime, timedelta
import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.retrieval.aggregate import StructuredRow, compute
from backend.context.retrieval.intent import parse_intent
from backend.context.retrieval.sources._util import make
NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
def bill(ref: str, cents: int, status: str = "pending", days: int = 5, payee: str | None = None,
         currency: str = "USD") -> StructuredRow:
    due = NOW + timedelta(days=days)
    name = payee or f"Payee {ref}"
    rec = make("bills", ref, f"Bill: {name}", f"{cents / 100:.2f} {status}", due)
    return StructuredRow(rec, {"amount_cents": cents, "currency": currency, "status": status,
                               "due_at": due, "ts": due, "payee": name})
ROWS = [
    bill("a", 5000, "pending", 3),
    bill("b", 12000, "confirmed_paid", -5),
    bill("c", 800, "pending", -2),
    bill("d", 45000, "pending", 10),
    bill("e", 9900, "pending", -20),
]
def run(question: str, rows: list[StructuredRow] = ROWS):
    return compute(rows, parse_intent(question, NOW), NOW)
def test_max_and_min() -> None:
    top = run("what is my biggest bill?")
    assert top and top.value == 45000 and top.record_ids == (ROWS[3].record.id,)
    assert top.text.startswith("COMPUTED:") and "450.00" in top.text
    low = run("what is my cheapest bill?")
    assert low and low.value == 800 and low.record_ids == (ROWS[2].record.id,)
def test_top_n_orders_descending_and_limits() -> None:
    comp = run("top 3 bills")
    assert comp and [v for _, v in comp.value] == [45000, 12000, 9900]
    assert comp.record_ids == tuple(r.record.id for r in (ROWS[3], ROWS[1], ROWS[4]))
def test_sum_of_unpaid_only_for_owe() -> None:
    comp = run("how much do I owe in total?")
    assert comp and comp.value == 5000 + 800 + 45000 + 9900
    assert set(comp.record_ids) == {ROWS[i].record.id for i in (0, 2, 3, 4)}
def test_sum_without_status_covers_all_rows() -> None:
    comp = run("what is the total of my bills")
    assert comp and comp.value == 5000 + 12000 + 800 + 45000 + 9900
def test_average_rounds_half_away_from_zero() -> None:
    comp = run("what is my average bill", [bill("x", 100), bill("y", 101)])
    assert comp and comp.value == 101  # 100.5 -> 101
    comp = run("what is my average bill")
    assert comp and comp.value == (5000 + 12000 + 800 + 45000 + 9900) // 5
def test_count() -> None:
    comp = run("how many bills do I have")
    assert comp and comp.value == 5 and len(comp.record_ids) == 5
def test_overdue_is_unpaid_and_before_now() -> None:
    comp = run("which bills are overdue?")
    assert comp
    assert comp.value == {"count": 2, "total_cents": 800 + 9900}
    assert set(comp.record_ids) == {ROWS[2].record.id, ROWS[4].record.id}
    assert "2 overdue" in comp.text
def test_due_soon_uses_a_seven_day_horizon() -> None:
    comp = run("bills due this week")
    assert comp is not None
    # "this week" is a calendar window (Mon Jun 1 - Mon Jun 8): unpaid bills due in it
    assert set(comp.record_ids) == {ROWS[0].record.id, ROWS[2].record.id}
    assert comp.value == {"count": 2, "total_cents": 5800}
def test_due_soon_without_window_defaults_to_seven_days() -> None:
    comp = run("what is due soon")
    assert comp and set(comp.record_ids) == {ROWS[0].record.id}
def test_earliest_latest_next() -> None:
    assert run("what is my earliest bill").record_ids == (ROWS[4].record.id,)  # type: ignore[union-attr]
    assert run("what is my latest bill").record_ids == (ROWS[3].record.id,)  # type: ignore[union-attr]
    nxt = run("what is my next bill")
    assert nxt and nxt.record_ids == (ROWS[0].record.id,)
    assert nxt.value == ROWS[0].fields["due_at"].isoformat()
def test_empty_input_and_missing_operator_return_none() -> None:
    assert run("what is my biggest bill?", []) is None
    assert run("show my bills") is None
def test_rows_without_the_needed_field_return_none() -> None:
    rec = make("bills", "z", "Bill: Z", "no amount", NOW)
    assert run("what is my biggest bill?", [StructuredRow(rec, {})]) is None
    assert run("how much do I owe in total?", [StructuredRow(rec, {"status": "pending"})]) is None
def test_ties_break_by_due_date_then_id() -> None:
    early = bill("t1", 7000, days=1)
    late = bill("t2", 7000, days=9)
    comp = run("what is my biggest bill?", [late, early])
    assert comp and comp.record_ids == (early.record.id,)
    same_a = bill("s1", 7000, days=4)
    same_b = bill("s2", 7000, days=4)
    first = min(same_a.record.id, same_b.record.id)
    assert run("what is my biggest bill?", [same_b, same_a]).record_ids == (first,)  # type: ignore[union-attr]
def test_money_is_integer_cents_with_no_float_drift() -> None:
    rows = [bill(f"m{i}", 10) for i in range(30)] + [bill("big", 1)]
    comp = run("what is the total of my bills", rows)
    assert comp and comp.value == 301 and isinstance(comp.value, int)
    assert "$3.01" in comp.text
def test_currency_is_shown_and_never_mixed() -> None:
    eur = [bill("e1", 1050, currency="EUR"), bill("e2", 250, currency="EUR")]
    comp = run("what is the total of my bills", eur)
    assert comp and comp.value == 1300 and "13.00 EUR" in comp.text and "$" not in comp.text
    mixed = run("what is the total of my bills", [bill("u", 100), bill("e", 200, currency="EUR")])
    assert mixed and mixed.value == {"USD": 100, "EUR": 200}
def test_window_filter_limits_rows() -> None:
    comp = run("what is the total of my bills last month", ROWS)  # b (May 29) and e (May 14)
    assert comp and comp.value == 12000 + 9900
    assert run("what is the total of my bills last week", [bill("w", 100, days=60)]) is None
    comp = run("what is the total of my bills this week", ROWS)
    assert comp and comp.value == 5800 and comp.scope["window"] is not None
def test_status_filter_paid() -> None:
    comp = run("how many paid bills do I have")
    assert comp and comp.value == 1 and comp.record_ids == (ROWS[1].record.id,)
def test_payee_filter() -> None:
    rows = [bill("p1", 1000, payee="Acme Water"), bill("p2", 2500, payee="Globex"),
            bill("p3", 500, payee="Acme Water")]
    comp = run("how much did I pay to Acme", rows)
    assert comp and comp.value == 1500 and comp.scope["payee"] == "acme"
def test_scope_text_states_rows_filters_and_window() -> None:
    comp = run("how much do I owe in total?")
    assert comp
    assert comp.scope["rows"] == 4 and comp.scope["total_rows"] == 5 and comp.scope["status"] == "unpaid"
    assert "Based on 4 of 5 bills" in comp.text and "status unpaid" in comp.text
    windowed = run("what is the total of my bills this week")
    assert windowed and "window 2026-06-01 to 2026-06-08" in windowed.text
def test_does_not_mutate_input() -> None:
    rows = list(ROWS)
    run("top 3 bills", rows)
    assert rows == ROWS
def test_naive_and_iso_string_dates_are_accepted() -> None:
    rec = make("bills", "iso", "Bill: Iso", "x", NOW)
    row = StructuredRow(rec, {"amount_cents": 100, "status": "pending", "due_at": "2026-06-01T00:00:00"})
    comp = run("which bills are overdue?", [row])
    assert comp and comp.value["count"] == 1
