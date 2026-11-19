# Ask Next-Month Meeting Bugfix Design

## Overview

Ask NerveCenter fails to find calendar events when the question names "next month" and also carries a term the ranker treats as a required keyword or name. The next-month window itself is parsed correctly (`parse_window` returns the first of next month inclusive to the first of the following month exclusive). The events are then dropped by the keyword gate in `score_record`, because a calendar event's title and snippet rarely contain filler words ("planned") or a person's name that only appears in the attendee list. A secondary gap is that the calendar adapter's snippet omits attendees, so a name match on attendees can never succeed.

The fix is narrow. Calendar records that fall inside an explicit window, when the question is calendar-domain, stay candidates even with zero keyword/name hits. Name hits still rank higher. The adapter adds attendee names and emails to the snippet. Everything outside calendar-domain windowed lookups keeps its current scoring.

A second variant comes from a real report: "when do i next see pam?" answered "I found nothing about pam in calendar, mail, ...". `parse_intent` (now = 2026-06-03) gives operator `next`, kind `aggregate`, keywords `('pam',)`, names `()`, domain_hints empty, filters `{}`. Three gaps combine: "see" does not hint the calendar domain, the lowercase "pam" is only a keyword so it never filters or matches attendees, and the adapter snippet omits attendees. The fix adds verb-based calendar hints, a leftover-keyword person/title filter for next/earliest/latest operators with a calendar hint, a fallback to upcoming events when the filter matches nothing, and the attendee snippet change already planned.

Deployment check (not a code task): the user's reply listed an older domain list (calendar ... wellness), so the running server may be an older build. Confirm the deployed build matches the current source before judging results.

## Glossary

- **Bug_Condition (C)**: A windowed, calendar-domain lookup with terms (keywords or names) where an in-window calendar record has no term hit, so `score_record` returns 0.0 and the record never reaches the model.
- **Property (P)**: Such an in-window calendar record scores greater than 0, ranks below records with term hits, and appears in the evidence.
- **Preservation**: Scoring, gating and retrieval behavior for all non-calendar domains, calendar questions without a window, and no-term windowed questions.
- **score_record**: Function in `backend/context/retrieval/ranker.py` returning 0 for "does not match" or a positive relevance.
- **parse_window**: Function in `backend/context/retrieval/intent.py` that maps "next month", "this week" and similar phrases to a `(start, end)` pair.
- **calendar / calendar_structured**: Adapters in `backend/context/retrieval/sources/communications.py` that read events and build `Record` objects (title plus `" | "`-joined when/location/description snippet).
- **keyword gate**: The `if intent.has_terms and not selected_by_domain: if hits == 0: return 0.0` branch in `score_record`.
- **Operator question**: A question whose `intent.operator` is next, earliest or latest (kind `aggregate`), such as "when do i next see pam?".
- **Leftover keyword**: A keyword from `parse_intent` that is not a stop word, operator word or domain word, and not a capitalised name (for example `pam`).
- **Person/title filter**: A case-insensitive match of a leftover keyword against an event's title, snippet and attendees.

## Bug Details

### Bug Condition

The bug manifests when the question has a window (such as "next month"), a calendar domain hint, and at least one keyword or name, and a calendar event inside the window matches none of those terms in `title + snippet`. `score_record` returns 0.0 at the keyword gate and `rank` discards the record, so Ask answers "not found" although the events exist.

Confirmed by running `parse_intent` on the current code (now = 2026-06-03):

- "what meetings do I have next month?" gives keywords `()`, names `()`, window July 1 to Aug 1, no operator. This variant passes the ranker, because there are no terms and the in-window branch applies.
- "do I have anything planned next month?" gives keywords `('planned',)`. Events titled "Standup" are gated out.
- "when is my meeting with Bob next month?" gives names `('bob',)`. An event whose title is "Quarterly review" with Bob only in `extra["attendees"]` is gated out, because the adapter snippet has no attendees.
- "what's my dentist appointment next month?" gives keywords `('dentist',)`. A title like "Dr. Lee 3pm" is gated out.
- "what is my next meeting next month?" gives an aggregate `next` operator plus the window, so it takes the structured path and is not affected by the gate.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type (intent: Intent, record: Record)
  OUTPUT: boolean

  RETURN X.intent.window IS NOT NULL
         AND "calendar" IN X.intent.domain_hints
         AND X.intent.kind NOT IN (AGGREGATE, DOMAIN_LIST, OVERVIEW)
         AND X.intent.has_terms
         AND X.record.domain = "calendar"
         AND X.record.ts IN X.intent.window
         AND termHits(X.intent, X.record) = 0
