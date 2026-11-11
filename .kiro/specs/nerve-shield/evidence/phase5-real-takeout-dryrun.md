# Phase 5: real Takeout dry run (G5 option B)

Date: 2026-11-11. Owner-supplied export `takeout-20261111T014130Z-{1,2}-001.tgz` in the repo root.
Mode: `run_import(..., dry_run=True)` (no sink, no records kept, no raw store, no model), network blocked in-process, local Mac only. Only counts are recorded here.

## Handling
- Both archives added to `.git/info/exclude` (`/takeout-*`, `/Takeout/`) so they cannot be committed; they were untracked and unignored before.
- Archive contents listed first (names and sizes only): part 1 = Calendar, Contacts, Fit, Account, Timeline (not touched); part 2 = `Takeout/Mail/All mail Including Spam and Trash.mbox` and `Signatures.json`. No absolute paths, `..` entries or links in either.
- Only the mbox was extracted, to a fresh `0700` directory under the per-user `$TMPDIR`, then deleted after the run. The tgz files are left in place for the owner.

## Results
| Metric | Value |
| --- | --- |
| mbox size | 7,447,294,689 bytes |
| Status | COMPLETED |
| Messages seen | 91,931 |
| Records (would be kept) | 91,922 |
| Quarantined: MESSAGE_TOO_LARGE (> 25 MiB) | 9 |
| Duplicates within the account | 0 |
| Inspection incomplete (parser limits) | 45 |
| With X-Gmail-Labels | 91,626 (296 without) |
| Without Message-ID | 0 |
| Wall time | 415.5 s (17.9 MB/s, 221 msgs/s), CPU-bound (407 s user) |
| Peak RSS | 618 MB |

## Findings
1. The importer handles a real 7.4 GB Gmail export end to end with no parse failures, no leading junk and no separator problems.
2. 9 messages exceed the 25 MiB per-message cap and would be quarantined; Gmail allows ~25 MB attachments plus MIME overhead, so the cap may need to be raised (owner decision) or these stay quarantined.
3. The manifest says 45 messages hit a parser limit but not which limit. A counts-only breakdown by `IncompleteReason` would help; not added yet.
4. Throughput is parser-bound at ~18 MB/s: a full real import of this account takes about 7 minutes per pass.
5. Peak memory 618 MB, likely from the largest messages being parsed whole; acceptable on the Mac, worth bounding further before running in a pod.

Not done: label summary (needs records, which the dry-run conditions exclude), any storage, the other services in part 1.
