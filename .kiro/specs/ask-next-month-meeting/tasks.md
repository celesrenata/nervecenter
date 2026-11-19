# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - In-window calendar events survive term gating
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: Scope the property to the concrete failing cases: now = 2026-06-03 12:00 UTC, calendar records dated in July 2026, questions containing "next month" plus a filler keyword ("planned"), a name ("Bob") or a title-mismatch keyword ("dentist")
  - Add the test in `tests/backend/test_ask_next_month_meeting.py`, using `parse_intent`, `score_record`, `rank` and `Record` (import `backend.api` first, as in `tests/backend/test_ask_ranker_gate.py`)
  - Assert `isBugCondition` inputs get `score_record > 0`, appear in `rank(...)`, and score below a record with a real term hit
  - Add an adapter case: fake `state.calendar_service` returning rows with `extra["attendees"]`, a recurring instance and an all-day event; assert attendee names/emails appear in the record snippet and the all-day row shows "(all day)" on the right date
  - Also cover the `next`-operator variant (now = 2026-06-03): "when do i next see pam?" with (a) an upcoming event whose attendee is Pam and whose title does not contain Pam, expect that event cited with its date; (b) an event titled "Lunch with Pam", expect that event cited with its date; (c) upcoming events not involving Pam, expect candidates listed rather than "found nothing". Also assert `parse_intent` yields a "calendar" domain hint for "see", "meet", "meeting with", "appointment" and "visit" wording with a next operator
  - Run on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples found (e.g., `score_record(Standup, "do I have anything planned next month?") == 0.0`; `parse_intent("when do i next see pam?")` has empty domain_hints and the answer is "found nothing about pam")
  - Mark task complete when the test is written, run, and the failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Non-bug inputs score identically
  - **IMPORTANT**: Follow observation-first methodology
  - Observe on UNFIXED code: `score_record` is 0.0 for a bill/mail/task record with a window and zero term hits; 0.0 for an out-of-window calendar record with unmatched terms; "what is my next appointment?" parses `next` with no window; "this week", "next week", "last month", "this month", "tomorrow", "today" produce their current windows; "what meetings do I have next month?" (no terms) returns in-window events
  - Write property-based tests (Hypothesis, as used in `tests/backend/test_ask_answer_coverage_preservation.py`) that capture a snapshot reference scorer copied from the current `score_record` and assert equality for all generated inputs where `isBugCondition` is false (varied domains, windows, kinds, term sets, in/out-of-window timestamps)
  - Add example tests for cancelled/deleted exclusion, the no-window 30-days-back to 180-days-ahead range, and count questions over typed calendar rows
  - Observe on UNFIXED code: the `next` operator for bills and other non-calendar domains (for example "when is my next bill due?") parses to the same intent and result, with no calendar hint and no person filter; capitalised names ("Pam", "Bob") parse as names. Add property/example tests that capture this
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

- [x] 3. Fix for next-month meeting not found in Ask

  - [x] 3.1 Implement the fix
    - In `backend/context/retrieval/ranker.py` `score_record`: when `hits == 0` at the keyword gate, keep the record if `intent.window` is set, the record is in the window, `record.domain == "calendar"` and "calendar" is in `intent.domain_hints`; give it a term score of 0.25 so it ranks below any record with a term hit
    - In `backend/context/retrieval/sources/communications.py` `calendar` and `calendar_structured`: add a helper that appends attendee names and emails (from `extra["attendee_names"]` and `extra["attendees"]`, capped at about 10) to the snippet; leave the snippet unchanged when absent
    - Do not change `parse_window` or `_NEXT_NOT_WINDOW`
    - If task 1 shows recurring or all-day rows dropped or mis-dated in the adapter, fix only that confirmed gap
    - In `backend/context/retrieval/intent.py`: add calendar domain hints for "see", "meet", "meeting with", "appointment" and "visit" when the operator is next/earliest/latest or a window is present; with a next/earliest/latest operator and a calendar hint, record leftover keywords (not names, stop words or domain words) as a person/title filter
    - In the operator path over `calendar_structured`: match the filter case-insensitively against title, snippet and attendees, return the earliest match with its date, and fall back to listing upcoming events as candidates when nothing matches
    - Reuse the attendee snippet helper so attendee-only events (title without "Pam") match
    - Leave bills and other-domain `next` handling and capitalised-name parsing unchanged
    - Deployment check (not a code task): confirm the running server build matches the current source, since the user's reply listed an older domain list (calendar ... wellness)
    - _Bug_Condition: isBugCondition(input) and isBugConditionNextPerson(input) from design_
    - _Expected_Behavior: expectedBehavior(result) from design_
    - _Preservation: Preservation Requirements from design_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - In-window calendar events survive term gating
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior
    - When this test passes, it confirms the expected behavior is satisfied
    - Run the bug condition exploration test from task 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed), including the "when do i next see pam?" cases
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9_

  - [x] 3.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-bug inputs score identically
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run the preservation property tests from task 2
    - Also run `tests/backend/test_ask_ranker_gate.py` and `tests/backend/test_ask_answer_coverage_preservation.py`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Follow-up Round (deployed tag ask-cov-20261118-1048)

