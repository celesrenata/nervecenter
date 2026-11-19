# Bugfix Requirements Document

## Introduction

The "Ask NerveCenter" view cannot answer many ordinary questions. Example: the user asked "what is my biggest bill?" and Ask replied "The provided records do not contain information about your bills or a biggest bill." Bills exist in the system (`BillService`, `/bills`), so the refusal is wrong.

Ask retrieves records in `backend/context/retrieval/` (`intent.py`, `ranker.py`, `service.py`, `sources/`) and asks the model to answer strictly from them (`backend/context/ask.py`, `_prompt`). Reading of the code points to these root causes:

- **Keyword gate in the ranker.** `score_record` returns 0 for any record with no keyword, name, or phrase hit once the question has any term. Words such as "biggest", "largest", "most", "total", "overdue" or "cheapest" are kept as keywords but never appear in record text, so every bill is dropped. Domain words ("bill", "email") are removed from keywords, so "what is my biggest bill" has no usable match term at all.
- **No query routing or aggregation.** There is no step that recognizes a superlative, sum, count, sort, or "next/latest/oldest" question and computes it over the whole domain. The model only sees at most 12 records or about 3600 characters (`MAX_RECORDS`, `CHAR_BUDGET`), after a keyword filter, so it cannot rank or total anything correctly.
- **Narrow adapters.** The bills adapter lists bills and proposed candidates but ignores paid/overdue state in ranking and drops anything past `limit`. The mail adapter reads only the newest threads and the last message body. Several data areas the product holds have no adapter at all: finance summaries, obligations and commitments, knowledge notes/entities/claims/decisions, health samples, routines and check-ins, the briefing/today view, NerveShield and mail-security status, and systems/Kubernetes/infra status.
- **Refusal wording.** The prompt tells the model to "say so plainly" when records lack the answer, and the model phrases that as "the provided records do not contain...". That text is also used when retrieval was simply too narrow, so the user cannot tell "no data" from "not searched".
- **No conversation memory.** Follow-ups such as "and the one after that?" or "when is it due?" are retrieved with no context from the earlier turns.

## Hypothesized user question categories

Ask must answer these categories when the underlying data exists in NerveCenter. Examples are illustrative.

| # | Category | Example questions |
|---|---|---|
| C1 | Bills and spending | "What is my biggest bill?", "What bills are due this week?", "How much do I owe in total?", "Which bills are overdue?", "How much did I pay Comcast last month?" |
| C2 | Email | "Any email from my landlord?", "What did the school send about Friday?", "How many unread threads do I have?", "Summarize my latest email from Sam." |
| C3 | Calendar | "What is on my calendar tomorrow?", "When is my next dentist appointment?", "Am I free Thursday afternoon?", "How many meetings do I have next week?" |
| C4 | Texts, relay and voicemail | "Did anyone leave me a voicemail today?", "What did Laura text me last?", "Who called back?" |
| C5 | People and contacts | "What is Dana's phone number?", "Who is waiting on me?", "Who am I waiting on?" |
| C6 | Tasks, goals, follow-ups, obligations | "What are my open tasks?", "What is my highest-priority task?", "What do I owe people?", "Which goals are behind?" |
| C7 | Documents and knowledge | "What do my notes say about the lease?", "What decisions did I record about the move?", "Find the insurance document." |
| C8 | Health, wellness and routines | "How did I sleep this week?", "What was my average mood last month?", "Did I do my morning routine today?" |
| C9 | Security and ops status | "Is anything wrong right now?", "Are there open incidents?", "Did NerveShield hold any mail today?", "Which systems are down?", "What errors happened in the last hour?" |
| C10 | Daily briefing and overview | "What do I need to know today?", "What needs my attention?", "Summarize my week." |
| C11 | Cross-domain lookups | "Everything about the Henderson project", "What is going on with my taxes?" |
| C12 | Follow-ups and chat | "And the one after that?", "When is it due?", "What can you do?", "Hello" |

## Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type AskRequest  // question, conversation history, app state
  OUTPUT: boolean

  RETURN answerExists(X.question, appData)             // the data to answer it is in NerveCenter
     AND ( usesAggregationOrComparison(X.question)     // biggest, most, total, count, average, earliest, latest, overdue
        OR domainWordOnly(X.question)                  // "my bills", "my email": only domain words, no match terms
        OR targetsDomainWithoutAdapter(X.question)     // finance, knowledge, health, routines, status, briefing, obligations
        OR isFollowUp(X.question, X.history) )         // refers to an earlier turn
END FUNCTION
```

```pascal
// Property: Fix Checking
FOR ALL X WHERE isBugCondition(X) DO
  result <- ask'(X)
  ASSERT result.answer states the correct value computed from the real records
     AND every factual claim cites a retrieved record id
     AND NOT refusesWith("records do not contain") WHEN answerExists(X.question, appData)
END FOR
```

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT ask(X) = ask'(X)
END FOR
```

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the user asks a superlative or comparison question about records that exist (for example "what is my biggest bill?") THEN the system drops every record because no record contains the word "biggest", and the model replies that the provided records contain no bills.

1.2 WHEN the user asks for a total, count, average, earliest, latest, or overdue/due-soon selection (for example "how much do I owe in total?", "which bills are overdue?") THEN the system gives the model at most about 12 keyword-filtered records and no computed result, so the answer is missing, partial, or wrong.

