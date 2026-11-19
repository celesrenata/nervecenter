# Ask NerveCenter Answer Coverage Bugfix Design

## Overview

Ask answers "what is my biggest bill?" with "the provided records do not contain information about your bills". Bills exist. The failure comes from the retrieval pipeline in `backend/context/retrieval/` and the prompt in `backend/context/ask.py`, not from the data.

Today retrieval is keyword search: `parse_intent` extracts keywords, adapters return up to `PER_ADAPTER_LIMIT` (300) rows, `score_record` drops any record without a keyword hit, and `rank` keeps at most `MAX_RECORDS` (12) or `CHAR_BUDGET` (3600 characters). The model then ranks, sums, and counts over that small slice. Words such as "biggest", "total", and "overdue" are operators, not record text, so they filter out every record. Several data areas have no adapter. A follow-up is retrieved from the new message alone.

The fix adds a deterministic layer ahead of the model and leaves the existing path intact for questions that already work:

1. **Query routing**: `parse_intent` classifies the question (operator, target domain, window, filters, conversational, follow-up) and strips operator words out of the required match terms.
2. **Aggregation**: a new `aggregate.py` computes max, min, top N, sum, count, average, earliest, latest, overdue, and due-soon over the full domain in code. The result and the rows it covers go to the model as evidence.
3. **New adapters**: finance summaries, obligations, knowledge, health, routines, briefing, mail-security status (summary only), and systems/infra status. They read through the same service layer the screens use.
4. **Retrieval limits**: adapters return whole-domain structured rows for aggregation. The evidence budget applies after computation, and the answer says when it is truncated.
5. **Answer wording**: the prompt and the empty-result answer name the sources searched, skipped, and not connected. A clearly empty lookup is answered in code.
6. **Conversation memory**: follow-ups resolve against the recent turns of the same Ask session before retrieval.

The request and response shapes stay the same. New response fields are additive.

## Glossary

- **Bug_Condition (C)**: An `AskRequest` whose answer exists in NerveCenter data and that uses an aggregation or comparison operator, has only domain words, targets a domain with no adapter, or is a follow-up (see `isBugCondition` below).
- **Property (P)**: A correct, cited answer computed from real records, with no "records do not contain" refusal when the data exists.
- **Preservation**: Behavior for requests outside C stays identical: ranked lookups, plain "no data" statements, `policy_denied`, scrubbing, exclusions, skip-on-failure, the proposal/confirm flow, citation ids, request and response shapes, saved sessions.
- **Operator**: A word or phrase that selects or computes over records and is not text to match. Examples: biggest, largest, most, total, count, average, earliest, latest, next, overdue, due soon, cheapest, top N.
- **Domain word**: A word that names a domain (bill, email, calendar). Used as a hint, never as a match term.
- **Computation**: A deterministic result (selected records, a sum, a count) produced in code, with its scope (date range, status filter, record count).
- **Evidence budget**: `MAX_RECORDS` and `CHAR_BUDGET`. The cap on how much text the model sees.
- **Structured row**: A record plus typed fields (`amount_cents`, `due_at`, `status`, `ts`) used by code to compute. It is not sent to the model as-is.
- **`retrieve`**: `backend/context/retrieval/service.py`. Runs adapters, ranks, returns `RetrievalResult`.
- **`score_record` / `rank`**: `backend/context/retrieval/ranker.py`. Keyword scoring and the evidence cap.
- **`parse_intent`**: `backend/context/retrieval/intent.py`. Deterministic question parsing.
- **`AskService._prompt`**: `backend/context/ask.py`. Builds the model prompt from evidence.
- **`_not_found_outcome`**: `backend/context/ask.py`. Answers an empty lookup in code.

## Bug Details

### Bug Condition

The bug manifests when the question needs more than keyword overlap with record text. `score_record` returns 0 for every record when the question has terms but none appear in the record. Operator words are kept as keywords, so they cause that miss. Domain words are removed from keywords, so "show my bills" has no terms and falls through to the domain-hint branch only when `has_terms` is false. Aggregates are left to a model that sees at most 12 rows. Domains without an adapter return nothing. A follow-up carries no subject.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type AskRequest   // question, session history, app state
  OUTPUT: boolean

  RETURN answerExists(X.question, appData)
         AND ( usesAggregationOrComparison(X.question)   // biggest, total, count, average, earliest, latest, overdue
            OR domainWordOnly(X.question)                // only domain and generic words, no match term
            OR targetsDomainWithoutAdapter(X.question)   // finance, knowledge, health, routines, status, briefing, obligations
            OR isFollowUp(X.question, X.history)         // refers to an earlier turn
            OR isConversational(X.question) )            // "hello", "what can you do?"
