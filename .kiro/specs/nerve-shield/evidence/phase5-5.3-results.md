# Phase 5 task 5.3: weak Gmail label normalisation

Status: implemented, unwired, synthetic label strings only. Decision NS-ADR-038.

Files: `backend/mail_security/takeout_labels.py`, `tests/backend/test_mail_security_takeout_labels.py`; the 5.1 "not wired" check now matches import statements only (it was tripping on a docstring mention).

Tests (77): system labels, categories and custom labels in mixed case and spacing; quoted labels containing commas and escaped quotes; every ambiguity code (Spam+Inbox, Trash+Inbox, Spam+Sent, Draft+Sent, multiple categories, unbalanced quotes, encoded words, empty, unknown category, localised names, control characters, too long, too many, truncated, absent); contradictions keep both labels; localised names and look-alikes stay custom; custom names hidden from repr and code projection; salted digests (salt-dependent, case-insensitive, short salt rejected); no verdict-shaped fields; 40 random-string fuzz cases; per-account summary has counts only; end-to-end on records from the 5.1 importer; static not-wired check.

Full suite: 7948 passed, 1 known unrelated failure.

Not done: decoding RFC 2047 label names (flagged instead), a locale-aware mapping (needs owner input on export languages), any use of labels as training data (Phase 6, needs review).