END FUNCTION
```

**Variant B: person lookup with the `next` operator.** The bug also manifests when the question is a next/earliest/latest operator question whose wording signals a calendar event ("see", "meet", "meeting with", "appointment", "visit"), carries a leftover lowercase keyword, and the answer is "found nothing".

```
FUNCTION isBugConditionNextPerson(X)
  INPUT: X of type (question: string, events: list of Event)
  OUTPUT: boolean

  intent := parse_intent(X.question)
  RETURN intent.operator IN (NEXT, EARLIEST, LATEST)
         AND hasCalendarVerb(X.question)
         AND leftoverKeyword(intent) IS NOT NULL
         AND upcomingEventsExist(X.events)
         AND ("calendar" NOT IN intent.domain_hints
              OR attendeeOnlyMatch(leftoverKeyword(intent), X.events)
              OR NOT filterMatchesAnyEvent(leftoverKeyword(intent), X.events))
END FUNCTION
```

### Examples

- "do I have anything planned next month?" with a July "Standup" event: currently score 0.0 (not found); expected a positive score and the event listed.
- "when is my meeting with Bob next month?" with July "Quarterly review", attendee bob@example.com: currently not found; expected the event listed as a match (attendee hit) or as a candidate.
- "what's my dentist appointment next month?" with only July "Dr. Lee 3pm": currently not found; expected listed as a candidate.
- "what meetings do I have next month?" with July events: already returns them; must keep doing so.
- No events in July: expected "nothing found" (unchanged).
- "when do i next see pam?" with an upcoming event whose attendee is Pam and whose title is "Quarterly review": currently "I found nothing about pam"; expected that event cited with its date.
- "when do i next see pam?" with an upcoming event titled "Lunch with Pam": currently "found nothing"; expected that event cited with its date.
- "when do i next see pam?" with upcoming events but none involving Pam: currently "found nothing"; expected upcoming events listed as candidates.
- "when is my next bill due?": expected unchanged (no calendar hint, no filter).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `next` operator handling for "what is my next appointment?" and its window-free event range (now to +365 days).
- Window parsing for this/next/last week, last/this month, today, tomorrow, yesterday, named months.
- "Nothing found" when no calendar event exists in the window; no invented meetings.
- Cancelled and deleted events excluded by the adapters.
- Keyword gating and ranking for non-calendar domains, with or without a window.
- Calendar questions with no window and no "next" wording read 30 days back to 180 days ahead.
- Count and aggregate questions over typed calendar rows.
- `next` operator for bills and other non-calendar domains.
- Capitalised names ("Pam", "Bob") keep being parsed as names, exactly as before.

**Scope:**
All inputs that are not a windowed, term-bearing calendar-domain lookup are unaffected. That includes bills, mail, messages, tasks, wellness lookups with a window, calendar lookups without a window, and aggregate/domain-list/overview questions.

## Hypothesized Root Cause

1. **Keyword gate applied to windowed calendar events** (confirmed in `score_record`): `has_terms and not selected_by_domain` returns 0.0 when `hits == 0`, with no exception for records that the window and domain already select. The window is a stronger selector than a filler word.
2. **Attendees missing from the snippet** (confirmed in `calendar` and `calendar_structured`): the snippet is built from when, location and description only. `extra["attendees"]` and `extra["attendee_names"]` are never included, so names cannot hit.
3. **Possible adapter-level gaps** (unconfirmed): recurring instances or all-day events may be dropped or mis-dated by `list_events` at window edges (all-day end exclusivity, timezone). The exploration test covers a recurring and an all-day row through the adapter; if it passes on unfixed code, this cause is refuted and no adapter date change is made.
4. **Prompt-level "not found" despite evidence** (unconfirmed, requirement 1.5): out of scope unless the exploration test shows evidence reaching the model with a "not found" answer. Fixing 1 and 2 removes the usual trigger.
5. **No calendar hint from "see"-style verbs** (confirmed by `parse_intent` on "when do i next see pam?": domain_hints empty): the domain hint table has no entries for "see", "meet", "meeting with", "appointment" or "visit", so the `next` operator runs over no domain and computes nothing.
6. **Lowercase name is only a keyword** (confirmed: keywords `('pam',)`, names `()`): names come from capitalised tokens, so "pam" never filters or matches attendees. The operator path has no step that turns a leftover keyword into a person/title filter, and no fallback when the filter matches nothing, so the answer is "found nothing".
7. **Attendees missing from calendar snippets** (same as cause 2): without attendees in the snippet, "pam" cannot match an event titled "Quarterly review" even after causes 5 and 6 are fixed.
8. **Stale deployment** (unconfirmed, deployment check only): the user's reply listed an older domain list (calendar ... wellness), so the running server may be an old build. This is verified by comparing the deployed build to the source, not by a code change.

## Correctness Properties

Property 1: Bug Condition - In-window calendar events survive term gating

_For any_ intent and calendar record where the bug condition holds (isBugCondition returns true), the fixed `score_record` SHALL return a score greater than 0 that is lower than the score of any in-window calendar record with a term hit, and `rank` SHALL include the record in its output, so the answer lists next-month events instead of "not found". Attendee names and emails SHALL be part of the record snippet so name hits on attendees register.

For the next-operator variant (isBugConditionNextPerson), the fixed pipeline SHALL derive the calendar hint from calendar verbs, apply a leftover keyword as a person/title filter matched against title, snippet and attendees, cite the matching event with its date, and fall back to listing upcoming events as candidates when the filter matches nothing, never answering "found nothing" while upcoming events exist.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9**

Property 2: Preservation - Non-bug inputs score identically

_For any_ intent and record where the bug condition does NOT hold (isBugCondition returns false), the fixed `score_record` SHALL return exactly the same value as the original, preserving keyword gating for non-calendar domains, out-of-window calendar records, no-window lookups, aggregate/domain-list/overview kinds, and the `next` operator path. This includes `next` questions for bills and other domains (no calendar hint, no filter) and capitalised names parsed as names.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9**

Property 3: Bug Condition - No unrelated event as the answer (follow-up A)

_For any_ `next`/`earliest`/`latest` question with a person/keyword filter where no row matches the filter (isBugConditionUnrelatedFallback returns true), the fixed pipeline SHALL NOT produce a computed answer that names a non-matching event, SHALL state that no upcoming event mentions the filter, and MAY list upcoming events only under an explicit "candidates, not matching" label. Where the person exists in a form the filter missed (nickname, full name, attendee email, description, contact), the fixed filter SHALL match it.

**Validates: Requirements 2.8, 2.10, 2.11, 2.12, 2.13**

Property 4: Bug Condition - Computed dates use the user timezone (follow-up B)

_For any_ computed calendar event whose UTC date differs from its user-timezone date (isBugConditionDateMismatch returns true), the fixed COMPUTED text and value SHALL show the user-timezone date, equal to the date in the record's snippet.

**Validates: Requirements 2.14**

Property 5: Bug Condition - Briefing failure does not surface when irrelevant (follow-up C)

_For any_ question with no briefing hint where the briefing adapter raises or times out (isBugConditionBriefingNotice returns true), the fixed answer SHALL NOT contain "could not search briefing". With real briefing data the adapter SHALL return records without raising.

**Validates: Requirements 2.15**

Property 6: Preservation - Filtered and unfiltered `next`, dates and notices (follow-up)

_For any_ input where none of the follow-up bug conditions hold, the fixed code SHALL produce the same result as before: filters that match keep narrowing, unfiltered `next` is unchanged, dates without a timezone configured are unchanged, and the "could not search" notice still appears for a requested domain that fails.

**Validates: Requirements 3.10, 3.11, 3.12, 3.13**

## Fix Implementation

### Changes Required

Assuming the root cause analysis is correct:

**File**: `backend/context/retrieval/ranker.py`

**Function**: `score_record`

**Specific Changes**:
1. **Gate exception**: In the `intent.has_terms and not selected_by_domain` branch, when `hits == 0`, return 0.0 unless `intent.window is not None and in_window and record.domain == "calendar" and "calendar" in intent.domain_hints`. In that case keep the in-window bonus, set the term score to 0.25, then fall through to the domain and recency bonuses.
2. **Ordering**: A record with a term hit scores at least 1.0 (hit) + 2.0 (window) + 1.0 (domain) = 4.0. A zero-hit candidate scores at most 0.25 + 2.0 + 1.0 + 0.5 (recency) = 3.75, so hits always rank first.

**File**: `backend/context/retrieval/sources/communications.py`

**Functions**: `calendar`, `calendar_structured`

**Specific Changes**:
3. **Attendees in snippet**: Add a small helper that reads `extra["attendee_names"]` and `extra["attendees"]` (when `extra` is a dict) and appends `"with <names/emails>"` to the snippet parts, capped at about 10 entries. No change when the row has no extra or attendees.
   **Design gap resolved (hint option chosen):** "do I have anything planned next month?" had no calendar hint, so the gate exception (which needs `"calendar" in domain_hints`) would not fire. `parse_intent` now adds the calendar hint when a window is present and the question contains "planned" or "on my schedule". "planned" stays a match keyword so term hits still outrank zero-hit candidates. The gate exception stays hint-gated, so bill/mail/task records with no hits still score 0.0 (the alternative, rescuing by record domain regardless of hint, would loosen the gate for every windowed question).
4. **No window change**: `parse_window` and `_NEXT_NOT_WINDOW` stay as they are; they already treat "next month" as a window.

**File**: `backend/context/retrieval/intent.py`

**Function**: `parse_intent` (domain hint derivation)

**Specific Changes**:
5. **Calendar verb hints**: Add "see", "meet", "meeting with", "appointment" and "visit" (word-boundary match) to the calendar domain hints. Only add the hint when the operator is next/earliest/latest or a window is present, so other uses of "see" in non-operator questions keep their current hints.
   Implemented in `intent.py`: "see", "meet", "visit" add the hint only with a window or a next/earliest/latest operator, and are dropped from keywords.
6. **Leftover keyword filter (already exists)**: `_apply_filters` in `backend/context/retrieval/aggregate.py` (the `next` operator path, run from `service._compute`) already keeps rows matching a keyword and falls back to all rows when none match, so no new code was needed there. Original text: When the operator is next/earliest/latest and "calendar" is in `domain_hints`, record the leftover keywords (not stop words, operator words or domain words, and not already names) as `filters["person"]` (or the existing filter field used by the operator path). Capitalised names keep the existing name path.

**File**: `backend/context/retrieval/` operator path (the code that runs `next`/`earliest`/`latest` over `calendar_structured`)

**Specific Changes**:
7. **Match and fall back**: Filter upcoming events by a case-insensitive match of the person filter against title, snippet and attendees. Return the earliest match with its date. If nothing matches, return the upcoming events as candidates instead of an empty result. Without a calendar hint or a person filter, behavior is unchanged.

**File**: `backend/context/retrieval/sources/communications.py`

**Specific Changes**:
8. **Attendee snippet**: The attendee helper from change 3 also serves this variant, so "pam" matches an attendee-only event.

**Deployment (no code)**: Check that the running server build matches the current source; the older domain list in the user's reply suggests it may not.

## Testing Strategy

### Validation Approach

First surface counterexamples on the unfixed code, then confirm the fix and the preservation of all other scoring.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix, and confirm or refute the root cause.

**Test Plan**: Build calendar `Record`s dated in July 2026 (now = 2026-06-03 12:00 UTC) and score them against intents from `parse_intent`. Also run the `calendar` adapter against a fake state whose calendar service returns rows with attendees, a recurring instance and an all-day event. Run on UNFIXED code.

**Test Cases**:
1. **Filler keyword**: "do I have anything planned next month?" with a July "Standup" record (will fail on unfixed code)
2. **Attendee name**: "when is my meeting with Bob next month?" with title "Quarterly review", attendee bob@example.com in extra (will fail on unfixed code)
3. **Title mismatch**: "what's my dentist appointment next month?" with title "Dr. Lee 3pm" (will fail on unfixed code)
4. **Recurring and all-day**: adapter returns both, dated correctly (may fail on unfixed code)
5. **Attendee-only person lookup**: "when do i next see pam?" (now = 2026-06-03) with an upcoming event whose attendee is Pam and whose title does not contain Pam; expect that event cited with its date (will fail on unfixed code)
6. **Title person lookup**: same question with an event titled "Lunch with Pam"; expect that event cited with its date (will fail on unfixed code)
7. **No-match fallback**: same question with upcoming events that do not involve Pam; expect candidates listed, not "found nothing" (will fail on unfixed code)

**Expected Counterexamples**:
- `score_record` returns 0.0 for in-window calendar records, and `rank` returns an empty list.
- Possible causes: keyword gate with no window exception; attendees missing from the snippet; adapter edge handling.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := score_record'(input.record, input.intent, now)
  ASSERT result > 0
  ASSERT result < minScoreWithTermHit(input.intent)
  ASSERT input.record IN rank'([input.record], input.intent, now)
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT score_record(input.record, input.intent, now) = score_record'(input.record, input.intent, now)
END FOR
```

