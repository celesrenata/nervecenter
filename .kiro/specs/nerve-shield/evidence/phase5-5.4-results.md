# Phase 5 task 5.4: privacy-scoped personal trust / relationship index

Status: implemented, unwired, synthetic addresses only. Decision NS-ADR-039.

Design deviation to review: the spec says "using existing graph/retrieval". The existing `backend.knowledge` graph is global (no account namespace), so the index is a separate account-scoped store and does not write to the graph. A test asserts it imports neither `backend.knowledge` nor `backend.persistence`.

Tests (34): established/none/sparse history and configurable threshold; dormant sender; unaligned auth history; new sender at a known domain; reply to a known thread by a participant; thread-hijack shape (known thread, sender new to it); reply to unknown thread; cross-account invisibility of history and threads; unrelated digests for the same address and thread across accounts; every operation denied for an unallowed account; revoke denies reads, writes and re-allow, deletes by default, can keep data while blocking; revocation survives export/import; forget sender removes history, threads and the domain hint; delete account; export holds no address, name, domain, Message-ID; round trip equal; wrong master secret reads nothing; invalid sender and naive timestamps store nothing; address and References parsing; static not-wired and not-touching-graph checks.

Full suite: 7982 passed, 1 known unrelated failure.

Not done: changed payment endpoint tracking (design section 1 item 3; needs a payment-identifier extractor), feeding it from 5.2 evidence entries, persistence in a protected store, and the opaque link to knowledge entities.
