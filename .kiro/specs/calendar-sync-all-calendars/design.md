# Calendar Sync All Calendars Bugfix Design

## Overview

`GoogleCalendarProvider.list_calendars` returns only the primary calendar when `config.calendar_ids` is unset. Events on shared and secondary calendars are never fetched, so Ask cannot find them (production example: "see Pam on December 11th").

The fix makes the default subscription set "every readable calendar the user has selected or shown". Each event records its source calendar in `extra`. Events present on several calendars collapse to one row without changing existing ids. The change stays inside the provider and sync runner. Ask's calendar retrieval (`backend/context/retrieval/sources/communications.py`, `calendar` and `calendar_structured`) already reads every non-deleted `calendar_events` row through `registry.calendar.list_events`, so it needs no change. There is no migration, since provenance goes in the JSONB `extra` column.

## Glossary

- **Bug_Condition (C)**: A Google account without `config.calendar_ids` whose calendarList has a selected, readable, non-primary calendar that carries an event in the sync window
- **Property (P)**: That event is stored as exactly one `calendar_events` row with calendar provenance, and is visible to Ask
- **Preservation**: Primary-only behavior under `calendar_ids`, existing ids and citations, write target, deletion markers, `singleEvents` expansion, window, page size, OAuth scopes
- **list_calendars**: `GoogleCalendarProvider.list_calendars` in `backend/calendar/providers.py` (about line 284); chooses the calendars to fetch
- **sync (provider)**: `GoogleCalendarProvider.sync`; loops over calendars and pages (`MAX_EVENT_PAGES = 20`, `maxResults=250`)
- **sync_account**: `CalendarSyncRunner.sync_account` in `backend/calendar/sync.py`; ingests events and stores `sync_state.last_calendars` (about line 283)
- **_writable_provider**: `sync.py` (about line 343); picks the write target `calendar_ids[0]` or `"primary"`
- **calendarList entry**: Google resource with `id`, `summary`, `primary`, `selected`, `hidden`, `deleted`, `accessRole`

## Bug Details

### Bug Condition

The bug manifests when no `calendar_ids` override exists and the account has at least one calendar besides the primary one that passes the user-visible filters. `list_calendars` keeps only `c.get("primary") is True`, so those calendars are never queried.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type { account: CalendarAccount, calendarList: list<CalendarListEntry>, event: GoogleEvent, window: TimeWindow }
  OUTPUT: boolean

  cal := calendarOf(input.event)
  RETURN input.account.provider = "google"
         AND input.account.config.calendar_ids IS EMPTY
         AND cal.primary != true
         AND cal.deleted != true AND cal.hidden != true
         AND (cal.selected = true)
         AND cal.accessRole IN ["reader", "writer", "owner"]
         AND eventInWindow(input.event, input.window)
         AND NOT storedInCalendarEvents(input.account, input.event)