**Testing Approach**: Property-based testing, because it generates many question/record combinations across domains, windows and operators and catches gate edge cases that hand-picked examples miss. The existing `test_ask_answer_coverage_preservation.py` already uses a reference scorer (`ref_score`) for this style.

**Test Plan**: Observe `score_record` and `rank` on UNFIXED code for non-bug inputs, then capture them as properties.

**Test Cases**:
1. **Non-calendar windowed lookups**: bills/mail/tasks records with a window and zero term hits still score 0.0.
2. **Out-of-window calendar records**: still score 0.0 when terms exist and nothing hits.
3. **No-window calendar lookups and `next`-operator questions**: scores and structured rows unchanged.
4. **Windows parse identically**: this/next/last week, this/last month, today, tomorrow produce the same ranges.
5. **Other-domain `next`**: "when is my next bill due?" parses to the same intent and result, with no calendar hint or person filter.
6. **Capitalised names**: "when do I next see Pam?" and "meeting with Bob" parse names exactly as before.

### Unit Tests

- Scoring of in-window calendar records for filler keyword, name and title-mismatch cases.
- Attendee names and emails appear in the adapter snippet; absent attendees leave the snippet unchanged.
- Cancelled/deleted events remain excluded.

### Property-Based Tests

- Random in-window calendar records and term sets: score > 0, and below any term-hit score.
- Random non-bug (domain, window, kind, record) combinations: new score equals the original score.
- Rank order: term-hit records always precede zero-hit candidates.

