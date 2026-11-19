# Implementation Plan

Tests use seeded fake services and a scripted model, following `tests/backend/test_ask_retrieval.py` (`make_state`, fixed `NOW`). No real model or network calls. Property tests use Hypothesis.

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Operator, domain-only, uncovered-domain, follow-up, and conversational questions are refused or answered wrongly
  - **CRITICAL**: This test MUST FAIL on unfixed code. Failure confirms the bug exists.
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: The test encodes the expected behavior and validates the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: Scope the property to the concrete failing cases below, with Hypothesis varying amounts, statuses, due dates, and counts
  - Create `tests/backend/test_ask_answer_coverage_exploration.py` with a fake state seeded with bills (varied amounts, paid/unpaid, past and future due dates) and one or two rows per uncovered domain, plus a scripted model that echoes the evidence it receives
  - Cases from design (isBugCondition: aggregation or comparison, domain-word-only, no adapter, follow-up, conversational):
    - "what is my biggest bill?" with 5 bills: evidence and answer name the highest `amount_cents` bill
    - "how much do I owe in total?": evidence contains the correct sum of unpaid bills
    - "which bills are overdue?": only unpaid bills with `due_at` before now
    - "show my bills" and "any email?": recent records of the domain
    - "how did I sleep this week?", "is anything wrong right now?", "what do my notes say about the lease?": at least one record from the new domain
    - 40 bills, ask for the total: the sum covers all 40
    - Turn 1 "what is my biggest bill?", turn 2 "when is it due?" with `session_id`: turn 2 retrieves that bill
    - "hello", "what can you do?": answer has no "records do not contain" text and no record lookup
  - Assert: answer value equals an independently computed reference, citations are a subset of retrieved ids, answer does not contain "records do not contain"
  - Run on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct and proves the bug exists)
  - Document counterexamples (for example `score_record` is 0 for every bill when `keywords == ("biggest",)`, evidence capped at `MAX_RECORDS`, no adapter for new domains, follow-up has no subject)
  - Mark the task complete when the test is written, run, and the failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 3: Preservation** - Non-buggy requests are unchanged
  - **Property 4: Preservation** - Policy, scrubbing, exclusions, and failure isolation
  - **IMPORTANT**: Follow observation-first methodology
  - Observe on UNFIXED code, record as golden results:
    - Ordered record ids and cited answer for "find texts from Laura" and "what did the school send about Friday"
    - Records for "what is on my calendar tomorrow" and "next week" window lookups
    - "I found nothing about ..." answer, with no model call, for a lookup with real terms and no match
    - `policy_denied` body for a non-local transport, with no retrieval
    - `EXCLUDED_STORES` have no adapter; adapter raise or timeout lands in `skipped` and others still answer
    - Calendar create and delete proposal/confirm flow (reuse `test_ask_calendar_create.py` and `test_ask_delete_*.py` cases)
    - Citation id form `<prefix>-<10 hex>`, unresolved ids moved to `ungrounded_claims`
    - Response keys and types, saved sessions via `/api/ask/sessions`
  - Create `tests/backend/test_ask_answer_coverage_preservation.py`
  - Write property-based tests:
    - Random questions with at least one non-operator keyword, no operator, and no history: `score_record` and `rank` output equals the recorded original behavior
    - Random adapter outcomes (ok, empty, raise, timeout): answer never raises, failures appear in `skipped`
    - Random record text with secrets and long strings: every snippet is scrubbed and clipped
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark the task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