END FUNCTION
```

### Examples

- Account celes@frameshift.net has a shared calendar "Pam" (selected, `reader`) with "see Pam on December 11th". Expected: one row dated 2026-12-11 and Ask answers it. Actual: no row.
- The same invite sits on both primary and a shared calendar with the same Google event id. Expected: one row, `extra.calendar_ids` lists both. Actual (naive multi-calendar fix): the second upsert overwrites the first, and a cancelled copy can mark the active event deleted.
- `config.calendar_ids = ["x@group.calendar.google.com"]`. Expected: only that calendar, no calendarList request. This already works and must not change.
- A calendar with `selected=false` or `accessRole=freeBusyReader`. Expected: not synced.
- A US holiday calendar and the contacts birthday calendar. Expected: skipped by default (see Design Decisions).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `calendar_ids` set means exactly those calendars, in order, with no calendarList call
- Primary-calendar events keep `ical_uid` = Google event id, in-memory id `account_id:ical_uid`, and Ask ref `account_id:ical_uid`
- Write, delete, search and instance listing use `calendar_ids[0]` else `primary`
- Cancelled events get the deletion marker, the durable audit row stays, and the in-memory copy is removed
- Requests keep `singleEvents=true`, `showDeleted=true`, `maxResults=250`, and the 14-day back and 60-day forward window
- OAuth scopes are unchanged
- CalDAV and Apple providers are untouched

**Scope:**
Everything outside the Google default-subscription path is unaffected: configured accounts, CalDAV, Apple, the write path, and the Ask retrieval code.

### OAuth scope check

`backend/connectors/registry.py` `EXTRA_SCOPES` maps `("google","calendar")` to `calendar.readonly` and `("google","calendar_write")` to `calendar.events`. `calendarList.list` is permitted under `calendar.readonly`, and production already calls it successfully to find the primary calendar (both accounts sync healthy). Reading secondary-calendar events uses `events.list`, covered by the same scope. **No scope change is needed.** A grant with only `calendar.events` would fail calendarList with 403, which `_get_json` already maps to `upstream_error`. That is existing behavior and requirement 2.8 keeps it.

### Design Decisions

1. **Filter rule** (unset `calendar_ids`): include an entry when `deleted` is not true, `hidden` is not true, `accessRole` is `reader`, `writer` or `owner`, and (`selected` is true or `primary` is true). Request `calendarList` with `minAccessRole=reader` and default `showHidden=false`/`showDeleted=false`, and re-check the flags client-side because the API default and fixtures can differ. `freeBusyReader` is excluded because event details are not readable. The primary calendar is always included even if `selected` is missing.
2. **Holiday and birthday calendars**: excluded by default. Holiday calendars (id ending `#holiday@group.v.calendar.google.com`) and the contacts birthday calendar (`addressbook#contacts@group.v.calendar.google.com`) produce many all-day rows that would crowd Ask's calendar retrieval cap and add little. Optional account config `include_holidays` and `include_birthdays` (default false) opt in. A user-created "Birthdays" calendar with an ordinary id is included, because it is a normal selected calendar.
3. **Provenance storage**: `ProviderEvent.extra` gains `calendar_id`, `calendar_summary` and, after dedupe, `calendar_ids`. No schema change.
4. **Identity**: `ical_uid` stays the Google event id. Dedupe happens inside one pass on key `(iCalUID or id, instance start instant)`. `iCalUID` from the payload is saved in `extra.ical_uid_raw`. Priority: active over cancelled, then primary, then configured or calendarList order. The winner keeps its Google event id, so primary rows keep their current ids and citations. A non-primary event keeps its plain Google id because Google event ids rarely collide across unrelated calendars, and a true collision is the same event by definition. If an event moves from secondary-only to also-on-primary, the row (same id) is updated in place.
5. **Write path unchanged**: `_writable_provider` keeps `calendar_ids[0]` else `"primary"`. `delete_event` for a synced non-primary event returns False (404 treated as gone), as it does today for any event not on the write target. Ask only deletes events it created on the write target.
6. **Per-calendar failure isolation**: failures on discovered non-primary calendars are recorded and skipped. Failures on the primary or configured calendars still raise, as today.
7. **Orphan handling**: after a pass, calendars in `previous last_calendars` but absent from the new subscribed set, when the new set is non-empty and the primary was fetched, have their events marked deleted via the existing marker. The runner finds them by `extra.calendar_id` in the dropped set, skipping the primary. Rows without `calendar_id` (legacy) are treated as primary and never tombstoned. If any calendar fetch failed this pass, that calendar is not treated as dropped.
8. **Calendar list result shape**: `list_calendars()` keeps returning `list[str]` for compatibility. A new `list_calendar_entries()` returns the entries with metadata, and `list_calendars()` derives ids from it. Calendar metadata is cached on the provider instance for the pass.

### Sync window and pagination assessment

