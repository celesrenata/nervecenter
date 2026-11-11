# Phase 6 task 6.1: opt-in training data selection

Status: implemented, unwired, synthetic examples only; nothing is trained. Decision NS-ADR-041.

Files: `backend/mail_security/training_selection.py` (new), `backend/mail_security/feedback.py` (`Proposal.assessment_ref`), tests `test_mail_security_training_selection.py` (new) and `test_mail_security_store_boundaries.py` (selection may read feedback; selection imports no intel, memory, model, labels, persistence or storage module).

Done criterion: for each of 9 secret kinds, an example carrying it is excluded, its kind is counted, and neither the secret nor its value appears in any selected text or the manifest. Secrets in the subject also exclude. Benign numbers (order ids, times, non-Luhn digit runs, "password reset" without a value) are not flagged.

Other tests: every consent failure mode (none, other account, other purpose, expired, not yet valid, revoked, source not covered) excludes with its own code; any valid grant suffices and is recorded; labels only from approved same-account proposals, provenance kept, conflicting labels exclude; PII replaced by stable tokens, salt-dependent, repr hides text; exact and near duplicates; near-identical texts with different labels all excluded (poison shape); model-instruction text held out; sender share cap; other-account examples never selected; manifest counts only, status PENDING_HUMAN_REVIEW; bad salt or naive time rejected.

Full suite: 8053 passed, 1 known unrelated failure.

Known limits: regex detectors are English/US-centric (SSN only; no other national ids), will miss novel token formats, and over-exclude some benign text (for example "promo code 2026"). Names and postal addresses are not pseudonymised. The real Takeout dry run (G5 option B) is still pending the owner's export.
