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

## Second pass: label summary and incomplete-reason breakdown (owner approved, 2026-11-11)
Records were held in memory only long enough to reduce labels to counts (no names kept), then discarded; the extracted copy was deleted afterwards. 380 s.

| Labels (weak, provider export) | Count of 91,922 |
| --- | --- |
| INBOX | 90,358 |
| UNREAD | 85,762 |
| OPENED | 5,477 |
| IMPORTANT | 4,977 |
| SENT | 1,144 |
| ARCHIVED | 664 |
| SPAM | 78 |
| DRAFT | 71 |
| STARRED | 22 |
| TRASH | 0 |

Categories: PROMOTIONS 42,363; UPDATES 32,924; SOCIAL 8,810; PERSONAL 4,542; FORUMS 1,738.
Ambiguities: UNKNOWN_CATEGORY 4,743; NO_LABELS 296; no contradictions (no SPAM_AND_INBOX etc.).
Custom labels per message: 0 for 87,160; 1 for 4,747; 2 for 15.
Incomplete reasons: DECODED_BYTES_EXCEEDED 36, ARCHIVE_NOT_INSPECTED 9.

Findings:
- INBOX is on 98% of messages and SPAM on only 78 (0.08%), so provider labels are very weak as security labels, as designed: they must not become training labels.
- 4,743 messages carry a `Category ...` label the normaliser does not know; almost all custom-label hits come from it. Likely a Gmail category added after the normaliser's list; the name has not been inspected.
- No TRASH labels despite the "Including Spam and Trash" export name.
- Incomplete messages are large decoded bodies (36) and attached archives that are deliberately not inspected (9).

## Third pass: unknown category names (owner approved)
Printed only `Category <suffix>` suffixes not in the normaliser's list and matching `[A-Za-z][A-Za-z ]{0,29}`; other shapes counted only. Result: Purchases 3,964; Bills 757; Travel 35; other shapes 0. These are Gmail categories, now added (NS-ADR-043). Extracted copy deleted afterwards.

## Cap raised to 64 MiB (owner decision, NS-ADR-044)
Size-only scan (no parsing): 16 messages above 20 MiB, largest 31.3 MiB; the 9 quarantined ones were 25.7-31.3 MiB.
Dry run again: 91,931 seen, 91,931 records, 0 quarantined, 54 incomplete (DECODED_BYTES_EXCEEDED 45, SOURCE_BYTES_EXCEEDED 9, ARCHIVE_NOT_INSPECTED 9; a message can have several), 386 s, peak RSS 845 MB. Extracted copy deleted.