### Integration Tests

- `retrieve` with a fake calendar service for "do I have anything planned next month?" returns the July events.
- "how many meetings do I have next month?" still counts from typed rows.
- A month with no events still yields no records.

## Follow-up Round: Deployed Fix (tag ask-cov-20261118-1048)

### Observed Production Answer

For "when do I next see pam?" the deployed fix produced: "The computed calendar calculation, based on all 30 calendar records with 10 shown, indicates the next event is a Family Video Call on 2026-11-19 [REF computed]. The shown record lists that next Family Video Call as Wed Nov 18, 18:30-19:15 [REF cal-d9f5f6e6b8]. I could not search briefing just now." The hint and operator now work (the `next` operator ran over 30 calendar rows). Three defects remain.

### Defect A: Unrelated Event Presented as the Answer

**Bug Condition:**
```
FUNCTION isBugConditionUnrelatedFallback(X)
  INPUT: X of type (intent: Intent, rows: list of StructuredRow)
  OUTPUT: boolean

  RETURN X.intent.operator IN (NEXT, EARLIEST, LATEST)
         AND X.intent.keywords IS NOT EMPTY
         AND NOT EXISTS r IN X.rows: filterHit(X.intent.keywords, r)
         AND X.rows IS NOT EMPTY
END FUNCTION
```

