# Bugfix Requirements Document

## Introduction

In Ask NerveCenter, a question about a meeting in the following calendar month fails to find the meeting, for example "what meetings do I have next month?" or "when is my meeting with X next month?". The user reported only "it failed to find a meeting next month", so the exact wording is unknown. These requirements cover the plausible variants: a generic meetings list for next month, a named-person lookup, a single-meeting "when is" lookup, and a count. The calendar events exist and sit inside the next-month window, but Ask answers as if there are none.

A second reported variant has no window: "when do i next see pam?" was answered "I found nothing about pam in calendar, mail, ...". Verified with `parse_intent` (now = 2026-06-03): operator `next`, kind `aggregate`, keywords `('pam',)`, names `()`, domain_hints empty, filters `{}`. The verb "see" gives no calendar hint, and the lowercase name "pam" is only a keyword, so the `next` operator has nothing to compute and the event is never matched against attendees.

Deployment note (not a code task): the user's reply listed an older domain list (calendar ... wellness), which suggests the running server may be an older build than the current code. Before judging the fix, check that the deployed build matches the current source.

## Follow-up Round (after deploy of tag ask-cov-20261118-1048)

The first fix was deployed. The production question "when do I next see pam?" then returned: "The computed calendar calculation, based on all 30 calendar records with 10 shown, indicates the next event is a Family Video Call on 2026-11-19 [REF computed]. The shown record lists that next Family Video Call as Wed Nov 18, 18:30-19:15 [REF cal-d9f5f6e6b8]. I could not search briefing just now." Three defects remain, numbered 1.9 to 1.12 below:

- A. The `next` operator with the leftover filter "pam" matched no row, and `aggregate._apply_filters` fell back to all upcoming events, so an unrelated event was named as the answer (1.9, 1.10).
- B. The computed date (2026-11-19) disagrees with the record (Wed Nov 18, 18:30-19:15), most likely a UTC versus user-timezone formatting difference (1.11).
- C. The notice "I could not search briefing just now" appeared on a question where briefing was not relevant (1.12).

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the user asks a generic question about next month's meetings (for example "what meetings do I have next month?" or "any meetings next month?") and calendar events exist in next month whose titles do not contain the word "meeting" THEN the system finds no meeting and answers that nothing was found

1.2 WHEN the user asks for a meeting with a named person next month (for example "when is my meeting with X next month?") and the matching event carries the name only in its attendees, description, or location, or the title differs in form from the name THEN the system finds no meeting and answers that nothing was found

1.3 WHEN the user asks about next month's meetings and the matching events are recurring instances or all-day events THEN the system omits those events from the evidence and answers that nothing was found

1.4 WHEN the question contains "next month" together with other wording (for example "next meeting next month", "my next month meeting", or "meetings next month with X") THEN the system resolves the question inconsistently between the `next` operator and the next-month window, so the next-month events are not selected

1.5 WHEN next-month events are retrieved and passed to the model as evidence THEN the system can still produce a "not found" style answer instead of listing them

1.6 WHEN the user asks a person-and-time question using a verb such as "see", "meet", "meeting with", "appointment" or "visit" (for example "when do i next see pam?") THEN the system derives no calendar domain hint, so the `next` operator runs over no domain and computes nothing

1.7 WHEN the question has an operator of kind next, earliest or latest and a lowercase person name (for example "pam") THEN the system treats the name only as a keyword, not as a name, so it does not filter or match event attendees, and answers "I found nothing about pam in calendar, mail, ..."

1.8 WHEN a calendar event has the person only in its attendees (the title does not contain the name) THEN the calendar adapter snippet omits attendees, so the name cannot match, even when a calendar hint is present

1.9 WHEN the `next` operator has a leftover person/keyword filter (for example "pam") that matches no row THEN `aggregate._apply_filters` falls back to all upcoming events, and the system confidently names an unrelated event (for example "Family Video Call") as the answer

1.10 WHEN the filter matches no calendar row because the person appears in a form the filter misses (a nickname or full name such as "Pamela", an attendee email, the event description, or a contact name that differs from the event title) THEN the system does not look at contacts or other evidence (messages, mail, waiting_on) and so cannot tell the user whether Pam exists or when they are next due to see her

1.11 WHEN a computed `next` answer is rendered THEN the COMPUTED line shows a date taken from the UTC timestamp (2026-11-19) while the record text shows the user-timezone time (Wed Nov 18, 18:30-19:15), so the two disagree for evening events

1.12 WHEN a question has no briefing relevance (no briefing hint) and the briefing adapter raises or times out on real data THEN the answer ends with "I could not search briefing just now."

### Expected Behavior (Correct)

2.1 WHEN the user asks a generic question about next month's meetings THEN the system SHALL return all non-cancelled, non-deleted calendar events whose start falls in next month (first of next month inclusive to first of the month after, exclusive), regardless of whether the title contains the word "meeting"

2.2 WHEN the user asks for a meeting with a named person next month THEN the system SHALL return next-month events where the name matches the title, attendees, description, or location, and SHALL list next-month events as candidates instead of answering "not found" when none match the name

2.3 WHEN next-month events include recurring instances or all-day events THEN the system SHALL include them in the evidence with correct dates, and all-day events SHALL be shown as all-day on the correct date

