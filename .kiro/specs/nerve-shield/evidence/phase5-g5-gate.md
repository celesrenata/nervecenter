# Phase 5 — G5 Gate summary (owner-facing) — STOP, decision requested

**Status: STOPPED AT G5. Phase 6 has NOT been started and will not start without an explicit owner decision recorded below.**
**Gate text (tasks.md 5.6):** "Local import demonstrably safe, resumable, account-isolated and non-actionable; approve Phase 6."
**Scope run:** tasks 5.1 to 5.5, synthetic fixtures only. No real Takeout archive was read, no real mail reached any model, nothing is wired into the pipeline, API or cluster.
**Legend:** VERIFIED = observed by the orchestrator (full suite re-run, targeted counts). NOT MET = not done.

## Verdict in one paragraph
The local import path exists as libraries and is tested hard on synthetic data: bounded streaming with no archive extraction, crash-and-resume equivalence at every checkpoint and write, account isolation in import, labels, trust index and feedback, and no path from feedback to model weights or global intel. It has not touched a real Takeout export, has no API or worker, and raw evidence capture stays off pending the object-store decision.

## Checklist
| Gate property | Status | Evidence |
| --- | --- | --- |
| Safe | **VERIFIED on fixtures** | Allowed roots, no symlinks below the root, `O_NOFOLLOW`, `.mbox`/`.eml` only, compressed archives refused (no extraction code), line/message/count/file caps, bounded MIME parser, fuzz cases, network disabled in tests (5.1). |
| Resumable | **VERIFIED on fixtures** | Crash before/after every checkpoint (19/11/5 points), at every evidence and raw write (10 each), cancel at 18 points: resumed result identical to an uninterrupted run; fail closed on file, listing, request, caps or state drift (5.2). |
| Account-isolated | **VERIFIED on fixtures** | Unselected accounts never opened; per-account dedup, distinct logical keys for shared bytes (5.1); per-account HMAC keys in the trust index, revoke denies and deletes (5.4); feedback listing and decisions account-checked (5.5). |
| Non-actionable | **VERIFIED** | Labels are WEAK provider hints with no verdict field (5.3); trust features always carry FAMILIARITY_IS_NOT_VERIFICATION (5.4); feedback only produces proposals; approval applies nothing (5.5). No module is wired. |

Full suite: **8011 passed, 1 failed** (the known date-dependent `test_routines.py::TestWellnessSleepSeam` test, unrelated).

Commits (local only): `9c7bfce` (5.1), `e45df97` (5.2), `374f899` (5.3), `95bde6e` (5.4), `9043883` (5.5).

## What is NOT proven or not done
1. No real Takeout export has been read. Throughput and memory on a multi-GB mbox are unmeasured.
2. Raw evidence capture is off: the object store is plain HTTP and shared. Entries record METADATA_ONLY + RAW_NOT_RETAINED.
3. No API endpoint, worker, persistence tables (feedback ledger, trust index store) or HISTORICAL_SHADOW policy wiring.
4. Spec deviation (5.4): the trust index is separate from `backend.knowledge` because that graph has no account namespace. Owner to confirm.
5. Changed payment endpoint tracking (design section 1) is not built.
6. Localised Gmail system label names are flagged, not mapped (export locale unknown).
7. Carried over: endpoint authentication on ArcForge/Granite hosts undecided; OmniRoute memory-write absence on secure routes unproven.

## Decisions requested
1. Confirm the 5.4 deviation (separate trust index) or ask for a graph-backed design.
2. Raw evidence capture: keep off, or decide object-store encryption first.
3. Next step:
   - **A. Approve Phase 6** (privacy-preserving training lifecycle), starting with 6.1 on synthetic data only.
   - **B. Prove Phase 5 on a real export first**: a dry run (manifest only, no records kept, no model) on one Takeout mbox you choose, to measure throughput and check real-world parsing.
   - **C. Pause** here.

My recommendation is B before A: Phase 6 builds datasets from imported data, and the importer has never seen real Takeout output.

## Owner decision (to be completed by owner)
```
Decision:
Date:
Notes:
```
