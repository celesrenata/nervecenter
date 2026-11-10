# Phase 5 task 5.1: offline opt-in MBOX/EML import

Status: implemented, unwired, synthetic fixtures only. No real Takeout archive was read, nothing reached any model, and nothing changed on the cluster.

Files: `backend/mail_security/takeout_import.py`, `tests/backend/test_mail_security_takeout_import.py`. Decision NS-ADR-036.

Covered by tests (64, network disabled with socket patched to raise):
- Two-account import: provenance per record, in-account duplicate collapsed, identical message kept in both accounts with distinct logical keys, cross-account duplicate counts, missing Message-ID handled.
- Exact bytes and offsets; mboxrd unquoting; unquoted `From ` in a body (mboxo) does not split; `From ` at a read-chunk boundary does not split.
- Dry run is the default and never calls the sink; dry-run and real manifests agree.
- Unselected account never opened (os.open spy); invalid account scopes refused.
- Archive policy: outside roots, `..` traversal, symlinked file, symlinked directory component, compressed archives (zip, gz, tgz, zst), wrong suffix, missing path, directory named `.mbox`, file-size cap before reading.
- Caps: oversized message quarantined and the stream continues; long line without newline; message-count cap (LIMIT_REACHED); leading junk; blank message; MIME parser limits mark records incomplete without dropping them.
- EML: directory walk with depth cap, symlinks skipped, non-`.eml` files ignored, oversize quarantined, file-count cap, single file.
- Cancel stops the import mid-source.
- Manifest JSON contains no subject, address, label, Message-ID or path; record repr hides raw bytes and labels.
- 25 random-byte mbox fuzz cases never raise.
- Static: no other backend module imports the importer.

Full suite: 7828 passed, 1 known unrelated failure (`test_routines` date test).

Not done (later tasks): checkpoint/resume, persisted quarantine and evidence storage (5.2), label normalisation (5.3), API endpoint, HISTORICAL_SHADOW policy wiring. Not measured: throughput on a real multi-GB Takeout file.