2.4 WHEN the question contains "next month" together with other wording THEN the system SHALL apply the next-month window consistently and SHALL select events from that window, never treating "next month" as the `next` operator

2.5 WHEN next-month events are retrieved and passed to the model as evidence THEN the system SHALL answer with those events and their dates, and SHALL NOT answer "not found"

2.6 WHEN the question uses a verb such as "see", "meet", "meeting with", "appointment" or "visit" THEN the system SHALL add the calendar domain hint so the `next` operator runs over `calendar_structured`

2.7 WHEN the operator is next, earliest or latest, the calendar hint is present, and a keyword is left over after intent parsing THEN the system SHALL use that keyword as a person/title filter matched against title, snippet and attendees, and SHALL return the matching event with its date (for "when do i next see pam?" the next upcoming event with Pam)

2.8 WHEN that person/title filter matches no event THEN the system SHALL NOT present any unmatched event as the answer; it MAY list upcoming events only as candidates that are clearly labeled as not matching the filter (revised in the follow-up round, see 2.10 and 2.11; the original wording let the fallback be presented as the answer)

2.9 WHEN a calendar event lists the person as an attendee THEN the calendar adapter snippet SHALL include the attendee names and emails, so a name such as "pam" matches an event whose title does not contain it

2.10 WHEN the `next` operator has a person/keyword filter that matches no row THEN the system SHALL NOT compute or present an unrelated event as the answer, and SHALL say that no upcoming event mentions "pam" (naming the filter)

2.11 WHEN the filter matches nothing and upcoming events exist THEN the system MAY list the nearest upcoming events, and SHALL label them as candidates that do not match the filter, outside the computed answer (this supersedes the unlabeled fallback in the earlier 2.8)

2.12 WHEN the filter is a person name THEN the system SHALL match it against title, description, location, attendee names and attendee emails, including nickname and full-name variants (for example "pam" and "Pamela") and a contact whose name or email resolves to the same person, before concluding that nothing matches

2.13 WHEN the calendar has no match for the person THEN the system SHALL consider cross-domain evidence (contacts, messages, mail, waiting_on) as supporting context, SHALL cite it only when it actually mentions a planned meeting or visit with that person, and SHALL otherwise state that no upcoming event was found

2.14 WHEN a computed answer includes an event date or time THEN the system SHALL render it in the user timezone (`NERVE_USER_TIMEZONE`, the same handling as commit bc450082), so the COMPUTED line and the record text show the same day and time

2.15 WHEN the briefing adapter cannot read briefing data THEN the system SHALL fail soft: it SHALL NOT add "I could not search briefing just now." to a question where briefing was not relevant (no briefing hint), and SHALL keep the notice only when the user asked for briefing content; the adapter SHALL produce records from real briefing data without raising

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the user asks for the next appointment without a window (for example "what is my next appointment?") THEN the system SHALL CONTINUE TO use the `next` operator and return the earliest upcoming event

3.2 WHEN the user asks about "this week", "next week", "last month", "this month", "tomorrow", or "today" THEN the system SHALL CONTINUE TO parse the same window and return the events in it

3.3 WHEN no calendar event exists in next month THEN the system SHALL CONTINUE TO answer that nothing was found, without inventing meetings

3.4 WHEN a calendar event is cancelled or deleted THEN the system SHALL CONTINUE TO exclude it from results

3.5 WHEN the user asks a keyword question in another domain (bills, mail, tasks, wellness) with a window THEN the system SHALL CONTINUE TO return the same records, with the same keyword gating and ranking, as before

3.6 WHEN the user asks a calendar question with no window and no "next" wording THEN the system SHALL CONTINUE TO read events from 30 days back to 180 days ahead

3.7 WHEN the user asks a count or aggregate question over calendar events (for example "how many meetings do I have next month?") THEN the system SHALL CONTINUE TO compute the count over typed calendar rows

3.8 WHEN the user asks a `next` operator question for bills or another non-calendar domain (for example "when is my next bill due?") THEN the system SHALL CONTINUE TO compute it as before, with no calendar hint and no person/title filter added

3.9 WHEN the question contains a capitalised name (for example "Pam" or "Bob") THEN the system SHALL CONTINUE TO treat it as a name exactly as before

3.10 WHEN a keyword filter on the `next` operator matches at least one row (for example "next dentist appointment") THEN the system SHALL CONTINUE TO compute over the matching rows only and return the earliest match

3.11 WHEN the `next` operator has no filter (for example "what is my next appointment?") THEN the system SHALL CONTINUE TO compute over all upcoming events with no "no match" message and no candidate label

3.12 WHEN the user timezone is unset or invalid THEN the system SHALL CONTINUE TO format dates exactly as before, and bill, task and other non-calendar computed dates SHALL CONTINUE TO render as before unless they are timestamps of a calendar event

3.13 WHEN the user asks a briefing question (for example "what needs my attention?" or "give me my briefing") and the briefing adapter works THEN the system SHALL CONTINUE TO return briefing records, and WHEN a non-briefing domain fails or times out on a question that asked for that domain THEN the system SHALL CONTINUE TO show the "could not search" notice for it