Tasks 1 to 4 above are complete and unchanged. Tasks 5 to 10 cover the three defects found in production for "when do I next see pam?". Write and run the exploration tests (5, 6, 7) on the UNFIXED code before any fix, and write the preservation tests (8) against the UNFIXED code first.

- [x] 5. Write bug condition exploration test for the unrelated-event fallback (defect A)
  - **Property 3: Bug Condition** - No unrelated event as the answer
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: Scope to "when do i next see pam?" over upcoming calendar rows with no mention of Pam (for example 30 rows, the earliest titled "Family Video Call"), plus Hypothesis-generated filter strings absent from every row
  - Add the test in `tests/backend/test_ask_next_month_meeting.py` (or a new `tests/backend/test_ask_next_filter_followup.py`), importing `backend.api` first as in `tests/backend/test_ask_ranker_gate.py`
  - Run the root-cause investigation steps A1 to A6 from the design first: print `parse_intent` fields and the rows passed to `compute`; confirm whether Pam exists in contacts, attendee emails, descriptions, or as "Pamela"; record the findings as comments in the test file
  - Assert `aggregate.compute` (and `retrieve` end to end with fake adapters) does not name an unmatched event as the answer, states that no upcoming event mentions "pam", and labels any listed candidates as not matching
  - Add variant cases through the filter: attendee email `pamela@example.com`, description "dinner with Pamela", and a contact "Pam" whose alias email is the only attendee entry; assert each is matched
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples (for example `compute` returns "The next event is Family Video Call on 2026-11-19" because `_apply_filters` keeps all rows when none match)
  - Mark task complete when the test is written, run, and the failure is documented
  - _Requirements: 1.9, 1.10_

- [x] 6. Write bug condition exploration test for the computed date mismatch (defect B)
  - **Property 4: Bug Condition** - Computed dates use the user timezone
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: Scope to event starts whose UTC date differs from the user-zone date, with `NERVE_USER_TIMEZONE=America/Los_Angeles` (for example 2026-11-19 02:30 UTC, shown as Wed Nov 18 18:30), plus Hypothesis-generated start times and IANA zones
  - Run investigation steps B1 to B4 from the design first: confirm the environment variable in the deployment manifest, print `now` and row timestamps passed to `compute`, list every `.date().isoformat()` and `_iso(` use in `aggregate.py`, and record the findings as comments in the test
  - Assert the computed text and `value` show the user-zone date and equal the date in the record snippet produced by the calendar adapter
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples (for example computed "2026-11-19" while the record says "Wed Nov 18, 18:30-19:15")
  - Mark task complete when the test is written, run, and the failure is documented
  - _Requirements: 1.11_

- [x] 7. Write bug condition exploration test for the briefing failure notice (defect C)
  - **Property 5: Bug Condition** - Briefing failure does not surface when irrelevant
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: Scope to questions without a briefing hint (for example "when do i next see pam?") with a briefing adapter that (a) raises, (b) exceeds the adapter timeout, and a real `briefing` adapter run against a fake `compose` that returns missing optional fields (`due`, `days_elapsed`, `overnight`, `generated_at`)
  - Run investigation steps C1 to C5 from the design first: read the production `ask.retrieval.adapter_failed domain=briefing` log, time `GET /api/briefing`, check `intent.now` timezone against `compose`, check `state.briefing_service` wiring, and record the findings as comments in the test
  - Assert the final answer has no "could not search briefing" text, that the real adapter does not raise on the missing-field data, and that a synchronous slow `compose` does not block other adapters past the timeout
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples (for example the answer ends with "I could not search briefing just now." on a calendar-only question)
  - Mark task complete when the test is written, run, and the failure is documented
  - _Requirements: 1.12_

- [x] 8. Write preservation property tests for the follow-up round (BEFORE implementing fix)
  - **Property 6: Preservation** - Filtered and unfiltered `next`, dates and notices
  - **IMPORTANT**: Follow observation-first methodology
  - Observe on UNFIXED code: a keyword that matches at least one row (for example "next dentist appointment") narrows `compute` to matching rows; "what is my next appointment?" computes over all upcoming rows with no "no match" text; with `NERVE_USER_TIMEZONE` unset or invalid, computed dates are formatted as before; bill and task computed dates are unchanged; "what needs my attention?" returns briefing records; a failing domain that the question asked for (for example a bills question with `bills` skipped) still yields the "could not search" notice
  - Write property-based tests (Hypothesis) capturing those results for random rows, keyword sets that match at least one row, random times with the timezone unset, and random domain hints
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.10, 3.11, 3.12, 3.13_

