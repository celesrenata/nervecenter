# Bugfix Requirements Document

## Introduction

Google calendar sync subscribes only to each account's primary calendar. `GoogleCalendarProvider.list_calendars` in `backend/calendar/providers.py` reads `users/me/calendarList` and returns only the entry marked `primary`, unless `config.calendar_ids` is set. Neither production Google account (celes@frameshift.net, celes@celestium.life) sets it. Events on shared or secondary calendars never reach `calendar_events`, so Ask cannot see them. The user's "see Pam on December 11th" event is one such event: production has 43 rows, none on 2026-12-11 and none matching "pam".

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a Google account has no `config.calendar_ids` and its calendarList contains calendars other than the primary one that the user has selected or shown THEN the system syncs only the primary calendar and never fetches events from the other calendars

1.2 WHEN an event exists only on a shared or secondary calendar (for example "see Pam on December 11th", inside the 14-day back and 60-day forward sync window) THEN the system stores no `calendar_events` row for it and Ask cannot find or cite it

1.3 WHEN events are stored from a synced calendar THEN the system does not record which calendar each event came from, and `sync_state.last_calendars` lists only the primary calendar

1.4 WHEN the same event appears on more than one subscribed calendar of one account THEN the system has no rule for it. The `(account_id, ical_uid)` upsert key would let one calendar's copy overwrite the other's, and a cancelled copy could mask an active one

1.5 WHEN a calendar's event paging reaches `MAX_EVENT_PAGES` (20 pages of 250) while Google still returns `nextPageToken`, or when `calendarList` returns a `nextPageToken` THEN the system silently drops the remaining events or calendars and still reports a healthy sync

### Expected Behavior (Correct)

2.1 WHEN a Google account has no `config.calendar_ids` THEN the system SHALL subscribe to every calendarList entry that is not `deleted`, not `hidden`, is `selected` (or is the primary calendar), and has `accessRole` of `reader`, `writer` or `owner`. This includes shared and secondary calendars. It SHALL follow `calendarList` pagination.

2.2 WHEN an event exists on any subscribed calendar inside the sync window THEN the system SHALL store it in `calendar_events` so that Ask's calendar retrieval returns it, and the December 11 "see Pam" event SHALL appear after a resync

2.3 WHEN an event is synced THEN the system SHALL record its source calendar id and display name in `extra` (`calendar_id`, `calendar_summary`, and `calendar_ids` when several calendars carry it), and SHALL store the full subscribed calendar ids in `sync_state.last_calendars`

2.4 WHEN the same event appears on more than one subscribed calendar of one account THEN the system SHALL produce exactly one row for it. The primary calendar's copy SHALL win, then the order of `calendar_ids` or calendarList. An active copy SHALL win over a cancelled copy. All carrying calendars SHALL be listed in `extra.calendar_ids`.

2.5 WHEN event paging or calendarList paging stops because of a page cap with a `nextPageToken` still pending THEN the system SHALL record the truncation in `sync_state` (`last_truncated`) and log a warning, instead of dropping events silently

2.6 WHEN a non-primary calendar discovered from calendarList fails to fetch (for example 403 or 404) THEN the system SHALL skip that calendar, record the error in `sync_state` (`last_calendar_errors`), and still sync the remaining calendars and report the account healthy. A failure on the primary or an explicitly configured calendar SHALL still fail the pass as today.

2.7 WHEN a previously synced non-primary calendar is no longer subscribed (unselected, hidden, deleted or access lost) and calendarList returned a non-empty result THEN the system SHALL mark that calendar's events with the existing deletion marker so Ask stops citing them

2.8 WHEN calendarList cannot be read for lack of OAuth scope THEN the system SHALL surface a stable error code on the account (as `upstream_error` does today). The current `calendar.readonly` scope can read calendarList, so no scope change is needed.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `config.calendar_ids` is set THEN the system SHALL CONTINUE TO subscribe to exactly those calendars, skip the calendarList request, and apply none of the selected, hidden, deleted or accessRole filters

3.2 WHEN an event is stored from the primary calendar, or from an account that has only a primary calendar THEN the system SHALL CONTINUE TO use the same `ical_uid` (Google event id), the same in-memory id `account_id:ical_uid`, and the same Ask citation ref `account_id:ical_uid` as before

3.3 WHEN Ask creates or deletes a calendar event THEN the system SHALL CONTINUE TO write to `calendar_ids[0]` if set, else `primary`, and SHALL CONTINUE TO use that same target for search and instance listing in the delete flow

3.4 WHEN Google reports a cancelled event THEN the system SHALL CONTINUE TO set the deletion marker, keep the durable audit row, and remove the in-memory copy

3.5 WHEN recurring events are fetched THEN the system SHALL CONTINUE TO request `singleEvents=true` and `showDeleted=true`, so each occurrence is stored as its own row

3.6 WHEN a sync pass runs THEN the system SHALL CONTINUE TO use the window of 14 days back and 60 days forward and a page size of `maxResults=250`

3.7 WHEN the system requests Google access THEN the system SHALL CONTINUE TO request the same OAuth scopes (`calendar.readonly` for sync, `calendar.events` for writes)

3.8 WHEN an account uses the CalDAV or Apple provider THEN the system SHALL CONTINUE TO sync exactly as before

3.9 WHEN the existing schema is used THEN the system SHALL CONTINUE TO need no migration, because calendar provenance goes in the existing JSONB `extra` column