END FUNCTION
```

### Examples

- "What is my biggest bill?" Expected: the bill with the highest `amount_cents`, with payee, amount, due date, cited. Actual: "The provided records do not contain information about your bills or a biggest bill." (`biggest` is a keyword that no bill contains, so all bills score 0).
- "How much do I owe in total?" Expected: the sum over all unpaid bills, the count, and the scope. Actual: the model sees up to 12 filtered rows and no total.
- "Which bills are overdue?" Expected: every unpaid bill with `due_at` before now. Actual: `overdue` matches nothing, or the adapter ignores paid/overdue state and truncates at `limit`.
- "Show my bills" / "any email?" Expected: the most relevant or recent records of that domain. Actual: arbitrary or no records.
- "How did I sleep this week?" Expected: an answer from health samples. Actual: no health adapter, so "no information".
- "Is anything wrong right now?" Expected: open incidents and systems status. Actual: no status adapter.
- "And when is it due?" after a bill answer. Expected: the due date of the bill cited in the previous turn. Actual: retrieved with no subject.
- "Hello" / "what can you do?" Expected: a direct description of what Ask can search. Actual: treated as a lookup and refused.
- Edge: "What is my biggest bill?" with no bills stored. Expected: "I searched bills and found none." Not a model refusal and not an invented bill.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Lookups that work today ("what is on my calendar tomorrow", "find texts from Laura") return the same ranked records and the same cited answer.
- When the data truly does not exist, Ask says so plainly. It does not guess or invent records.
- Personal records go only over the secure local transport. Any other transport gets the existing `policy_denied` body. NerveShield-held mail bodies stay out.
- Snippets are scrubbed, clipped, and exclude every store in `EXCLUDED_STORES`. Every excluded store still has no adapter.
- A failing or timed-out adapter is skipped, recorded in `skipped`, and the other sources still answer.
- Calendar create and delete use the proposal and confirm flow unchanged. Nothing is written without confirmation.
- Citation ids stay stable and citation-safe. Unresolved ids go to `ungrounded_claims`. The web view still shows sources.
- Client-supplied `context`, `agent`, `inference`, `/api/context/ask/stream`, the CLI, SDK, and MCP tool accept the same request shape and return the same response shape. New fields are additive.
- Saved chats (`/api/ask/sessions`) load and save as before.

**Scope:**
All inputs that are plain keyword lookups with at least one match term, no operator, an existing domain, and no dependence on history are unaffected. This includes:
- Name, phrase, and keyword lookups across existing domains (mail, messages, calendar, tasks, logs, and so on)
- Date-window lookups ("next week", "last month")
- Calendar create/delete requests and requests with caller-supplied `context.fixture_refs`

## Hypothesized Root Cause

1. **Keyword gate in the ranker**: `score_record` returns 0 when `intent.has_terms` and `hits == 0`.
   - `parse_intent` only drops a short list of words (`STOPWORDS`, date words, domain words). "biggest", "largest", "overdue", "cheapest" survive as keywords.
   - "what is my biggest bill" therefore has keyword `biggest` and nothing else. Every bill scores 0.
   - Confirm by running `score_record` on a bill with `intent.keywords == ("biggest",)`.

2. **No operator or aggregation step**: nothing between `parse_intent` and the model recognizes superlatives, sums, counts, sorts, or next/latest/oldest.
   - `rank` caps at 12 records and 3600 characters, so the model cannot total or rank a full domain.
   - `STOPWORDS` also contains "most", "many", "much", "total", "count", which silently discards the operator signal.

3. **Narrow or missing adapters**:
   - The bills adapter returns `rows[:limit]`, ignores paid/overdue state, and emits only text snippets with no typed amount.
   - The mail adapter reads only the newest threads and the last message body.
   - No adapter exists for finance summaries, obligations, knowledge, health, routines, briefing, mail-security status, or systems/infra status.

4. **Silent truncation and generic refusal**:
   - Adapter limits and the evidence cap drop rows without telling the user.
   - `_prompt` says "say so plainly", and the model words that as "the provided records do not contain". The same sentence covers "no data", "not searched", and "source unavailable".
   - `_not_found_outcome` answers in code only when `lookup_shaped and has_terms`, so a domain-word-only or operator question goes to the model.

5. **No session context**: `retrieve(request.app.state, question)` in `backend/api/endpoints/context.py` receives only the current question. `session_id` is in the request but not used for retrieval.

6. **No conversational branch**: every question is a record lookup, so "hello" and "what can you do?" get a refusal.

## Correctness Properties

Property 1: Bug Condition - Operator, domain-only, uncovered-domain, follow-up, and conversational questions are answered correctly

_For any_ `AskRequest` where the bug condition holds (isBugCondition returns true), the fixed `ask'` SHALL return an answer whose stated value equals the value computed in code over the full target domain (selected record, sum, count, average, earliest, latest, overdue set), SHALL cite only retrieved record ids, SHALL state the scope used (date range, status filter, record count, truncation), and SHALL NOT contain "records do not contain" when the data exists.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.7, 2.8, 2.9**

