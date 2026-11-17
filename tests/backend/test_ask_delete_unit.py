"""Calendar deletion: gate, wording, series rewrite, matching. Pure, no I/O."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import backend.api  # noqa: F401  (import order: avoids a circular import)

from backend.calendar.providers import ProviderEvent
from backend.calendar.series import (
    end_recurrence_at,
    is_open_ended,
    repeats_label,
    series_starts_in_future,
)
from backend.context import ask_actions, ask_delete

SENTENCE = ("I no longer see Laura for therapy, can you remove all my future "
            "appointments with her?")
CUT = datetime(2026, 6, 2, 1, 22, tzinfo=UTC)


def test_gate_matches_the_users_sentence_and_not_creation() -> None:
    assert ask_delete.looks_like_calendar_delete(SENTENCE)
    for q in ["cancel my next session with Laura", "delete the dentist appointment",
              "take the meeting off my calendar", "remove every therapy event"]:
        assert ask_delete.looks_like_calendar_delete(q), q
    for q in ["add lunch with Sam to my calendar friday at noon", "what is on my calendar?",
              "hello world", ""]:
        assert not ask_delete.looks_like_calendar_delete(q), q
    assert not ask_actions.looks_like_calendar_create(SENTENCE)


@pytest.mark.parametrize("question,scope", [
    (SENTENCE, "all_future"),
    ("delete every meeting with Dana", "all_future"),
    ("cancel my upcoming sessions with Laura", "all_future"),
    ("cancel my next session with Laura", "next"),
    ("remove tomorrow's appointment with Laura", "next"),
    ("cancel the appointment with Laura on Friday", "date"),
    ("cancel my appointments with Laura", "ambiguous"),
    ("cancel my appointment with Laura", "ambiguous"),
])
def test_scope_comes_from_the_wording(question: str, scope: str) -> None:
    assert ask_delete.scope_from_wording(question) == scope


class Model:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def generate(self, model, prompt, **kw):
        return {"content": self.reply}


def test_person_must_appear_in_the_request_and_scope_wins_over_the_model() -> None:
    reply = '{"is_delete": true, "person": "Laura", "topic": "therapy", "scope": "next"}'
    intent = ask_delete.extract_delete_intent(Model(reply), "m", SENTENCE)
    assert intent == {"person": "Laura", "topic": "therapy", "scope": "all_future"}
    invented = '{"is_delete": true, "person": "Zelda", "topic": null}'
    got = ask_delete.extract_delete_intent(Model(invented), "m", "cancel my next session with Laura")
    assert got["person"] == "Laura"
    none = '{"is_delete": true, "person": null}'
    assert ask_delete.extract_delete_intent(Model(none), "m", "remove all my appointments with her")["person"] is None
    assert ask_delete.extract_delete_intent(Model('{"is_delete": false}'), "m", SENTENCE) is None
    assert ask_delete.extract_delete_intent(Model("not json"), "m", SENTENCE) is None


def test_end_recurrence_replaces_count_and_keeps_exdate() -> None:
    rule = ["RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=TU", "EXDATE;TZID=America/Los_Angeles:20260414T100000"]
    out = end_recurrence_at(rule, CUT, all_day=False)
    assert out[0] == "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20260602T012159Z"
    assert out[1] == rule[1]


def test_end_recurrence_open_ended_and_all_day_and_two_rules() -> None:
    assert end_recurrence_at(["RRULE:FREQ=DAILY"], CUT, True) == ["RRULE:FREQ=DAILY;UNTIL=20260601"]
    two = end_recurrence_at(["RRULE:FREQ=WEEKLY;BYDAY=MO", "RRULE:FREQ=WEEKLY;BYDAY=TH", "RDATE:20260701T100000Z"],
                            CUT, False)
    assert [r.split(";")[-1] for r in two[:2]] == ["UNTIL=20260602T012159Z"] * 2 and two[2].startswith("RDATE")


def test_an_earlier_until_is_kept() -> None:
    out = end_recurrence_at(["RRULE:FREQ=WEEKLY;UNTIL=20260501T000000Z"], CUT, False)
    assert out == ["RRULE:FREQ=WEEKLY;UNTIL=20260501T000000Z"]


def test_open_ended_and_labels_and_future_start() -> None:
    assert is_open_ended(["RRULE:FREQ=WEEKLY"]) and not is_open_ended(["RRULE:FREQ=WEEKLY;COUNT=3"])
    assert not is_open_ended(["RRULE:FREQ=WEEKLY;UNTIL=20270101T000000Z"])
    assert repeats_label(["RRULE:FREQ=WEEKLY"]) == "repeats weekly"
    assert repeats_label(["RRULE:FREQ=WEEKLY;INTERVAL=2"]) == "repeats every 2 weeks"
    assert series_starts_in_future(datetime(2026, 7, 1, tzinfo=UTC), CUT)
    assert not series_starts_in_future(datetime(2026, 4, 1, tzinfo=UTC), CUT)


def ev(title: str, *, desc: str | None = None, names=(), emails=()) -> ProviderEvent:
    return ProviderEvent(ical_uid=title, title=title, description=desc,
                         extra={"attendee_names": list(names), "attendees": list(emails)})


def contact(name: str, *emails: str) -> SimpleNamespace:
    return SimpleNamespace(display_name=name, emails=list(emails))


def test_strong_and_possible_matches() -> None:
    events = [ev("Therapy with Laura"), ev("Therapy", names=["Laura Smith"]),
              ev("Standup", emails=["laura@x.test"]), ev("Notes", desc="see Laura"),
              ev("Laurasaurus parade"), ev("Dentist")]
    got = {e.title: c for e, c in ask_delete.match_events(events, "Laura", [contact("Laura Smith", "laura@x.test")])}
    assert got == {"Therapy with Laura": "strong", "Therapy": "strong", "Standup": "strong", "Notes": "possible"}


def test_two_contacts_with_the_same_first_name_make_everything_possible() -> None:
    contacts = [contact("Laura Smith", "a@x.test"), contact("Laura Jones", "b@x.test")]
    got = {c for _, c in ask_delete.match_events([ev("Therapy with Laura")], "Laura", contacts)}
    assert got == {"possible"}
    full = ask_delete.match_events([ev("Lunch with Laura Smith")], "Laura Smith", contacts)
    assert [c for _, c in full] == ["strong"]
