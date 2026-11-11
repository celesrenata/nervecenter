# Phase 5 task 5.5: store boundaries, feedback sources and provenance

Status: implemented, unwired, synthetic records only. Decision NS-ADR-040.

Files: `backend/mail_security/feedback.py`, `tests/backend/test_mail_security_store_boundaries.py`.

Done criterion ("feedback does not change model weights or global indicators automatically"):
- Runtime: every feedback kind, recorded by an analyst asking for GLOBAL scope with consent, and every resulting pending proposal approved, leaves the classifier source tree byte-identical (sha256 over all files) and the trust index state unchanged.
- Static: the feedback module imports no intel, personal-memory, training or model module, so no code path from feedback to those stores exists. Reverse directions are checked too (intel store/repo, trust index, classifier).
- Approval yields status APPROVED only; no executor exists yet.

Other tests: routing per kind; NOT_SURE routes nowhere; intel needs indicator refs; GLOBAL scope only for analyst requests and analyst approvers; weak sources refused; free text, URLs, naive timestamps and bad refs refused; idempotent feedback ids; supersede with retained history; blocked training cannot be approved; cross-account listing and decisions denied.

Removed during review: a spy-repository assertion that nothing called (it proved nothing) and a field-name overlap check (`first_seen` exists in both stores by coincidence, not a leak).

Full suite: 8011 passed, 1 known unrelated failure.

Not done: the executor that applies approved proposals, persistence of the ledger (`mail_security_feedback` table), the feedback API endpoint.