**Expected behavior:**
```
FUNCTION expectedBehaviorNoMatch(result, intent)
  RETURN result.computation IS NULL OR result.computation.matched = false
         AND result.text states "no upcoming event mentions <filter>"
         AND (result.candidates IS EMPTY OR result.candidates ARE labeled "not matching <filter>")
         AND NOT (result.text names an unmatched event as "the next" event)
END FUNCTION
```

**Root cause (confirmed by reading code):** in `backend/context/retrieval/aggregate.py` `_apply_filters`, the keyword block does `matched = [r for r in out if _hit(r)]` and `if matched: out = matched`. With no match, `out` keeps every row, so `compute` runs on all upcoming events and returns the earliest (Family Video Call). The design of the first round (change 6, "already exists") treated this fallback as the desired candidates behavior, which is the flaw. The fallback exists for non-person keywords such as "next dentist appointment" where a keyword may be only a loose descriptor; it must not apply to a person filter.

**Root-cause investigation steps (run before fixing, record results in the exploration test notes):**
1. Reproduce with `parse_intent("when do i next see pam?", now)` and print `keywords`, `names`, `filters`, `domain_hints`, and the rows passed to `compute`. Confirm "pam" is in `keywords` and no row hits.
2. In production data (read-only), list calendar rows whose title, description, location, attendees or emails contain "pam", "pamela", "pamel" or "p.". Confirm whether any exist.
3. Check how attendee data reaches rows: `calendar_structured` `fields` and `extra["attendee_names"]`/`extra["attendees"]`. `_hit` reads only `record.title`, `record.snippet` and `fields["payee"]`; verify the snippet contains attendees (first-round change 3) and that the 10-entry cap is not cutting Pam out of large guest lists.
4. Check the contacts store for Pam: name, nickname, full name ("Pamela ..."), emails. If a contact exists, compare its emails with calendar attendee emails to see whether an alias resolves (the event may list only an email such as `pjones@...`).
5. Check cross-domain evidence for "pam": messages (conversations), mail threads, and waiting_on items mentioning Pam and a meeting or visit. Decide whether such evidence changes the answer or only adds context.
6. Confirm `_hit` word matching: `k in words or (len(k) > 4 and k in hay)`. "pam" (3 characters) matches only whole words, so "Pamela", "Pam's" (stem?) and emails like `pam.smith@x` (split on spaces only, not on `.` or `@`) are missed. Test each variant.