1.3 WHEN the user asks a question made only of domain words and generic words (for example "show my bills", "any email?", "what is on my plate?") THEN the system may retrieve nothing or arbitrary records because there is no match term, instead of listing the most relevant recent records of that domain.

1.4 WHEN the user asks about finance and spending, obligations and commitments, knowledge notes/decisions/claims, health samples, routines, the daily briefing, NerveShield or mail-security status, or systems/Kubernetes status THEN the system has no retrieval adapter for that data and answers that it has no information.

1.5 WHEN a source adapter returns more rows than its limit, or only the newest threads (mail) or last body (email thread), THEN the system silently omits matching older or deeper content and does not tell the user the search was partial.

1.6 WHEN retrieval finds no usable records THEN the system answers with generic wording ("the provided records do not contain information") that does not say which sources were searched, and a lookup with no match term is sent to the model instead of being answered from the data.

1.7 WHEN the user asks a follow-up that depends on the previous turn (for example "and when is it due?") THEN the system retrieves using only the new message and loses the subject.

1.8 WHEN the user asks a general or conversational question (for example "what can you do?", "hello") THEN the system treats it as a record lookup and can reply that the records contain no information.

### Expected Behavior (Correct)

2.1 WHEN the user asks a superlative or comparison question about records that exist THEN the system SHALL recognize the operator (max, min, top N, earliest, latest, overdue, due soon), apply it to the whole target domain in code, and answer with the selected record and its value, citing it (for example the bill with the highest amount, its payee, amount, and due date).

2.2 WHEN the user asks for a total, count, average, or filtered selection THEN the system SHALL compute the result deterministically over all matching records in the domain and window, provide the computed value and the records it covers to the model as evidence, and state the scope used (date range, status filter, number of records).

2.3 WHEN the question has only domain words and generic words THEN the system SHALL return the most relevant or most recent records of the hinted domain (bounded and ranked), and SHALL treat words that are only operators or filler (biggest, total, overdue, latest) as operators, not as required match terms.

2.4 WHEN the user asks about finance and spending, obligations and commitments, knowledge notes/decisions/claims, health samples, routines and check-ins, the daily briefing, NerveShield and mail-security status, or systems, incidents and infrastructure status THEN the system SHALL have an adapter for each that reads through the same service layer the screens use, and SHALL answer from it, subject to the existing allowlist and exclusion policy.

2.5 WHEN a source has more matching data than fits in the evidence budget THEN the system SHALL search the full domain before ranking, SHALL say when the answer is based on a truncated set, and SHALL not rely on the model to count or rank raw rows.

2.6 WHEN retrieval finds nothing or the data is not connected THEN the system SHALL say which sources were searched and which were unavailable or skipped, SHALL distinguish "no matching data" from "source not connected", and SHALL answer from code without a model call where the lookup is clearly empty.

2.7 WHEN the user asks a follow-up THEN the system SHALL resolve it against the recent turns of the same Ask session (subject, domain, time window, and cited records) before retrieving.

2.8 WHEN the user asks a conversational or capability question THEN the system SHALL answer directly, describing what Ask can search, without a record lookup and without a "records do not contain" refusal.

2.9 WHEN the system answers any question in categories C1 to C12 above and the data exists THEN the system SHALL give a correct, cited answer, and each category SHALL have at least one automated test using seeded fake data and a scripted model.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the question is a lookup that already works today (for example "what is on my calendar tomorrow", "find texts from Laura") THEN the system SHALL CONTINUE TO return the same ranked records and cited answer.

3.2 WHEN the retrieved records do not contain the answer and data truly does not exist THEN the system SHALL CONTINUE TO say so plainly and not guess or invent records.

3.3 WHEN personal records are involved THEN the system SHALL CONTINUE TO send them only over the secure local transport, deny with the existing `policy_denied` body on any other transport, and keep NerveShield-held mail bodies out.

3.4 WHEN a snippet is built THEN the system SHALL CONTINUE TO scrub secrets, clip text, and exclude the stores in `EXCLUDED_STORES` (credentials, auth/sessions, security internals, plumbing, mental-health forms, raw finance ledger lines), and every excluded store SHALL still have no adapter.

3.5 WHEN a source adapter fails or times out THEN the system SHALL CONTINUE TO skip it, record it in `skipped`, and still answer from the other sources.

3.6 WHEN the user asks Ask to create or delete calendar events THEN the system SHALL CONTINUE TO use the proposal and confirm flow unchanged, and nothing is written without confirmation.

3.7 WHEN a citation is returned THEN the system SHALL CONTINUE TO use stable, citation-safe record ids that resolve to a retrieved record, drop unresolved ids into `ungrounded_claims`, and show sources in the web view.

3.8 WHEN a client supplies its own `context`, `agent`, or `inference` selection, or uses `/api/context/ask/stream`, the CLI, SDK, or MCP tool THEN the system SHALL CONTINUE TO accept the same request shape and return the same response shape (new fields only additive).

3.9 WHEN saved Ask chats are loaded or saved (`/api/ask/sessions`) THEN the system SHALL CONTINUE TO behave as before.