Property 2: Bug Condition - Empty results name the sources and are answered in code

_For any_ `AskRequest` where the target domain was searched and holds no matching data, or the source is not connected, or the source was skipped, the fixed `ask'` SHALL answer from code without a model call, SHALL list the sources searched, unavailable, and skipped, and SHALL distinguish "no matching data" from "source not connected".

**Validates: Requirements 2.6**

Property 3: Preservation - Non-buggy requests are unchanged

_For any_ `AskRequest` where the bug condition does NOT hold (isBugCondition returns false), the fixed `ask'` SHALL produce the same ranked records, citations, status, and response shape as the original `ask`, preserving keyword and window lookups, plain "no data" statements, the proposal/confirm flow, saved sessions, and request and response shapes.

**Validates: Requirements 3.1, 3.2, 3.6, 3.8, 3.9**

Property 4: Preservation - Policy, scrubbing, exclusions, and failure isolation

_For any_ request and any retrieval path (old or new, including every new adapter), the fixed system SHALL send personal records only over the secure local transport and return the existing `policy_denied` body otherwise, SHALL scrub and clip every snippet, SHALL expose no store in `EXCLUDED_STORES`, SHALL skip a failing or timed-out adapter and still answer from the rest, and SHALL resolve cited ids to retrieved records or move them to `ungrounded_claims`.

**Validates: Requirements 3.3, 3.4, 3.5, 3.7**

## Fix Implementation

### Changes Required

Assuming the root cause analysis is correct. Each part below maps to one design area.

#### 1. Query routing and intent

**File**: `backend/context/retrieval/intent.py`

**Function**: `parse_intent`, `Intent`

1. **Operator extraction**: add `Operator` (`kind`: max, min, top_n, sum, count, avg, earliest, latest, next, overdue, due_soon, list; optional `n`; optional `field`: amount, due, ts, priority). Parse from the lowered text with a small table of phrases ("biggest", "largest", "most expensive", "highest", "cheapest", "smallest", "how much", "total", "how many", "average", "next", "latest", "oldest", "overdue", "past due", "due this week").
2. **Operator words are not keywords**: remove matched operator words from `keywords`. `most`, `many`, `much`, `total`, and `count` stay in `STOPWORDS` but now feed the operator table before being dropped.
3. **Target domain**: extend `_DOMAIN_WORDS` with the new domains (`finance`, `obligations`, `knowledge`, `health`, `routines`, `briefing`, `security`, `systems`) and phrases ("what do i owe", "is anything wrong", "what needs my attention", "morning routine"). Add `Intent.operator`, `Intent.filters` (status, payee, name), and `Intent.kind` in {`lookup`, `aggregate`, `domain_list`, `overview`, `conversational`, `followup`}.
4. **Domain-only questions**: when `keywords`, `names`, and `phrases` are empty and a domain hint exists, set `kind = domain_list`. The ranker uses the existing "list recent" branch and an adapter default (recent or most relevant, bounded).
5. **Conversational detection**: a small closed set (greetings, thanks, "what can you do", "help") sets `kind = conversational` only when the question has no domain hint and no names. Anything else stays a lookup.
6. **Follow-up detection**: `kind = followup` when the question is short, has no names or domain hint, and starts with or contains a pronoun or elliptical cue ("and the one after", "when is it due", "that one", "it", "them", "what about"). Resolution happens in section 6.

**File**: `backend/context/retrieval/ranker.py`

**Function**: `score_record`

7. Keep the keyword gate for `kind == lookup` with real match terms. Skip it for `aggregate`, `domain_list`, `overview`: those records are selected by `aggregate.py` or by domain, not by keyword hit. Ordinary lookups score exactly as before.