**Decision (design):**
- Add a person-versus-descriptor distinction. A leftover keyword is a person filter when it is not a known domain word and either a contact or an attendee token matches it, or the question uses a person verb ("see", "meet", "visit", "meeting with"). For a person filter, no match means no computed answer. For a descriptor keyword (for example "dentist"), keep the current narrowing when something matches and the fallback otherwise, but label the fallback as non-matching.
- Make `_hit` person-aware: tokenise on non-alphanumerics (so emails and possessives split), compare stems, and allow a prefix match for names of at least 3 characters ("pam" matches "pamela", "pamela@..."), plus a nickname table seeded from contacts (`display_name`, `nickname`, `emails`). Matching reads from `StructuredRow.fields["attendees"]` as well as the snippet, so the 10-entry snippet cap does not hide a match.
- `compute` returns a `Computation` marked unmatched (`matched = False`, text "no upcoming event mentions pam", `candidates` listing up to 3 nearest events). The prompt builder renders candidates under a "Not matching <filter>, nearest upcoming:" label and instructs the model not to call a candidate the answer. This replaces first-round change 7, "return the upcoming events as candidates instead of an empty result", and requirement 2.8.
- Cross-domain evidence: when the calendar has no match for a person filter, the retrieval keeps the ordinary ranked records from contacts, messages, mail and waiting_on (they are already retrieved in parallel). The prompt tells the model to use them only if they mention a planned meeting or visit, and otherwise to say none was found. No new adapter is added.