- [x] 3. Fix for Ask answer coverage (operators, aggregation, adapters, limits, wording, follow-ups)

  - [x] 3.1 Add operator, kind, and filters to intent parsing
    - In `backend/context/retrieval/intent.py` add `Operator` (`kind`: max, min, top_n, sum, count, avg, earliest, latest, next, overdue, due_soon, list; optional `n`; optional `field`)
    - Add a phrase table (biggest, largest, most expensive, highest, cheapest, smallest, how much, total, how many, average, next, latest, oldest, overdue, past due, due this week)
    - Remove matched operator words from `keywords`; keep `most`, `many`, `much`, `total`, `count` in `STOPWORDS` but feed the operator table first
    - Extend `_DOMAIN_WORDS` with finance, obligations, knowledge, health, routines, briefing, security, systems and phrases ("what do i owe", "is anything wrong", "what needs my attention", "morning routine")
    - Add `Intent.operator`, `Intent.filters` (status, payee, name), `Intent.kind` in {lookup, aggregate, domain_list, overview, conversational, followup}
    - Set `kind = domain_list` when keywords, names, and phrases are empty and a domain hint exists
    - Set `kind = conversational` from a closed set (greetings, thanks, "what can you do", "help") only when no domain hint and no names
    - Set `kind = followup` for short questions with no names or domain hint and an elliptical or pronoun cue
    - _Bug_Condition: isBugCondition(X) where usesAggregationOrComparison OR domainWordOnly OR isFollowUp OR isConversational_
    - _Expected_Behavior: operator words are operators, not match terms; kind classifies the question_
    - _Preservation: plain keyword, name, phrase, and window lookups parse to the same keywords and kind = lookup_
    - _Requirements: 2.1, 2.2, 2.3, 2.7, 2.8, 3.1_

  - [x] 3.2 Skip the keyword gate for non-lookup kinds in the ranker
    - In `backend/context/retrieval/ranker.py` `score_record`, keep the gate for `kind == lookup` with real match terms
    - Skip it for `aggregate`, `domain_list`, and `overview`; use the existing "list recent" branch for domain lists
    - _Bug_Condition: isBugCondition(X) where every record scores 0 because operator words are keywords_
    - _Expected_Behavior: domain-only and aggregate questions return bounded, recent-first domain records_
    - _Preservation: lookup scoring is identical to the original_
    - _Requirements: 2.3, 3.1_

  - [x] 3.3 Add the aggregation module
    - Create `backend/context/retrieval/aggregate.py` with `StructuredRow(record, fields)`, `Computation(kind, value, text, scope, record_ids, truncated)`, and `compute(rows, intent, now)`
    - Implement max, min, top N, sum, count, average, earliest, latest, next, overdue (unpaid and `due_at < now`), due soon (`now <= due_at < now + 7 days` or the intent window)
    - Use integer cents or `Decimal` for money; break ties by `due_at` then id
    - Apply `intent.window` and `intent.filters` first; default status for "owe", "due", "overdue" is unpaid; record the scope (window, status, payee, row count)
    - Produce `COMPUTED: <statement>` evidence text with covered record ids
    - Return `None` for empty input
    - _Bug_Condition: isBugCondition(X) where usesAggregationOrComparison(X.question)_
    - _Expected_Behavior: value computed in code over the full domain, scope stated_
    - _Preservation: not invoked for kind = lookup_
    - _Requirements: 2.1, 2.2, 2.5, 3.1_

  - [x] 3.4 Extend retrieval types and service
    - In `backend/context/retrieval/types.py` add `computation`, `truncated`, `unavailable`, `searched_detail` to `RetrievalResult` as additive fields
    - In `service.py` run an adapter's structured entry point when `intent.kind == aggregate`; adapters without one fall back to the current record list
    - For aggregates read the full domain up to a hard safety cap (about 5000 rows) within `ADAPTER_TIMEOUT_S`; set `truncated[domain]` on hitting the cap; keep `PER_ADAPTER_LIMIT` for lookups
    - Compute first, then apply `MAX_RECORDS` and `CHAR_BUDGET` only to model-visible records; never cut the computed result
    - Record dropped-row counts and add a scope line ("Based on 12 of 47 bills") and a `provenance` truncation flag
    - Keep adapter failure isolation: raise or timeout goes to `skipped`; a service that is absent goes to `unavailable`
    - _Bug_Condition: isBugCondition(X) where the domain exceeds the evidence budget or adapters cut rows_
    - _Expected_Behavior: full-domain search before ranking, truncation stated_
    - _Preservation: skip-on-failure, `policy_denied`, and lookup limits unchanged_
    - _Requirements: 2.2, 2.5, 2.6, 3.3, 3.5_

  - [x] 3.5 Widen the bills adapter (C1)
    - In `backend/context/retrieval/sources/planning.py` keep `bills(state, intent, limit)` for lookups
    - Add `bills_structured(state, intent)` returning every bill with typed `amount_cents`, `currency`, `status`, `due_at`, `payee`; no `rows[:limit]` cut
    - Include proposed candidates as `unconfirmed`, exclude duplicates; put status in the snippet
    - _Requirements: 2.1, 2.2, 2.5_

  - [x] 3.6 Add finance adapter (C1)
    - Create `sources/finance.py`: spending summaries by payee and period from the finance service; summaries only
    - Do not read raw ledger lines; `finance_ledger` stays in `EXCLUDED_STORES`
    - Expose a structured entry point for sum, count, and average by payee and month
    - _Requirements: 2.4, 3.4_

  - [x] 3.7 Add obligations adapter (C6)
    - Create `sources/obligations.py`: commitments and what the user owes others and others owe the user, with due dates and status
    - Expose a structured entry point for overdue, due soon, and counts
    - _Requirements: 2.4_

  - [x] 3.8 Add knowledge adapter (C7)
    - Create `sources/knowledge.py`: notes, entities, claims, decisions; text match plus title and entity match
    - _Requirements: 2.4_

  - [x] 3.9 Add health adapter (C8)
    - Create `sources/health.py`: sleep, mood, energy samples with date and value; structured entry point for average and range over a window
    - Mental-health form answers stay excluded
    - _Requirements: 2.4, 3.4_

  - [x] 3.10 Add routines adapter (C8)
    - Create `sources/routines.py`: routines and check-ins with completion state for a day
    - _Requirements: 2.4_

  - [x] 3.11 Add briefing adapter (C10)
    - Create `sources/briefing.py`: daily briefing and "needs attention" view using the same call as the Today screen
    - _Requirements: 2.4_

  - [x] 3.12 Add status adapter (C9)
    - Create `sources/status.py`: open incidents, NerveShield and mail-security status as counts and summaries only (no bodies), systems, Kubernetes, and infra health
    - Read only the service summary the screen shows; `mail_security`, `mail_evidence`, `mail_intel` and other internal stores stay excluded
    - _Requirements: 2.4, 3.3, 3.4_

  - [x] 3.13 Register new domains and adapters
    - In `backend/context/retrieval/policy.py` add each new domain to `DOMAINS` (id prefix, context kind, screen link)
    - In `sources/__init__.py` add each adapter to `ADAPTERS`
    - Every new adapter returns `None` when its service is absent, builds records through `_util.make` (citation-safe ids, scrubbed, clipped)
    - _Requirements: 2.4, 3.4, 3.7_

  - [x] 3.14 Widen the mail adapter (C2)
    - In `sources/communications.py` read beyond the newest threads when the operator needs it (count unread, latest from a sender)
    - Match on thread participants and subject across the mailbox window, not only the latest messages
    - Keep the NerveShield-held body exclusion
    - _Requirements: 1.5, 2.2, 2.5, 3.3_

  - [x] 3.15 Check calendar, messages, people, tasks adapters for operator support (C3, C4, C5, C6)
    - Add structured entry points where needed for next appointment, count of meetings in a window, free/busy, last text from a person, voicemail today, contact fields, open tasks by priority, goals behind
    - Keep lookup entry points unchanged
    - _Requirements: 2.1, 2.2, 3.1_

  - [x] 3.16 Change answer wording and empty results
    - In `backend/context/ask.py` `_prompt`: replace "say so plainly" with instructions to state any COMPUTED line first, name sources searched when evidence does not answer, never write "the provided records do not contain" when evidence exists, and not recompute or rank rows
    - Widen `_not_found_outcome` to any retrieval with no records and no computation (including aggregate and domain-list); answer in code without a model call and distinguish no match, not connected (`unavailable`), and skipped (`skipped`)
    - Add the conversational branch: fixed capability answer from a table of searchable domains with example questions, no retrieval, no model call
    - Add `computation`, `truncated`, `unavailable` to `provenance`; keep `status`, `answer`, `citations`, `records`, `ungrounded_claims`, `provenance` as before
    - _Bug_Condition: isBugCondition(X) where retrieval is empty or the question is conversational_
    - _Expected_Behavior: Property 2, sources named, answered from code_
    - _Preservation: existing "I found nothing about ..." answer and response shape_
    - _Requirements: 2.6, 2.8, 3.2, 3.7, 3.8_

  - [x] 3.17 Add follow-up resolution
    - Create `backend/context/retrieval/followup.py` with `resolve_followup(question, turns) -> ResolvedQuestion`
    - Carry over the previous domain, filters, window, and cited record ids; "the one after that" shifts the previous ranked selection by one; "when is it due" reads the field of the previous cited record; a new explicit domain or name overrides the carried subject
    - In `backend/api/endpoints/context.py` load the last 4 turns of the session named by `session_id` from the existing sessions store; no new storage; missing session means standalone
    - Pass `history` to `retrieve(state, question, now, history=turns)`; the model sees the literal question plus a short "Previous subject" line
    - Re-read carried record ids through the adapters, never trust stored text; the same policy, scrub, and exclusion steps apply
    - Route `ask` and `ask_stream` through the same `_retrieval` helper
    - _Bug_Condition: isBugCondition(X) where isFollowUp(X.question, X.history)_
    - _Expected_Behavior: follow-up resolved against recent turns of the same session_
    - _Preservation: requests without `session_id` or history behave as before; saved sessions unchanged_
    - _Requirements: 2.7, 3.3, 3.8, 3.9_

  - [x] 3.18 Write unit tests for the new components
    - `tests/backend/test_ask_intent_operators.py`: operator extraction for each operator, operator words removed from `keywords`, `kind` classification, new domain hints
    - `tests/backend/test_ask_aggregate.py`: every operator, empty input, ties, cents and currency, window and status filters, scope text
    - `tests/backend/test_ask_ranker_gate.py`: gate unchanged for lookups, bypassed for aggregate and domain-list
    - `tests/backend/test_ask_new_adapters.py`: each new adapter returns citation-safe ids, scrubbed and clipped snippets, `None` when its service is missing, and touches no excluded store; a check that no adapter exists for any `EXCLUDED_STORES` entry
    - `tests/backend/test_ask_followup.py`: carries subject, shifts selection, overrides on new name or domain, standalone with empty history
    - `tests/backend/test_ask_wording.py`: no-data, not-connected, and skipped wording; no "records do not contain" when evidence exists; conversational capability answer
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_

  - [x] 3.19 Write per-category tests C1 to C12
    - Create `tests/backend/test_ask_categories.py` with seeded fake data and a scripted model; at least one test per category, each asserting a correct value and a resolvable citation:
      - C1 Bills and spending: biggest bill, total owed, overdue, due this week, amount paid to a payee last month
      - C2 Email: from a sender, about a topic, unread count, latest from a person
      - C3 Calendar: tomorrow, next appointment of a kind, free on a day part, meeting count next week
      - C4 Texts, relay, voicemail: voicemail today, last text from a person, who called back
      - C5 People and contacts: phone number, who is waiting on me, who I am waiting on
      - C6 Tasks, goals, follow-ups, obligations: open tasks, highest-priority task, what I owe people, goals behind
      - C7 Documents and knowledge: notes about a topic, recorded decisions, find a document
      - C8 Health, wellness, routines: sleep this week, average mood last month, morning routine today
      - C9 Security and ops status: anything wrong now, open incidents, held mail today (count only), systems down, recent errors
      - C10 Briefing and overview: what I need to know today, what needs attention, week summary
      - C11 Cross-domain: "everything about the Henderson project", "what is going on with my taxes" returns multiple domains within the evidence budget
      - C12 Follow-ups and chat: two-turn "biggest bill" then "when is it due", "and the one after that", "what can you do", "hello"
    - _Requirements: 2.9_

  - [x] 3.20 Write property-based and integration tests
    - Property: random bill sets (amounts, statuses, due dates, payees, counts above `MAX_RECORDS`) for biggest, total, overdue, due this week; computed value equals an independent reference and cited ids are in the retrieved set
    - Property: random questions of only generic and domain words give no required match terms and a bounded recent-first domain list
    - Property: random adapter outcomes (ok, empty, raise, timeout, `None`) produce correct source naming and never raise
    - Property: random two-turn sessions never return a record outside what the same adapters and policy allow
    - Integration: `POST /api/context/ask` and `/api/context/ask/stream` for "what is my biggest bill?"; two-turn flow with `session_id`; cross-domain taxes question; briefing and status questions; domain larger than the evidence budget gives a correct total and a stated scope; transport denial and adapter failure paths still return `policy_denied` and `skipped`
    - _Requirements: 2.1, 2.2, 2.5, 2.6, 2.7, 3.3, 3.5_

  - [x] 3.21 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Operator, domain-only, uncovered-domain, follow-up, and conversational questions are answered correctly
    - **IMPORTANT**: Re-run the SAME test from task 1. Do NOT write a new test.
    - The test from task 1 encodes the expected behavior
    - Run `tests/backend/test_ask_answer_coverage_exploration.py`
    - **EXPECTED OUTCOME**: Test PASSES (confirms the bug is fixed)
    - Also confirm Property 2: empty results name searched, unavailable, and skipped sources and are answered without a model call
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_

  - [x] 3.22 Verify preservation tests still pass
    - **Property 3: Preservation** - Non-buggy requests are unchanged
    - **Property 4: Preservation** - Policy, scrubbing, exclusions, and failure isolation
    - **IMPORTANT**: Re-run the SAME tests from task 2. Do NOT write new tests.
    - Run `tests/backend/test_ask_answer_coverage_preservation.py` and the existing `test_ask_retrieval.py`, `test_ask_calendar_create.py`, `test_ask_delete_*.py`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full backend test suite and confirm all tests pass, including the C1 to C12 category tests
  - Ask the user if questions arise
