# Phase 5 task 5.2: checkpoint, restart, cancel, idempotency, quarantine

Status: implemented, unwired, synthetic fixtures only. No real Takeout archive was read, nothing reached any model, nothing changed on the cluster. Decision NS-ADR-037.

Files: `backend/mail_security/takeout_job.py` (new), `backend/mail_security/takeout_import.py` (resume positions, shared `_accept`, counting/cancel fix, symlinked-root fix), tests `tests/backend/test_mail_security_takeout_job.py` (new) and `test_mail_security_takeout_import.py`.

Fixture: two accounts; account A has an mbox (8 messages: in-account duplicate, oversize, empty, no Message-ID, one message shared with B) plus an EML directory (duplicate of an mbox message, one oversize); account B has an mbox with the shared message.

Crash/resume evidence (each resumed run compared with an uninterrupted run: manifest, quarantine, evidence entry keys, raw objects byte for byte):

| Injection | Points exercised |
| --- | --- |
| Crash before checkpoint write | 19 / 11 / 5 (checkpoint every 1 / 2 / 5) |
| Crash after checkpoint write | 19 / 11 / 5 |
| Crash at evidence-sink write | 10 (every 1 and every 3) |
| Crash at raw-store write | 10 |
| Cancel | 18 points |

All matched. Raw objects are written exactly once per account and digest; a finished job re-run does no work.

Fail-closed resumes, each with no further writes: source file appended (SOURCE_CHANGED), EML file added (SOURCE_CHANGED), selected accounts changed or caps changed (JOB_MISMATCH), version / type / range / job-id / account / position / kind corruption, unparseable state, offset moved off a separator (source refused, CHECKPOINT_MISMATCH).

Other checks: raw capture is off by default and entries say METADATA_ONLY + RAW_NOT_RETAINED; when allowed, stored bytes are exact; dry run writes nothing; file checkpoint store is `0600`, leaves no temp files, keeps the previous state when a write fails, rejects path-like job ids, and contains no subjects, addresses, labels, Message-IDs or paths; quarantine is bounded and holds only (source_index, position, code).

Defects found while building and fixed: messages were counted before being processed (resume would double count); cancel could land between read and processing; EML walk issues would be recounted on resume; an allowed root reached through a symlink (macOS `/var`) was always refused.

Full suite: 7871 passed, 1 known unrelated failure (`test_routines` date test).

Not done: real-archive throughput, storage of the quarantine list outside the checkpoint, any API/worker wiring, encryption for raw capture (blocked on the open object-store decision).
