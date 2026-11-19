"""Bug condition exploration (defect A): unrelated event named as the answer (EXPECTED to fail pre-fix).

Root-cause findings (investigation A1-A6, run on UNFIXED code):
- A1: parse_intent("when do i next see pam?") gives operator next, keywords ('pam',), names (), so "pam"
  is only a keyword; no row hits it.
- A3/A6: aggregate._apply_filters._hit tokenises on whitespace/commas only, compares stems, and the
  prefix/substring branch needs len(k) > 4, so "pam" never matches "Pamela", "pamela@example.com" or
  "Pam's"; it also reads only title/snippet/payee, never fields["attendees"].
- A2/A4/A5: production data/contacts are not reachable from the test environment; cases below simulate them.
- Cause: `if matched: out = matched` leaves every row when nothing matches, so compute() returns the
  earliest unrelated event (Family Video Call).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.retrieval import retrieve
from backend.context.retrieval.aggregate import StructuredRow, compute
from backend.context.retrieval.intent import parse_intent
from backend.context.retrieval.types import Record

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
QUESTION = "when do i next see pam?"


def row(title: str, start: datetime, snippet: str = "", ref: str = "1", attendees: str = "") -> StructuredRow:
    snip = " | ".join(p for p in (start.strftime("%a %b %d, %H:%M"), snippet, attendees) if p)
    rec = Record(id=f"cale-{ref:0>10}", domain="calendar", title=title, snippet=snip, ts=start)
    return StructuredRow(rec, {"status": "scheduled", "ts": start, "payee": title})


def unrelated_rows(n: int = 30) -> list[StructuredRow]:
    rows = [row("Family Video Call", NOW + timedelta(days=1), attendees="with tony@hillyerd.com, james@hillyerd.com", ref="0")]
    rows += [row(f"KubeVirt meeting {i}", NOW + timedelta(days=2 + i), ref=str(i + 1)) for i in range(n - 1)]
    return rows


def _assert_not_answer(result, filt: str = "pam") -> None:
    text = (result.text if result else "").lower()
    named = result is not None and ("family video call" in text or "kubevirt" in text)
    if named:
        # allowed only when explicitly labeled as non-matching candidates
        assert "not matching" in text or "no upcoming event" in text
        assert not text.startswith("computed: the next is")
    else:
        assert result is None or "no upcoming event" in text
    if result is not None:
        assert filt in text and "no upcoming event" in text


def test_intent_for_pam_is_keyword_only() -> None:
    intent = parse_intent(QUESTION, NOW)
    assert intent.operator is not None and intent.operator.kind == "next"
    assert intent.keywords == ("pam",) and intent.names == ()


def test_compute_does_not_name_unrelated_event_as_answer() -> None:
    intent = parse_intent(QUESTION, NOW)
    result = compute(unrelated_rows(), intent, NOW)
    # unfixed: "COMPUTED: The next is Family Video Call on 2026-11-19 ..."
    assert result is not None, "must explain that no upcoming event mentions pam"
    assert "no upcoming event" in result.text.lower() and "pam" in result.text.lower()
    assert "the next is family video call" not in result.text.lower()


class _Cal:
    def __init__(self, rows):
        self.rows = rows

    async def list_events(self, *, start=None, end=None, limit=500, **_):
        return [r for r in self.rows if start <= r.start < end][:limit]


def _events(rows):
    from types import SimpleNamespace
    out = []
    for i, sr in enumerate(rows):
        out.append(SimpleNamespace(
            account_id="a", ical_uid=f"u{i}", title=sr.record.title, start=sr.fields["ts"],
            end=sr.fields["ts"] + timedelta(hours=1), all_day=False, location=None, description=None,
            deleted=False, status="confirmed",
            extra={"attendees": ["tony@hillyerd.com", "james@hillyerd.com"]} if i == 0 else {},
        ))
    return out


def test_retrieve_end_to_end_no_unrelated_computed_answer() -> None:
    from types import SimpleNamespace
    state = SimpleNamespace(repository_registry=SimpleNamespace(calendar=_Cal(_events(unrelated_rows()))))
    result = asyncio.run(retrieve(state, QUESTION, NOW))
    comp = result.computation
    assert comp is None or "no upcoming event" in comp.text.lower(), (
        f"computed answer names an unrelated event: {comp.text if comp else None}"
    )


@pytest.mark.parametrize(
    ("title", "snippet", "attendees"),
    [
        ("Quarterly review", "", "with pamela@example.com"),
        ("Dinner", "dinner with Pamela", ""),
        ("Visit", "", "with Pamela Jones, Bob"),
        ("Catch up", "Pam's place", ""),
    ],
)
def test_person_variants_are_matched(title: str, snippet: str, attendees: str) -> None:
    rows = unrelated_rows() + [row(title, NOW + timedelta(days=10), snippet, ref="99", attendees=attendees)]
    result = compute(rows, parse_intent(QUESTION, NOW), NOW)
    assert result is not None and title in result.text and "no upcoming event" not in result.text.lower()


def test_contact_alias_email_in_fields_is_matched() -> None:
    target = row("Lunch", NOW + timedelta(days=10), ref="98")
    target.fields["attendees"] = ["pam.jones@example.com"]
    result = compute(unrelated_rows() + [target], parse_intent(QUESTION, NOW), NOW)
    assert result is not None and "Lunch" in result.text


@settings(max_examples=25, deadline=None)
@given(filt=st.text(alphabet="qxzjkvw", min_size=3, max_size=8))
def test_unmatched_filter_never_names_a_row(filt: str) -> None:
    intent = parse_intent(f"when do i next see {filt}?", NOW)
    if not intent.keywords:
        return
    result = compute(unrelated_rows(), intent, NOW)
    assert result is None or "no upcoming event" in result.text.lower()
    if result is not None:
        assert "the next is family video call" not in result.text.lower()