#### 2. Aggregation

**File (new)**: `backend/context/retrieval/aggregate.py`

**Function**: `compute(rows: list[StructuredRow], intent: Intent, now) -> Computation | None`

1. **Types**: `StructuredRow(record: Record, fields: dict[str, Any])` and `Computation(kind, value, text, scope, record_ids, truncated)`.
2. **Operators**: max/min/top N by a typed field, sum, count, average, earliest, latest, next (nearest `ts >= now`), overdue (`status` unpaid and `due_at < now`), due soon (`now <= due_at < now + 7 days`, or the intent window). Use `Decimal` or integer cents for money. Ties break by `due_at` then id, so output is stable.
3. **Scope**: apply `intent.window` and `intent.filters` before computing. `scope` records the window, status filter, payee filter, and row count. Default for "owe", "due", "overdue" is unpaid.
4. **Evidence**: the `Computation.text` becomes a first evidence item, `COMPUTED: <statement>`, with the covered record ids. The covered records (the winner, or the top rows up to the evidence budget) follow as normal records so citations resolve.
5. **Prompt**: the model words and cites the computed result. It is told not to recompute, rank, or count.

**File**: `backend/context/retrieval/types.py`, `service.py`

6. `RetrievalResult` gains `computation: Computation | None`, `truncated: dict[str, int]`, `unavailable: list[str]`, and `searched_detail`. All are additive.
7. `retrieve` runs an adapter's structured entry point when `intent.kind == aggregate`. Adapters without one fall back to the current record list.

#### 3. New and widened adapters

All adapters read through the service layer the screens use, return `None` when the service is absent (reported as `unavailable`), and go through `_util.make` so ids are citation-safe and snippets are scrubbed and clipped.

**File**: `backend/context/retrieval/sources/planning.py`

1. **Bills**: keep `bills(state, intent, limit)` for lookups. Add `bills_structured(state, intent)` that returns every bill with typed `amount_cents`, `currency`, `status`, `due_at`, `payee`. Remove the `rows[:limit]` cut for aggregation. Include proposed candidates as `unconfirmed` and exclude duplicates. Snippets include status so paid, overdue, and due-soon are visible.

**File (new)**: `backend/context/retrieval/sources/finance.py`, `obligations.py`, `knowledge.py`, `health.py`, `routines.py`, `briefing.py`, `status.py`

2. **finance**: spending summaries by payee and period from the finance service. Summaries only. Raw ledger lines stay excluded (`finance_ledger` remains in `EXCLUDED_STORES`).
3. **obligations**: commitments and what the user owes others, with due dates.
4. **knowledge**: notes, entities, claims, decisions. Text match plus title/entity match.
5. **health**: sleep, mood, energy samples with date and value. Supports average and range over a window. Mental-health form answers stay excluded.
6. **routines**: routines and check-ins with completion state for a day.
7. **briefing**: the daily briefing and "needs attention" view, built from the same call the Today screen uses.
8. **status**: open incidents, NerveShield and mail-security status as counts and summaries only (held/quarantined counts, no bodies), and systems, Kubernetes, and infra health. Internal stores (`mail_security`, `mail_evidence`, `mail_intel`, and so on) stay excluded. Only the existing service summary the screen shows is read.

**File**: `backend/context/retrieval/policy.py`, `sources/__init__.py`

9. Add each new domain to `DOMAINS` (id prefix, context kind, screen link) and to `ADAPTERS`. The existing exclusion test must keep passing, and a new test checks that no new adapter reads an excluded store.

**File**: `backend/context/retrieval/sources/communications.py`

10. **Mail**: read beyond the newest threads when the operator needs it (count unread, latest from a sender). Match on thread participants and subject across the whole mailbox window, not only the latest messages. Keep the held-by-NerveShield body exclusion.

#### 4. Retrieval limits

**File**: `backend/context/retrieval/service.py`, `ranker.py`

1. `PER_ADAPTER_LIMIT` (300) stays for keyword lookups. For aggregates the adapter reads the full domain, bounded by a hard safety cap (for example 5000 rows) and the existing `ADAPTER_TIMEOUT_S`. Hitting the cap sets `truncated[domain]`.
2. Compute first, then apply the evidence budget. `rank` keeps `MAX_RECORDS` and `CHAR_BUDGET` for the records the model sees. The computed result is never cut by the budget.
3. When rows were dropped by a cap, by `MAX_RECORDS`, or by an adapter limit, the answer text includes a scope line ("Based on 12 of 47 bills"). The truncation flag goes into `provenance`.