### Defect B: Computed Date Differs from Record Text

**Bug Condition:**
```
FUNCTION isBugConditionDateMismatch(X)
  INPUT: X of type (event: StructuredRow, user_tz: string)
  OUTPUT: boolean

  RETURN dateIn(X.event.start, UTC) != dateIn(X.event.start, X.user_tz)
         AND computedDateText(X.event) = dateIn(X.event.start, UTC)
END FUNCTION
```

**Expected behavior:** `computedDateText(event) = dateIn(event.start, user_tz)`, equal to the date in the record snippet.

**Root cause hypothesis:** `aggregate.compute` formats the answer with `when.date().isoformat()` (line near 306) and `_iso`, where `when` comes from `_ts`/`_due`/`_primary_date`. `_dt` and `_ts` normalise to the `now` timezone only for naive values; aware UTC values keep UTC. Commit bc450082 passes `_user_now(moment)` to `parse_intent` only, so `intent.now` is user-zone but `compute` is called with the raw UTC `moment` (confirm in `retrieve`), and the event timestamp stays in UTC. An 18:30 Pacific event on Nov 18 is 02:30 UTC on Nov 19, which matches the observed 2026-11-19. The record text (adapter snippet) already uses the user zone, hence Nov 18.

**Root-cause investigation steps:**
1. Convert the observed example: 2026-11-18 18:30 America/Los_Angeles equals 2026-11-19 02:30 UTC. Confirm `NERVE_USER_TIMEZONE` in `deployments/kubernetes/base/api-deployment.yaml` (added by bc450082) and that the running pod has it set.
2. Print the `now` and the row timestamps that `retrieve` passes to `compute`, and their `tzinfo`.
3. Grep `aggregate.py` for every `.date().isoformat()`, `_iso(` and `strftime` use (answer text at the `next`, `top_n` and `due` branches, and `_iso` for `value`) and list which carry calendar timestamps.
4. Compare with the adapter helper that formats snippet times in `communications.py` (changed by bc450082) to reuse it.

**Decision:** add a shared helper (for example `user_tz()` in `backend/context/retrieval/_tz.py`, extracted from `service._user_now`, which keeps working as a wrapper) and convert every aware datetime to the user zone inside `aggregate` before formatting, in `_ts`, `_due`, `_primary_date` or at the formatting points. `compute` formats `when` in the user zone and `_iso` returns the user-zone ISO string. Computed values for naive datetimes and when the timezone is unset or invalid stay unchanged. Include the time of day for calendar events ("Wed Nov 18, 18:30") so the computed line and the record match in more than the date.

### Defect C: "I could not search briefing just now"

**Bug Condition:**
```
FUNCTION isBugConditionBriefingNotice(X)
  INPUT: X of type (question: string, retrieval: RetrievalResult)
  OUTPUT: boolean

  RETURN "briefing" NOT IN parse_intent(X.question).domain_hints
         AND "briefing" IN X.retrieval.skipped
END FUNCTION
```

**Root cause hypotheses:** In `backend/context/retrieval/sources/briefing.py`, `briefing()` calls `svc.compose("morning", now=now)`. `compose` is synchronous (`backend/briefing/service.py` line 142), so it runs on the event loop and cannot be interrupted by `asyncio.wait_for(..., ADAPTER_TIMEOUT_S = 3.0)`. A slow compose still blocks the loop, then the timeout fires after the fact (or the exception is raised). `_run` logs `ask.retrieval.adapter_failed` and adds the domain to `skipped`, and `ask.py` (around line 346) appends the notice for every skipped domain regardless of relevance.

