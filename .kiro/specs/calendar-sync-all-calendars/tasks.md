# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Secondary and shared calendars are not synced
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: Scope the property to the concrete failing case: Google account with no `calendar_ids`, calendarList with a primary entry plus a selected `reader` shared calendar that holds an event on 2026-12-11
  - Create `tests/backend/test_calendar_all_calendars.py` using the scripted `HttpTransport` pattern from `tests/backend/test_calendar_integration.py`
  - Assert `GoogleCalendarProvider.sync` requests events for the shared calendar and returns the "see Pam" event with `extra.calendar_id`
  - Assert `CalendarSyncRunner.sync_account` stores exactly one `calendar_events` row for it and lists both calendars in `sync_state.last_calendars`
  - Add cases for the same event id on two calendars (one row, `extra.calendar_ids` has both) and for a calendarList spread over two pages
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples found (for example "only the primary events request is made; the shared calendar is never queried")
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Override, primary-only, write target, deletion and request shape
  - **IMPORTANT**: Follow observation-first methodology
  - Observe on UNFIXED code: with `calendar_ids` set no calendarList request is made and only those calendars are queried
  - Observe on UNFIXED code: with a primary-only calendarList, rows keep `ical_uid` = Google event id, in-memory id `account_id:ical_uid` and Ask ref `account_id:ical_uid`
  - Observe on UNFIXED code: `_writable_provider` returns `calendar_ids[0]` or `"primary"`
  - Observe on UNFIXED code: cancelled events get the deletion marker, keep the audit row, and leave the in-memory plane
  - Observe on UNFIXED code: event requests carry `singleEvents=true`, `showDeleted=true`, `maxResults=250` and the 14-day back and 60-day forward window for a fixed "now"
  - Write property-based tests (Hypothesis) over generated configured accounts, primary-only accounts and event shapes, asserting those observed outputs
  - Confirm the OAuth scope map in `backend/connectors/registry.py` `EXTRA_SCOPES` is asserted unchanged, and existing CalDAV and Apple tests pass
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

- [x] 3. Fix: Google calendar sync subscribes only to the primary calendar

  - [x] 3.1 Implement the fix in `backend/calendar/providers.py`
    - Add `list_calendar_entries()`: `calendar_ids` override returns those ids with no network call; otherwise page `calendarList` (`minAccessRole=reader`, bounded 10 pages) and apply the filter rule (not deleted, not hidden, role `reader`/`writer`/`owner`, `selected` or primary), primary first
    - Skip holiday and contacts-birthday calendars unless `include_holidays` or `include_birthdays` is true in account config
    - Keep `list_calendars()` returning `list[str]`, derived from the entries
    - In `sync`, tag events with `extra.calendar_id`, `extra.calendar_summary`, `extra.ical_uid_raw`; keep request parameters, window and `MAX_EVENT_PAGES` unchanged
    - Isolate failures on discovered non-primary calendars into `SyncResult.errors`; re-raise for primary and configured calendars
    - Record `SyncResult.truncated` and log a warning when the page cap leaves a `nextPageToken`
    - Dedupe by `(iCalUID or id, start instant)`: active over cancelled, then primary, then configured or calendarList order; fill `extra.calendar_ids`; keep the winner's Google event id as `ical_uid`
    - _Bug_Condition: isBugCondition(input) from design (no calendar_ids, selected readable non-primary calendar with an in-window event)_
    - _Expected_Behavior: Property 1 from design (one row per event with provenance, visible to Ask)_
    - _Preservation: Property 2 from design (override, ids, write target, deletion, request shape, scopes)_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.5, 3.6, 3.7, 3.8_

  - [x] 3.2 Implement sync-state and orphan handling in `backend/calendar/sync.py`
    - Persist `last_calendars` (ids), `last_calendar_meta`, `last_calendar_errors`, `last_truncated` in `sync_account`
    - Mark events of calendars that dropped out of the subscribed set as deleted through the existing `_ingest_both_planes` marker path; never touch the primary or legacy rows without `calendar_id`; skip when the new set is empty or the calendar's fetch failed this pass
    - Leave `_writable_provider`, `write_event`, `delete_event`, `to_ingest_payload`, `event_ical_id` unchanged
    - No Alembic migration
    - _Bug_Condition: isBugCondition(input) from design_
    - _Expected_Behavior: Requirements 2.3, 2.5, 2.6, 2.7 from design_
    - _Preservation: write target and deletion markers from design_
    - _Requirements: 2.3, 2.5, 2.6, 2.7, 3.3, 3.4, 3.9_

  - [x] 3.3 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - All selected readable calendars are synced
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior
    - When this test passes, it confirms the expected behavior is satisfied
    - Run bug condition exploration test from task 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [x] 3.4 Verify preservation tests still pass
    - **Property 2: Preservation** - Override, primary-only, write target, deletion and request shape
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run preservation property tests from task 2, plus the existing `tests/backend/test_calendar_integration.py` and `tests/backend/test_ask_calendar_create.py`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all tests still pass after fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

  - [x] 3.5 Add remaining unit and integration tests
    - Filter matrix, dedupe priority, error isolation, truncation flag, calendarList paging, orphan handling (see design Unit Tests)
    - Integration: `sync_account` over a fake registry, then Ask calendar retrieval returns the Dec 11 event with ref `account_id:ical_uid`
    - _Requirements: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7_

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [~] 5. Deploy and verify in production (non-code)
  - Deploy the backend through the normal release process
  - Trigger a resync for both Google accounts with `POST /api/calendar/accounts/{account_id}/sync` (celes@frameshift.net and celes@celestium.life)
  - Read-only checks: `sync_state.last_calendars` now lists more than the primary address for the account that owns the shared calendar; `calendar_events` has a row on 2026-12-11 matching "pam" with `extra.calendar_id` set; `last_calendar_errors` and `last_truncated` are empty or explained
  - Ask "when do I see Pam?" and confirm the answer cites the Dec 11 event
  - If the event is still missing, check whether its calendar is unselected, hidden, a `freeBusyReader`, or outside the 14-day back and 60-day forward window, and report back instead of changing config
  - _Requirements: 2.1, 2.2, 2.3_