#### 5. Answer wording and empty results

**File**: `backend/context/ask.py`

**Functions**: `_prompt`, `_not_found_outcome`, `_skipped_notice`

1. **Prompt**: replace "If the records do not contain the answer, say so plainly" with instructions to (a) state any COMPUTED line first, (b) say which sources were searched when the evidence does not answer the question, (c) never write "the provided records do not contain" when evidence exists, and (d) not recompute or rank rows.
2. **Empty answer in code**: `_not_found_outcome` widens from `lookup_shaped and has_terms` to any retrieval with no records and no computation, including aggregate and domain-list intents. The answer reads, for example: "I searched bills and found none. Not connected: finance. Could not search: logs just now." Three states are distinct: searched with no match, not connected (`unavailable`), skipped on error (`skipped`).
3. **Conversational branch**: `kind == conversational` returns a fixed capability answer from a table of searchable domains, with no retrieval and no model call. The answer says what Ask can search and gives example questions.
4. **Response shape**: `status`, `answer`, `citations`, `records`, `ungrounded_claims`, `provenance` are unchanged. `provenance` gains `computation`, `truncated`, `unavailable`. Web, CLI, SDK, and MCP clients ignore unknown fields.

#### 6. Conversation memory

**File**: `backend/api/endpoints/context.py`, `backend/context/retrieval/service.py`, new `backend/context/retrieval/followup.py`

1. **History source**: load the last few turns (default 4) of the Ask session named by `session_id` from the existing sessions store. No new storage. If `session_id` is missing or has no turns, the question is treated as standalone.
2. **Resolution**: `resolve_followup(question, turns) -> ResolvedQuestion` carries over the previous subject (domain, filters, window, cited record ids) when `intent.kind == followup`. Resolution is deterministic and rule-based:
   - "and the one after that" shifts the previous ranked selection by one.
   - "when is it due" and similar field questions look up the previous cited record ids and read the requested field.
   - A new explicit domain or name in the follow-up overrides the carried subject.
3. **Retrieval**: `retrieve(state, question, now, history=turns)` runs the resolved intent. The user's literal question is still what the model sees, plus a short "Previous subject" line.
4. **Safety**: carried record ids are re-read through the adapters, not trusted from stored text, and pass the same policy, scrub, and exclusion steps. A history turn never widens access.
5. **Stream and non-stream**: `ask` and `ask_stream` both call the same `_retrieval` helper, so behavior is the same on `/api/context/ask/stream`.

## Testing Strategy

### Validation Approach

The strategy runs in two phases. First, write tests that surface counterexamples on the UNFIXED code. Then confirm the fix and the preserved behavior. Tests use seeded fake services and a scripted model, as `tests/backend/test_ask_retrieval.py` does (`make_state`, fixed `NOW`). No test calls a real model or network. Hypothesis (already in `pyproject.toml`) drives the property tests.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE the fix. Confirm or refute the root causes. If refuted, re-hypothesize.

**Test Plan**: Seed a fake state with several bills (different amounts, statuses, due dates) and one or two rows in each uncovered domain. Run `retrieve` and `AskService.ask` with a scripted model that echoes the evidence it receives. Run on UNFIXED code and record what it sees.

**Test Cases**:
1. **Biggest bill**: "what is my biggest bill?" with 5 bills. Assert the highest bill is in the evidence and the answer names it (will fail on unfixed code: all bills score 0).
2. **Total owed**: "how much do I owe in total?" Assert the evidence contains the correct sum (will fail: no computation).
3. **Overdue**: "which bills are overdue?" with paid and unpaid past-due bills. Assert only unpaid past-due bills are selected (will fail).
4. **Domain-only**: "show my bills" and "any email?". Assert recent records of the domain are returned (may fail on unfixed code).
5. **Uncovered domain**: "how did I sleep this week?", "is anything wrong right now?", "what do my notes say about the lease?" Assert at least one record from the new domain (will fail: no adapter).
6. **Truncation**: 40 bills, ask for the total. Assert the sum covers all 40 (will fail: cap at 12).
7. **Follow-up**: turn 1 "what is my biggest bill?", turn 2 "when is it due?". Assert turn 2 retrieves that bill (will fail: no history).
8. **Conversational**: "hello", "what can you do?". Assert no "do not contain" refusal (will fail).

