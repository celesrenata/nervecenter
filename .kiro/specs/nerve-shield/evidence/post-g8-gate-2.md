# Post-G8 gate 2: static inspection G1 and remaining open gates

Date: 2026-11-13. Owner: "reassess, then move on to the next gate". No gate is defined past the follow-on specs, so this reviews everything still open and records what each one needs.

## 1. Static deep inspection G1: PASS (NS-ADR-087)

The owner's "reassess" was taken as keeping it enabled.
- **Re-assessment:** `reassess_flagged --with-documents --apply` re-queued 18 messages as the next revision: the 9 with PDF/OOXML/ZIP documents plus the 9 REVIEW messages the tool always includes. All finished within 110 s.
- **Result:** REVIEW **9 → 10**. The one addition is the predicted document-link `ASCII_BRAND_TYPOSQUAT` (MEDIUM, URL_DECEPTION). There were no structural codes, no incompletes and no SAFE_TO_PROCESS.
- **Side effect handled:** re-assessed revisions are re-observed, and expectedness would have counted them twice. `seed_expectedness` now keeps only the newest COMPLETED revision per message. A rebuild gave 148 counts across 76 senders, with no double counting.
- Final dispositions: 338 LOW_PRIORITY, 10 REVIEW, 1 INCOMPLETE.

## 2. G8 follow-on decision 2 (owner: "dunno"): data

The question: may the SHADOW signals feed the `REVIEW_GATED` promotion gate? Counterfactual on all live mail:

| Signal | Would newly gate today |
|---|---|
| Thread continuity BROKEN/SUSPECT | **0**: inbound ledger is 12 NOT_A_REPLY, 1 CONTINUOUS, 1 UNKNOWN; never BROKEN or SUSPECT |
| CAMPAIGN_OF_FLAGGED | **0**: 40 live campaigns of size 2 or more (max 14), but no LOW_PRIORITY member shares a campaign with a flagged message |
| UNEXPECTED_HIGH_IMPACT_ASK | **0** |

Turning them on changes nothing today. Their false-positive rate is unmeasured, because none has ever fired. The gate only holds a message for review (it never acts), so the downside of turning them on is extra review items, not lost mail.

Options:
- **(a)** Keep SHADOW. Same as now.
- **(b)** Allow them as advisory gate reasons. When a reason fires, a held item shows it, and you can release it.
- **(c)** Decide after the first real firing.

## 3. Still open, and what each needs

| Gate / item | Status | Needs |
|---|---|---|
| Task 3.6 calibration (G3 residual) | **Blocked on labels**: 0 `mail_security_feedback` rows, 0 expectedness feedback rows | Owner labels through the existing review UI. Even 60 labels only allow a sanity check, not calibration; scores stay UNCALIBRATED until then |
| G8 follow-on decision 2 | Data above | Owner picks (a), (b) or (c) |
| SMTP ingress D-3 | G0 STOP, recommended to close | Close the spec, or keep option B with a re-open trigger |
| DNS (NS-ADR-084) | Drafted, not applied | Owner applies the Route 53 batches |
| Detonation | Superseded by static inspection (NS-ADR-085/086) | Close the spec, unless you want it kept as a future option |
| Observer residuals (NS-ADR-080) | 14 phishing labels and the doc-link typosquat unreviewed | Owner glance at the 10 REVIEW items, which also produces labels for 3.6 |