**Root-cause investigation steps:**
1. Read the production logs for `ask.retrieval.adapter_failed domain=briefing` and its `error` field (`TimeoutError`, `AttributeError`, `ValidationError`, ...). The log line has only the exception type; also log the message with `exc_info` at debug level to find the cause.
2. Call `GET /api/briefing` against production and time it. If it exceeds 3 seconds, the cause is a timeout (compose reads attention, waiting-on, calendar, email, message and voicemail planes).
3. If the call returns fast, call `BriefingService.compose("morning", now=now)` with the user-zone `now` that Ask passes (`intent.now`) and check for naive/aware comparisons, since `intent.now` is now user-zone from bc450082 while the services may expect UTC. Check for missing fields: `a.due`, `w.days_elapsed`, `e.start`, `brief.overnight`, `brief.generated_at` being `None` on real data.
4. Check whether `state.briefing_service` exists in production wiring (if absent the adapter returns `None`, which is "unavailable", not "skipped"; so an exception is more likely).
5. Check which questions include briefing in the adapter table: the adapters run for every question, which is why an unrelated question can surface the briefing failure.

**Decision:**
1. Run `compose` off the event loop (`await asyncio.to_thread(svc.compose, "morning", now=...)`), with `now` converted to UTC, so a slow compose neither blocks the loop nor ignores the timeout.
2. Make the adapter fail soft: guard field access with defaults, catch exceptions inside the adapter, log the type and message, and return an empty list (searched, no records) instead of raising.
3. In `ask.py`, show the "could not search" notice only for skipped domains that the question asked for (domain in `intent.domain_hints`), or for all skipped domains when the question has no domain hints and nothing was found. Briefing is skipped silently when it has no hint. Skipped domains stay in the retrieval metadata and logs.
4. Do not widen the 3-second timeout for other adapters.

### Follow-up Preservation Requirements

- Keyword filters that match at least one row keep narrowing to those rows (requirement 3.10).
- `next` without a filter stays unchanged (requirement 3.11).
- Computed dates with no `NERVE_USER_TIMEZONE`, or an invalid one, stay unchanged, and non-calendar computed values (bills, tasks) keep their formatting (requirement 3.12).
- Briefing questions with working data still return briefing records, and a failing domain that the question asked for still shows the notice (requirement 3.13).
- The first-round changes (gate exception, attendee snippet, calendar verb hints, capitalised names) are not touched.

### Follow-up Testing Strategy

**Exploratory tests (UNFIXED code, expected to fail):**
1. **A, unrelated fallback:** `compute` over 30 upcoming rows, none mentioning Pam, with `intent` for "when do i next see pam?"; assert no computation names an event and the text says no upcoming event mentions "pam" (fails: names the earliest row).
2. **A, variant matches:** rows with attendee `pamela@x.com`, description "dinner with Pamela", and a contact "Pam" with an alias email; assert each is matched (will fail on at least the email and "Pamela" cases).
3. **B, date mismatch:** event at 2026-11-19 02:30 UTC with `NERVE_USER_TIMEZONE=America/Los_Angeles`; assert the computed text and value show 2026-11-18 and match the snippet date (fails: shows 2026-11-19).
4. **C, briefing notice:** adapter table where `briefing` raises (and one that sleeps past the timeout); assert a question without a briefing hint produces no "could not search briefing" text (fails). Also call the real adapter with a fake `compose` returning missing optional fields and assert it does not raise.

**Fix checking:** the exploratory tests above must pass after the fix, plus a property test over random event times and zones (the computed date equals the user-zone date) and random filter strings absent from all rows (no unmatched event is named).

**Preservation checking:** property tests over random rows and keyword sets: when at least one row matches, the result equals the old result; unfiltered `next` is identical; with the timezone unset, formatting is identical; briefing-hint questions keep the notice.

**Integration tests:** the full `retrieve` plus answer path for the production question with a fake 30-row calendar, a failing briefing adapter and `NERVE_USER_TIMEZONE=America/Los_Angeles` returns no event named as Pam's, no briefing notice, and any candidates labeled as non-matching.