**Expected Counterexamples**:
- Every bill has score 0 when `keywords == ("biggest",)`.
- Evidence has at most `MAX_RECORDS` rows and no computed value.
- New-domain questions return an empty `searched` list entry and the refusal sentence.
- Follow-up retrieval has no subject.
- Possible causes: keyword gate, missing operator step, missing adapters, adapter limits, no history in `retrieve`.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := ask_fixed(X)
  ASSERT result.answer states expectedValue(X, appData)      // computed independently from the seeded data
     AND result.citations SUBSET OF retrievedIds(result)
     AND NOT contains(result.answer, "records do not contain")
     AND result.scope IS stated
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT ask_original(X) = ask_fixed(X)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: Observe `retrieve` and `rank` output on UNFIXED code for keyword, name, phrase, and window lookups. Record those as golden results. Re-run after the fix and compare.

**Test Cases**:
1. **Keyword and name lookups**: "find texts from Laura", "what did the school send about Friday" return the same ordered record ids.
2. **Window lookups**: "what is on my calendar tomorrow", "next week" return the same records.
3. **No-data statement**: a lookup with real terms and no match still gets the "I found nothing about ..." answer without a model call.
4. **Policy and transport**: a non-local transport still returns `policy_denied` with no retrieval.
5. **Exclusions**: every store in `EXCLUDED_STORES` has no adapter, including the new ones.
6. **Adapter failure**: a raising or slow adapter lands in `skipped`, and the others still answer.
7. **Calendar proposal flow**: create and delete requests behave as in `test_ask_calendar_create.py` and `test_ask_delete_*.py`.
8. **Citations**: unresolved ids go to `ungrounded_claims`. Ids keep the `<prefix>-<10 hex>` form.
9. **Response shape**: the old keys are present with the same types. New keys are only additions.

### Unit Tests

- `parse_intent`: operator extraction for each operator, operator words removed from `keywords`, `kind` classification (lookup, aggregate, domain_list, conversational, followup), domain hints for new domains.
- `aggregate.compute`: max, min, top N, sum, count, average, earliest, latest, next, overdue, due soon; empty input; ties; currency and cents handling; window and status filters; scope text.
- `score_record`: keyword gate unchanged for lookups, bypassed for aggregate and domain-list.
- Each new adapter: returns records with citation-safe ids, scrubbed and clipped snippets, `None` when its service is missing, no excluded store touched.
- `resolve_followup`: carries subject, shifts selection, overrides on new name or domain, standalone with empty history.
- `_prompt` and `_not_found_outcome`: wording for no data, not connected, and skipped; no "records do not contain" when evidence exists; conversational capability answer.
- At least one test per category C1 to C12, each with seeded fake data and a scripted model (Requirement 2.9).

### Property-Based Tests

- Generate random bill sets (amounts, statuses, due dates, payees, counts above `MAX_RECORDS`). For "biggest", "total", "overdue", "due this week", assert the computed value equals an independent reference calculation, and the cited ids are in the retrieved set.
- Generate random questions made only of generic and domain words. Assert the intent has no required match terms and the result is a bounded, recent-first domain list.
- Generate random questions with at least one non-operator keyword and no operator. Assert `score_record` and `rank` output equal the original implementation (preservation).
- Generate random adapter outcomes (ok, empty, raise, timeout, `None`). Assert the answer names the sources correctly in each state and never raises.
- Generate random record text with secrets and long strings. Assert every snippet from every adapter, old and new, is scrubbed and clipped.
- Generate random two-turn sessions. Assert a follow-up never returns a record outside what the same adapters and policy allow for the current user.

### Integration Tests

- Full Ask through `POST /api/context/ask` and `/api/context/ask/stream` with seeded services and a scripted model: "what is my biggest bill?" answers with the right bill and a resolvable citation.
- Two-turn flow using `session_id`: biggest bill, then "when is it due?".
- Cross-domain: "what is going on with my taxes?" returns bills, mail, tasks, and knowledge records together within the evidence budget.
- Briefing and status: "what do I need to know today?" and "is anything wrong right now?" return briefing and incident/system records.
- Truncation: a domain larger than the evidence budget yields a correct computed total and a stated scope.
- Transport denial and adapter failure paths still return `policy_denied` and a partial answer with `skipped`, respectively.
- Saved sessions (`/api/ask/sessions`) round-trip unchanged, and the web view still shows sources for the new records.