- **Window**: 14 days back and 60 days forward from "now". The 2026-12-11 event is 23 days ahead and inside the window. Events more than 60 days ahead are not synced until they come into range. This is unchanged by design (preservation 3.6). Ask can answer questions only about events within the synced window, which is a separate limitation. If the user wants longer lookahead, widen `DEFAULT_FORE_WINDOW` in a follow-up.
- **Pagination**: the loop follows `nextPageToken` correctly up to `MAX_EVENT_PAGES = 20`, which is 5,000 events per calendar per pass. Pages can be shorter than 250 while a token is present, and the loop handles that. When the cap is hit, the loop falls out silently, so events are dropped without any signal. Production risk is low today (43 rows), but adding many calendars with `singleEvents=true` (a daily recurring event expands to about 74 rows in this window) raises it. The fix records `last_truncated` and logs a warning. It does not raise the cap.
- **calendarList paging**: `list_calendars` reads one page (Google's default is 100 entries) and ignores `nextPageToken`. The fix follows it, bounded at 10 pages.
- **timeMin/timeMax semantics**: Google returns events whose end is after `timeMin` and start is before `timeMax`, so events already in progress are included. `showDeleted=true` returns cancelled instances inside the same window.

## Hypothesized Root Cause

1. **Primary-only default**: `list_calendars` ends with `return [primary]`. This is the confirmed cause, and the module docstring promises multi-calendar support only through `config.calendar_ids`.
2. **No provenance column or key**: `(account_id, ical_uid)` is the upsert key and nothing records the source calendar, so a naive multi-calendar fix would overwrite or mask duplicate events.
3. **Silent truncation and single-page calendarList**: pagination limits give no signal, so related drops would go unnoticed.
4. **Atomic failure**: one failing calendar currently aborts the whole account pass, so adding secondary calendars without isolation could make a healthy primary sync fail.

## Correctness Properties

Property 1: Bug Condition - All selected readable calendars are synced

_For any_ Google account without `config.calendar_ids` and any event on a selected, non-hidden, non-deleted, readable (`reader`, `writer`, `owner`) calendar inside the sync window, the fixed sync SHALL store exactly one `calendar_events` row for that event with `extra.calendar_id` set to its calendar, and the row SHALL be returned by Ask's calendar retrieval. Calendars that are unselected, hidden, deleted, `freeBusyReader`, or holiday/contacts-birthday (unless opted in) SHALL NOT be synced.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7**

Property 2: Preservation - Primary-only and override behavior

_For any_ input where the bug condition does NOT hold (`calendar_ids` set, or an account whose only readable calendar is the primary one, or any CalDAV or Apple account), the fixed code SHALL produce the same subscribed calendars, the same event rows (`ical_uid`, in-memory id, Ask ref), the same write target, the same deletion markers, the same request parameters (`singleEvents`, `showDeleted`, `maxResults`, window) and the same OAuth scopes as the original code.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9**

## Fix Implementation

### Changes Required

**File**: `backend/calendar/providers.py`

**Function**: `GoogleCalendarProvider.list_calendars`, new `list_calendar_entries`, `sync`

**Specific Changes**:
1. **Discovery**: add `list_calendar_entries()`. If `config.calendar_ids` is set, return those ids as entries with no network call. Otherwise page through `calendarList` (`minAccessRole=reader`, bounded 10 pages) and apply the filter rule and holiday/birthday exclusion. Put the primary first. `list_calendars()` returns the ids.
2. **Per-calendar fetch**: in `sync`, tag each normalized event with `extra.calendar_id` and `extra.calendar_summary`, keep `extra.ical_uid_raw`. Wrap non-primary discovered calendars in try/except `TransportError` and record to `SyncResult.errors`. Re-raise for primary or configured calendars.
3. **Dedupe**: after the loop, collapse events by `(iCalUID or id, start instant)` using the priority in Design Decision 4 and fill `extra.calendar_ids`.
4. **Truncation**: when the page loop exhausts `MAX_EVENT_PAGES` with a token pending, add the calendar id to `SyncResult.truncated` and log a warning.
5. **Result shape**: add `errors: dict[str, str]` and `truncated: list[str]` to `SyncResult` with empty defaults, so CalDAV and Apple are unaffected. Leave the request parameters, window and `MAX_EVENT_PAGES` untouched.
6. **`_normalize`**: also add `iCalUID` as `extra.ical_uid_raw`. No other field changes.

**File**: `backend/calendar/sync.py`

**Function**: `CalendarSyncRunner.sync_account`

**Specific Changes**:
1. **State**: persist `last_calendars` (ids, as today), plus `last_calendar_meta`, `last_calendar_errors` and `last_truncated` from the result.
2. **Orphans**: after ingest, compute dropped calendars from the previous `last_calendars` and the guards in Design Decision 7. Mark the durable rows deleted with the existing `_ingest_both_planes` path (`deleted=True`, `status="cancelled"`). If a repository helper to find rows by `extra.calendar_id` is missing, add a small read to `CalendarRepository` (`list_events(account_id=...)` filtered in Python is acceptable at this scale).
3. **No changes** to `_writable_provider`, `write_event`, `delete_event`, `to_ingest_payload`, `event_ical_id`, `upsert_event`, or any Ask retrieval code.

**Files not changed**: `backend/connectors/registry.py` (scopes), the Alembic versions, `backend/context/retrieval/sources/communications.py`.

## Testing Strategy

### Validation Approach

Test through the injectable `HttpTransport` fake (no network). Existing fixtures in `tests/backend/test_calendar_integration.py` (`_google_calendar_list_response`, `_google_items_response`, the scripted transport) provide the pattern. New tests go in `tests/backend/test_calendar_all_calendars.py`. Property tests use Hypothesis. Run tests on the unfixed code first.

### Exploratory Bug Condition Checking

**Goal**: Show on unfixed code that secondary-calendar events are not fetched, and confirm the root cause is `list_calendars`.

**Test Plan**: Script calendarList with a primary entry and a selected `reader` shared calendar, script events for both, run `GoogleCalendarProvider.sync` and `CalendarSyncRunner.sync_account`, and assert both events are returned and stored.

**Test Cases**:
1. **Shared calendar event**: "see Pam" on 2026-12-11 on a selected shared calendar. Assert a row exists with `extra.calendar_id` (fails on unfixed code)
2. **calendarList paging**: 101 entries across two pages. Assert the second page's calendar is synced (fails on unfixed code)
3. **Provenance state**: assert `last_calendars` lists every subscribed calendar (fails on unfixed code)
4. **Duplicate event**: the same event id on primary and a shared calendar. Assert exactly one row with both calendar ids (fails on unfixed code, which never sees the second copy)

**Expected Counterexamples**:
- Only the primary calendar's events are returned, and the events request for the shared calendar is never made
- Cause: `list_calendars` returns `[primary]`

### Fix Checking

**Goal**: For every input where the bug condition holds, the fixed sync stores the event once with provenance.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  rows := sync_account'(input)
  ASSERT count(rows WHERE ical_uid = input.event.id) = 1
  ASSERT rows[0].extra.calendar_id IN subscribedCalendars(input.calendarList)
  ASSERT askCalendarRetrieval(rows) CONTAINS input.event
END FOR
```

### Preservation Checking

**Goal**: For all inputs where the bug condition does not hold, behavior matches the original code.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT sync_original(input).rows = sync_fixed(input).rows
  ASSERT sync_original(input).requests = sync_fixed(input).requests
END FOR
```

**Testing Approach**: Property-based testing, because the non-bug input space (configured ids, primary-only accounts, many event shapes) is large. Record the original provider's request sequence and rows on unfixed code, then assert equality after the fix.

**Test Cases**:
1. **calendar_ids override**: with `calendar_ids` set, no calendarList request, the same events requests and rows
2. **Primary-only account**: a calendarList containing only the primary entry produces the same rows, ids and Ask refs
3. **Write target**: `_writable_provider` returns `calendar_ids[0]` or `"primary"` regardless of subscribed calendars
4. **Cancelled events**: deletion marker, audit row kept, in-memory copy removed
5. **Request parameters**: `singleEvents=true`, `showDeleted=true`, `maxResults=250`, and the timeMin/timeMax values for a fixed "now"
6. **CalDAV and Apple**: existing tests unchanged

### Unit Tests

- Filter matrix: `selected`, `hidden`, `deleted`, each `accessRole`, primary without `selected`, holiday and contacts-birthday ids with and without opt-in
- Dedupe priority: primary over secondary, active over cancelled, stable under input order
- Per-calendar error isolation: a 403 on a secondary calendar is recorded and the pass stays healthy; a primary failure still raises
- Truncation flag when page cap is hit; calendarList page cap
- Orphan handling: a dropped calendar's events are tombstoned; an empty calendarList or a failed fetch tombstones nothing; legacy rows without `calendar_id` are untouched

### Property-Based Tests

- Generate calendarList entries with random flags and roles, and assert the subscribed set equals the filter rule and the primary is always first
- Generate event sets spread over random calendars with overlaps, and assert one row per `(iCalUID, start)` and no lost events
- Generate non-bug inputs and assert request and row equality with the original behavior

### Integration Tests

- Run `sync_account` over a fake registry and assert `calendar_events` rows, `last_calendars`, `last_calendar_meta`, and that `CalendarService` contains the secondary event
- Call the Ask calendar retrieval over those rows and assert the Dec 11 event is returned with ref `account_id:ical_uid`
- Resync after unselecting the shared calendar and assert its events are marked deleted and no longer cited