- [ ] 9. Fix for the follow-up defects: unrelated fallback, date mismatch, briefing notice

  - [x] 9.1 Fix defect A: no unrelated event as the answer
    - In `backend/context/retrieval/aggregate.py` `_apply_filters`: for a person filter, return no rows when nothing matches instead of keeping all rows; keep narrowing when something matches; for a descriptor keyword keep the old fallback but mark the result as non-matching
    - Make the match person-aware: tokenise on non-alphanumerics (emails, possessives), allow a prefix match for names of at least 3 characters ("pam" matches "pamela"), read `fields["attendees"]` as well as the snippet, and use contact name, nickname and email aliases when available
    - In `compute`, return an unmatched `Computation` ("no upcoming event mentions pam") with up to 3 nearest upcoming events as labeled candidates; update the prompt builder so candidates are rendered under a "Not matching <filter>" label and are never called the answer
    - Keep cross-domain records (contacts, messages, mail, waiting_on) in the evidence and instruct the model to cite them only if they mention a planned meeting or visit
    - Supersedes the unlabeled fallback of first-round requirement 2.8 and change 7
    - _Bug_Condition: isBugConditionUnrelatedFallback(input) from design_
    - _Expected_Behavior: expectedBehaviorNoMatch(result) from design_
    - _Preservation: follow-up Preservation Requirements from design_
    - _Requirements: 2.8, 2.10, 2.11, 2.12, 2.13, 3.10, 3.11_

  - [x] 9.2 Fix defect B: user-timezone dates in computed answers
    - Extract the timezone lookup from `service._user_now` into a shared helper (keep `_user_now` as a wrapper) and convert aware datetimes to the user zone in `aggregate` before formatting (`_ts`, `_due`, `_primary_date`, the `next` answer text and `_iso`)
    - Include the time of day for calendar events so the computed text matches the record text
    - Leave naive datetimes and the unset or invalid timezone case unchanged
    - _Bug_Condition: isBugConditionDateMismatch(input) from design_
    - _Expected_Behavior: computed date equals user-timezone date from design_
    - _Preservation: follow-up Preservation Requirements from design_
    - _Requirements: 2.14, 3.12_

  - [x] 9.3 Fix defect C: briefing adapter and notice
    - In `backend/context/retrieval/sources/briefing.py`: run `svc.compose` with `asyncio.to_thread`, pass a UTC `now`, guard optional fields with defaults, catch and log exceptions (type and message) inside the adapter, and return an empty list instead of raising
    - In `backend/context/ask.py` (notice near line 346): show "I could not search ..." only for skipped domains that the question asked for (domain in `intent.domain_hints`), and not for an irrelevant domain such as briefing; keep skipped domains in retrieval metadata and logs
    - Decision recorded in design: fix the adapter and make the notice relevance-aware; do not raise the 3-second adapter timeout
    - _Bug_Condition: isBugConditionBriefingNotice(input) from design_
    - _Expected_Behavior: no "could not search briefing" text without a briefing hint from design_
    - _Preservation: follow-up Preservation Requirements from design_
    - _Requirements: 2.15, 3.13_

  - [x] 9.4 Verify bug condition exploration tests now pass
    - **Property 3: Expected Behavior** - No unrelated event as the answer
    - **Property 4: Expected Behavior** - Computed dates use the user timezone
    - **Property 5: Expected Behavior** - Briefing failure does not surface when irrelevant
    - **IMPORTANT**: Re-run the SAME tests from tasks 5, 6 and 7 - do NOT write new tests
    - The tests from tasks 5, 6 and 7 encode the expected behavior
    - **EXPECTED OUTCOME**: Tests PASS (confirms the bugs are fixed)
    - _Requirements: 2.8, 2.10, 2.11, 2.12, 2.13, 2.14, 2.15_

  - [x] 9.5 Verify preservation tests still pass
    - **Property 6: Preservation** - Filtered and unfiltered `next`, dates and notices
    - **IMPORTANT**: Re-run the SAME tests from task 8 - do NOT write new tests
    - Also run the tests from tasks 1 and 2, `tests/backend/test_ask_ranker_gate.py`, `tests/backend/test_ask_answer_coverage_preservation.py` and `tests/backend/test_ask_retrieval_windows.py`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)

  - [~] 9.6 Verify end to end after deploy
    - Deployment check (not a code task): confirm the new build is running and `NERVE_USER_TIMEZONE` is set in the pod
    - Ask "when do i next see pam?" in production: expect either a Pam event with a user-timezone date and time, or a statement that no upcoming event mentions "pam" with labeled candidates, and no "could not search briefing" notice
    - _Requirements: 2.10, 2.11, 2.14, 2.15_

- [~] 10. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
