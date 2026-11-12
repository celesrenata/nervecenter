# Spike 8.3 — Personal expectedness engine (research)

Date: 2026-11-12. Status: offline prototype in `backend/mail_security/research/expectedness.py`, with synthetic data only. It is not wired, has no flag, and used no real mail.

## Question

Does a per-account model of "what does this sender normally ask me for?" separate anomalous high-impact asks better than an unpersonalized prior? And can it do that with opt-in, explicit uncertainty, fair treatment of sparse senders, and reversible feedback?

## Prototype

**Opt-in per account.** When the account isn't opted in, nothing is recorded and every score is `NOT_OPTED_IN`.

**Storage.** For each HMAC(account salt, registrable sender domain), the model keeps request-type counts. There are no addresses, subjects or bodies, and `forget()` erases everything.

**Model.** P(type | sender) is a Dirichlet posterior with prior strength 4, using a smoothed global or account type distribution as the prior. A sender with fewer than 5 observations gets `ABSTAIN`, and the prior is reported instead.

**Status from a ~95% credible interval, against a 5% cutoff:**

| Status | Condition |
|---|---|
| `UNEXPECTED` | upper bound < 5% |
| `EXPECTED` | lower bound > 5% |
| `UNCERTAIN` | the interval straddles 5% |

A test caught that the first version labeled straddling cases `EXPECTED`. That's now `UNCERTAIN`: 304 of 1,920 synthetic verdicts were relabeled, and no metric changed.

**Feedback ("this was expected").**
- Each feedback adds +2 pseudo-counts under a feedback id. It's idempotent on replay, and one feedback can't overturn sustained history.
- It expires after 180 days.
- `undo()` restores the **exact** previous state (test).

**Advisory only.** No status means safe. High-impact types (PAY, CHANGE_PAYMENT_DETAILS, SUBMIT_CREDENTIALS, SHARE_PERSONAL_DATA) keep `high_impact=True` even when `EXPECTED`. The module imports no policy or trust store, so it can't relax the 4.x rule that credential and payment asks need review.

## Synthetic evaluation (`expectedness_eval.py`; raw: `phase8-8.3-eval.json`)

**Setup.** The run uses seed 11, 40 accounts and 24 senders each.
- **Senders:** each has a relationship class (biller, vendor, employer, friend, bank, SaaS with login flows) with its own request distribution.
- **History:** 30% of senders are sparse (1–4 messages); the rest have 6–40.
- **Test events:** 1,920. A *normal* event is drawn from the relationship; an *anomalous* one is a high-impact type that's implausible for that relationship.

| Metric | Personalized | Unpersonalized global prior |
|---|---|---|
| AUC, all events | **0.981** | 0.917 |
| AUC, non-abstained events | **0.997** | 0.923 |
| AUC, high-impact types only | **0.972** | 0.857 |

Fairness by history size:

| Measure | Sparse senders | Rich senders |
|---|---|---|
| Abstain rate | 100% | 0% |
| False `UNEXPECTED` on normal asks | 0% | 0.45% |
| `UNEXPECTED` recall on anomalous asks | 0% | 75.8% |

Status counts: EXPECTED 530, UNEXPECTED 514, UNCERTAIN 304, ABSTAIN 572.

## Findings

1. **Personalization helps most where it matters: high-impact asks** (AUC 0.97 against 0.86). The same request type is routine for one relationship and alarming for another: credentials from a SaaS login flow versus from a friend, or a payment to a biller versus one requested by an employer. A global prior can't express that.
2. **Fairness is explicit, not free.** Sparse and new senders always abstain, so they're never penalized for lacking history (0% false flags). The flip side is **no recall on anomalous asks from new contacts**. Those must come from the other detectors (auth, intel, 8.2 continuity, policy); this engine adds nothing for first contact.
3. **Prior choice matters (documented limit).** An account-derived prior is dominated by heavy senders. An account that is half bill payments learns P(PAY) ≈ 0.5, and a friend's payment ask stops looking unexpected. A product should use a global or population prior, or a prior computed per relationship class, not the raw account mix. A test asserts this limit.
4. **Uncertainty reporting needs care.** With about 20 observations, a 1.7% estimate still has an interval up to about 7%, so the honest answer is `UNCERTAIN`.

## Prerequisite (blocking for production)

**Request types come from the semantic observer** (Granite `RequestType`), which isn't part of the live pipeline: live assessments have 0 model runs (7.7 dashboard). Without request-type extraction, there's nothing to model. Header and sender evidence are also body-only today (see 8.2).

## Privacy report

| Aspect | Status |
|---|---|
| Opt-in | Per account; off means no state at all |
| Stored data | Per-account HMAC sender keys → request-type counts; feedback ids, keys, types, weights, expiry |
| Not stored | Addresses, display names, subjects, bodies, message ids, timestamps of individual mail |
| Cross-account | None; salts differ per account, and nothing is pooled |
| Deletion | `forget()` erases counts and feedback |
| Re-identification risk | A sender key is linkable *within* the account (by design). Counts can reveal relationship patterns to anyone with account-level access, so it needs the same protection as the trust index (5.4) |
| Reversibility | Each feedback undoes exactly and expires after 180 days |

## Proposed follow-on (needs a separate G8 approval)

1. Wire request-type extraction (Granite observer, in SHADOW) first.
2. Then run this engine per account, opt-in, using a population or relationship-class prior.
3. Emit only `UNEXPECTED` as an advisory review reason.

## Tests

`tests/backend/test_mail_security_research_8_3.py`: **9 passed**. They cover:
- not opted in records nothing;
- personalization separates asks: `UNCERTAIN` at 20 observations, `UNEXPECTED` at 80;
- the documented prior-dominance limit;
- sparse senders abstain;
- keyed counts only, and `forget()`;
- bounded, idempotent, exactly reversible and expiring feedback;
- `EXPECTED` never means safe, and there are no policy or trust imports;
- the synthetic baseline targets;
- determinism.
