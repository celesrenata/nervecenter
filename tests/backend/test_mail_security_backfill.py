"""NerveShield recovery / backfill provider tests (Defect 2).

Live defect: the deployed runner had no ``sources_provider``, so a message whose
job insert was lost in the commit-to-schedule window was never recovered and the
already-ingested messages were never assessed. The provider reconstructs sources
from already-committed communications through a STRICT ``content_ref`` grammar
and the ingest worker's own identity builders, so a backfilled job key is
IDENTICAL to the live seam's key (a later real ingest dedupes against it).

Invariants: R-002, R-018, R-023, I-02, I-05, I-11, T-14, T-15, T-17, T-28.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import _mail_security_harness_helpers as H
import pytest
import structlog
from sqlalchemy import delete, event, select

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.ingest.mail_worker import (
    _communication_id,
    _content_key,
    security_account_scope,
    security_source_identity,
)
from backend.mail_security import telemetry
from backend.mail_security.acquisition import ACQUISITION_META_KEY
from backend.mail_security.backfill import (
    DEFAULT_BATCH_SIZE,
    BackfillSkip,
    CommittedCommunicationSources,
    batch_size_from_env,
    parse_content_ref,
)
from backend.mail_security.identifiers import logical_source_key, outbox_job_key
from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    ENV_MAIL_SECURITY_ENABLED,
)
from backend.mail_security.pipeline_runner import MailSecurityPipelineRunner
from backend.persistence.models.personal import (
    AttentionItemRow,
    CommunicationRow,
    IngestCursorRow,
)

_ON = {ENV_MAIL_SECURITY_ENABLED: "1"}
_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
_DIGEST = "a" * 24
BODY = f"Hello, your parcel is on its way {H.CANARY}.\n".encode()


@pytest.fixture
async def harness(tmp_path):
    h = await H.make_harness(tmp_path / "backfill.sqlite")
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


def _provider(harness: Any, **kw: Any) -> CommittedCommunicationSources:
    return CommittedCommunicationSources(
        connections=harness.registry.connections,
        communications=harness.registry.communications,
        object_store=harness.store,
        jobs=harness.registry.mail_security,
        **kw,
    )


async def _ingest_off(harness: Any, monkeypatch: Any, n: int, *, prefix: str = "m") -> list[str]:
    """Ingest ``n`` messages with the flag OFF (committed, but no job: the lost
    commit-to-schedule window / the pre-existing live backlog)."""
    ids = []
    for i in range(n):
        mid = f"<{prefix}-{i}@example.com>"
        item = dataclasses.replace(H.mailitem(mid, BODY), timestamp=H.TS + timedelta(minutes=i))
        await H.ingest(harness, monkeypatch, item, uid=10 + i, flag_on=False)
        ids.append(mid)
    return ids


async def _attention(harness: Any) -> list[Any]:
    async with harness.registry.communications._sessions() as s:  # noqa: SLF001
        rows = (await s.execute(select(AttentionItemRow))).scalars().all()
    return sorted(r.id for r in rows)


async def _insert_comm(harness: Any, ref: str, *, minutes: int = 0) -> None:
    comms = harness.registry.communications
    person = await comms.create_person(display_name="X")
    await comms.create_communication(
        person_id=person.id, channel="email", direction="incoming",
        content_ref=ref, subject=f"S {H.CANARY}",
        received_at=H.TS + timedelta(minutes=minutes), flags={"bulk": True},
    )


# --------------------------------------------------------------------------
# 1. strict content_ref grammar
# --------------------------------------------------------------------------
def test_parse_accepts_exact_shape() -> None:
    parsed = parse_content_ref(f"content://mail/{_UUID}/{_DIGEST}")
    assert parsed is not None
    assert parsed.connection_id == _UUID
    assert parsed.object_digest == _DIGEST
    assert parsed.object_key == f"mail/{_UUID}/{_DIGEST}"


_HOSTILE = [
    f"content://mail/{_UUID}/../{_DIGEST}",
    f"content://mail/{_UUID}/{_DIGEST}/extra",
    f"content://mail/{_UUID}/x/{_DIGEST}",
    f"content://mail/../{_UUID}/{_DIGEST}",
    f"content://mail//{_UUID}/{_DIGEST}",
    f"s3://mail/{_UUID}/{_DIGEST}",
    f"Content://mail/{_UUID}/{_DIGEST}",
    f"content://Mail/{_UUID}/{_DIGEST}",
    f"content://email/{_UUID}/{_DIGEST}",
    f"content:/mail/{_UUID}/{_DIGEST}",
    f"mail/{_UUID}/{_DIGEST}",
    f"content://mail/{_UUID.upper()}/{_DIGEST}",
    f"content://mail/{_UUID.replace('-', '')}/{_DIGEST}",
    f"content://mail/{_UUID[:-1]}g/{_DIGEST}",
    f"content://mail/{_UUID}0/{_DIGEST}",
    f"content://mail/{{{_UUID}}}/{_DIGEST}",
    f"content://mail/{_UUID}/{_DIGEST[:-1]}",
    f"content://mail/{_UUID}/{_DIGEST}a",
    f"content://mail/{_UUID}/{'A' * 24}",
    f"content://mail/{_UUID}/{'z' * 24}",
    f"content://mail/{_UUID}/{_DIGEST[:-1]}%",
    f"content://mail/{_UUID}/{_DIGEST}\n",
    f"content://mail/{_UUID}/{_DIGEST} ",
    f" content://mail/{_UUID}/{_DIGEST}",
    f"content://mail/{_UUID}/{_DIGEST}?x=1",
    f"content://mail/{_UUID}/{_DIGEST}#f",
    f"content://mail/{_UUID}/%2e%2e",
    f"content://mail/{_UUID}/" + "a" * 5000,
    "content://mail/" + "a" * 100_000,
    "email:abc",
    "",
    None,
    123,
    b"content://mail/x/y",
]


@pytest.mark.parametrize("ref", _HOSTILE)
def test_parse_rejects_hostile_refs(ref: Any) -> None:
    assert parse_content_ref(ref) is None


# --------------------------------------------------------------------------
# 2. parser <-> ingest-worker key builder coupling
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mail_id", ["gmail:18c2f0a1b2c3d4e5", "<a.b+c@example.com>", "imap-INBOX-7"]
)
def test_parser_accepts_every_ref_the_ingest_builder_produces(mail_id: str) -> None:
    """Imports the builder from the ingest code: a future key-format change
    breaks this test instead of silently orphaning refs."""
    conn_id = str(uuid.uuid4())
    key = _content_key(conn_id, mail_id)
    parsed = parse_content_ref(f"content://{key}")
    assert parsed is not None
    assert parsed.connection_id == conn_id
    assert parsed.object_key == key


# --------------------------------------------------------------------------
# 3. key parity with the live seam
# --------------------------------------------------------------------------
async def test_backfilled_key_identical_to_live_seam_key(harness, monkeypatch) -> None:
    mid = "<parity-1@example.com>"
    await H.ingest(harness, monkeypatch, H.mailitem(mid, BODY), flag_on=True)
    live = await harness.registry.mail_security.list_jobs(harness.scope)
    assert len(live) == 1
    # Independently derive what the provider reconstructs for the same ref.
    provider = _provider(harness)
    source = await provider.reconstruct(harness.scope, live[0].meta["content_ref"], {})
    assert source is not None
    sk = logical_source_key(
        harness.scope, provider=source["provider"], provider_uid=source["provider_uid"],
        rfc_message_id=source["rfc_message_id"], folder=source["folder"],
    )
    assert sk == live[0].source_key
    job_key = outbox_job_key(harness.scope, sk, DEFAULT_ANALYSIS_PROFILE_VERSION)
    assert job_key == live[0].outbox_job_key
    # Same identity builder the live seam uses.
    assert {k: source[k] for k in ("provider", "provider_uid", "rfc_message_id", "folder")} == (
        security_source_identity(harness.conn, cursor_kind="imap_seq", mail_id=mid)
    )
    assert security_account_scope(harness.conn.id) == harness.scope
    assert source[ACQUISITION_META_KEY] == "LEGACY_BODY_ONLY"


async def test_later_real_ingest_dedupes_against_backfilled_job(harness, monkeypatch) -> None:
    mid = "<later-1@example.com>"
    await H.ingest(harness, monkeypatch, H.mailitem(mid, BODY), flag_on=False)
    repo = harness.registry.mail_security
    from backend.mail_security.queue import sweep_once

    res = await sweep_once(repo, harness.scope, now=H.CLOCK, sources_provider=_provider(harness))
    assert len(res.created) == 1
    backfilled = await repo.list_jobs(harness.scope)
    assert backfilled[0].meta[ACQUISITION_META_KEY] == "LEGACY_BODY_ONLY"
    # Forget the cursor seen-list so the live worker re-delivers the message.
    async with harness.registry.communications._sessions() as s:  # noqa: SLF001
        await s.execute(delete(IngestCursorRow))
        await s.commit()
    from backend.persistence.repositories.mail_security_repo import MailSecurityRepository

    live_keys: list[str] = []
    real_upsert = MailSecurityRepository.upsert_job

    async def spy(self: Any, **kw: Any) -> Any:
        live_keys.append(kw["outbox_job_key"])
        return await real_upsert(self, **kw)

    monkeypatch.setattr(MailSecurityRepository, "upsert_job", spy)
    await H.ingest(harness, monkeypatch, H.mailitem(mid, BODY), flag_on=True)
    # The live seam really ran, with the backfilled key.
    assert live_keys == [backfilled[0].outbox_job_key]
    after = await repo.list_jobs(harness.scope)
    assert [j.id for j in after] == [backfilled[0].id]
    assert after[0].outbox_job_key == backfilled[0].outbox_job_key


# --------------------------------------------------------------------------
# 4. bounds, ordering, idempotency, skips
# --------------------------------------------------------------------------
async def test_batch_bound_newest_first_and_idempotent(harness, monkeypatch) -> None:
    mids = await _ingest_off(harness, monkeypatch, 5)
    provider = _provider(harness, batch_size=2)
    repo = harness.registry.mail_security
    from backend.mail_security.queue import sweep_once

    expected_refs = [
        f"content://{_content_key(harness.conn.id, m)}" for m in reversed(mids)
    ]
    seen: list[str] = []
    for _ in range(3):
        provider.begin_pass()
        await sweep_once(repo, harness.scope, now=H.CLOCK, sources_provider=provider)
        refs = [j.meta["content_ref"] for j in await repo.list_jobs(harness.scope)]
        new = [r for r in refs if r not in seen]
        assert len(new) <= 2
        seen.extend(sorted(new, key=expected_refs.index))
    assert seen == expected_refs  # newest first, 2 + 2 + 1
    # Repeated passes create nothing more (idempotent).
    for _ in range(2):
        provider.begin_pass()
        res = await sweep_once(repo, harness.scope, now=H.CLOCK, sources_provider=provider)
        assert res.created == []
    assert len(await repo.list_jobs(harness.scope)) == 5
    assert provider.skip_counts[BackfillSkip.ALREADY_SCHEDULED] >= 5


async def test_budget_is_per_pass_across_scopes(harness, monkeypatch) -> None:
    await _ingest_off(harness, monkeypatch, 3)
    provider = _provider(harness, batch_size=2)
    provider.begin_pass()
    first = await provider(harness.scope)
    second = await provider(harness.scope)
    assert len(first) == 2
    assert second == []  # pass budget exhausted


def test_default_batch_is_small() -> None:
    assert 1 <= DEFAULT_BATCH_SIZE <= 50


async def test_already_scheduled_is_skipped(harness, monkeypatch) -> None:
    await H.ingest(harness, monkeypatch, H.mailitem("<live-a@example.com>", BODY), flag_on=True)
    provider = _provider(harness)
    assert await provider(harness.scope) == []
    assert provider.skip_counts[BackfillSkip.ALREADY_SCHEDULED] == 1


async def test_unknown_disabled_and_foreign_scopes_skipped(harness, monkeypatch) -> None:
    await _ingest_off(harness, monkeypatch, 1)
    provider = _provider(harness)
    assert await provider(f"conn:{uuid.uuid4()}") == []
    assert provider.skip_counts[BackfillSkip.CONNECTION_UNKNOWN] == 1
    assert await provider("src:api") == []
    assert await provider("conn:not-a-uuid") == []
    assert provider.skip_counts[BackfillSkip.NON_CONNECTION_SCOPE] == 2
    await harness.registry.connections.update_connection(harness.conn.id, enabled=False)
    assert await provider(harness.scope) == []
    assert provider.skip_counts[BackfillSkip.CONNECTION_DISABLED] == 1
    assert await provider.scopes() == []


async def test_hostile_and_unbound_refs_in_scope_are_skipped(harness, monkeypatch) -> None:
    cid = harness.conn.id
    await _insert_comm(harness, f"content://mail/{cid}/../{_DIGEST}")
    await _insert_comm(harness, f"content://mail/{cid}/{_DIGEST}/x")
    # Well-formed ref with no stored object behind it.
    await _insert_comm(harness, f"content://mail/{cid}/{'b' * 24}")
    # Object exists but its recorded message id does not hash to the ref digest.
    forged = f"mail/{cid}/{'c' * 24}"
    await harness.store.put(forged, b"x", metadata={"message_id": "<other@x>"})
    await _insert_comm(harness, f"content://{forged}")
    # Object exists but carries no usable message id.
    blank = f"mail/{cid}/{'d' * 24}"
    await harness.store.put(blank, b"x", metadata={"message_id": "a b\n"})
    await _insert_comm(harness, f"content://{blank}")
    provider = _provider(harness)
    assert await provider(harness.scope) == []
    counts = provider.skip_counts
    assert counts[BackfillSkip.REF_REJECTED] == 2
    assert counts[BackfillSkip.OBJECT_MISSING] == 1
    assert counts[BackfillSkip.IDENTITY_MISMATCH] == 1
    assert counts[BackfillSkip.IDENTITY_UNAVAILABLE] == 1


async def test_other_connection_refs_never_read(harness, monkeypatch) -> None:
    """R-023: a scope only ever sees refs under its own connection prefix."""
    other = await harness.registry.connections.create_connection(
        kind="imap", config={"mailbox": "INBOX"}, enabled=True,
    )
    key = _content_key(other.id, "<o@x>")
    await harness.store.put(key, BODY, metadata={"message_id": "<o@x>"})
    await _insert_comm(harness, f"content://{key}")
    provider = _provider(harness)
    assert await provider(harness.scope) == []
    srcs = await provider(f"conn:{other.id}")
    assert [s["content_ref"] for s in srcs] == [f"content://{key}"]
    assert sorted(await provider.scopes()) == sorted([harness.scope, f"conn:{other.id}"])


# --------------------------------------------------------------------------
# 5. privacy: identifiers + flags only, no content read or emitted
# --------------------------------------------------------------------------
async def test_provider_reads_no_content_and_emits_no_canary(harness, monkeypatch) -> None:
    await _ingest_off(harness, monkeypatch, 2)

    class NoGetStore:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def get(self, key: str) -> bytes:
            raise AssertionError("backfill must never read object content")

        async def head(self, key: str) -> Any:
            return await self._inner.head(key)

    statements: list[str] = []

    def capture(_c, _cur, statement, *_a) -> None:
        statements.append(statement.lower())

    event.listen(harness.engine.sync_engine, "before_cursor_execute", capture)
    provider = CommittedCommunicationSources(
        connections=harness.registry.connections,
        communications=harness.registry.communications,
        object_store=NoGetStore(harness.store),
        jobs=harness.registry.mail_security,
    )
    with structlog.testing.capture_logs() as logs:
        provider.begin_pass()
        srcs = await provider(harness.scope)
    event.remove(harness.engine.sync_engine, "before_cursor_execute", capture)
    assert len(srcs) == 2
    comm_reads = [s for s in statements if "from communications" in s]
    assert comm_reads
    assert all("subject" not in s for s in comm_reads)
    blob = json.dumps(srcs, default=str) + json.dumps(logs, default=str)
    assert H.CANARY not in blob
    assert H.ACCOUNT not in blob
    for s in srcs:
        assert set(s) == {
            "provider", "provider_uid", "rfc_message_id", "folder",
            "content_ref", "flags", ACQUISITION_META_KEY,
        }
        assert all(isinstance(v, bool) for v in s["flags"].values())


# --------------------------------------------------------------------------
# 6. runner: flag-off inert; end-to-end backfill
# --------------------------------------------------------------------------
class _ExplodingProvider:
    def begin_pass(self) -> None:
        raise AssertionError("flag OFF must not touch the provider")

    async def scopes(self) -> list[str]:
        raise AssertionError("flag OFF must not touch the provider")

    async def __call__(self, scope: str) -> list[Any]:
        raise AssertionError("flag OFF must not touch the provider")


async def test_flag_off_runner_never_touches_provider(harness) -> None:
    runner = MailSecurityPipelineRunner(
        repository=harness.registry.mail_security,
        worker_factory=lambda s: H.security_worker(harness),
        sources_provider=_ExplodingProvider(),
        env={ENV_MAIL_SECURITY_ENABLED: "0"},
    )
    assert await runner.run_once() is None
    assert runner.start() is None


async def test_failing_scope_listing_is_counted_and_isolated(harness, monkeypatch) -> None:
    from backend.mail_security.pipeline_runner import RunnerFailure

    await H.ingest(harness, monkeypatch, H.mailitem("<iso-1@example.com>", BODY), flag_on=True)

    class Broken:
        def begin_pass(self) -> None:
            pass

        async def scopes(self) -> list[str]:
            raise RuntimeError(H.CANARY)

        async def __call__(self, scope: str) -> list[Any]:
            return []

    runner = MailSecurityPipelineRunner(
        repository=harness.registry.mail_security,
        worker_factory=lambda s: H.security_worker(harness),
        sources_provider=Broken(),
        clock=lambda: H.CLOCK,
        env=_ON,
    )
    with structlog.testing.capture_logs() as logs:
        assert await runner.run_once() == [harness.scope]
    assert runner.failure_counts[RunnerFailure.BACKFILL_SCOPES_FAILED] == 1
    assert H.CANARY not in json.dumps(logs, default=str)
    assert len(await harness.registry.mail_security.list_assessments(harness.scope)) == 1


async def test_wiring_flag_on_passes_backfill_provider(harness, monkeypatch) -> None:
    from backend.api import wiring

    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
    monkeypatch.setenv("NERVE_MAIL_SECURITY_BACKFILL_BATCH", "7")
    monkeypatch.setattr(wiring, "_mail_security_object_store", lambda: harness.store)
    target = SimpleNamespace(state=SimpleNamespace(repository_registry=harness.registry))
    task = wiring.start_mail_security_runner(target)
    try:
        assert task is not None
        sources = target.state.mail_security_runner._sources  # noqa: SLF001
        assert isinstance(sources, CommittedCommunicationSources)
        assert sources._batch == 7  # noqa: SLF001
    finally:
        await wiring.stop_mail_security_runner(target)


async def test_wiring_flag_off_builds_no_provider(harness, monkeypatch) -> None:
    from backend.api import wiring
    from backend.mail_security import backfill

    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "0")
    monkeypatch.setattr(
        backfill, "CommittedCommunicationSources",
        lambda *a, **k: pytest.fail("provider built with flag off"),
    )
    monkeypatch.setattr(wiring, "_mail_security_object_store", lambda: harness.store)
    target = SimpleNamespace(state=SimpleNamespace(repository_registry=harness.registry))
    assert wiring.start_mail_security_runner(target) is None
    assert not hasattr(target.state, "mail_security_runner")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", DEFAULT_BATCH_SIZE), ("x", DEFAULT_BATCH_SIZE), ("0", 1), ("10", 10), ("99999", 500)],
)
def test_batch_size_from_env(raw: str, expected: int) -> None:
    assert batch_size_from_env({"NERVE_MAIL_SECURITY_BACKFILL_BATCH": raw}) == expected


async def test_end_to_end_runner_backfills_three_preexisting(harness, monkeypatch) -> None:
    await _ingest_off(harness, monkeypatch, 3)
    repo = harness.registry.mail_security
    assert await repo.list_jobs(harness.scope) == []
    attention_before = await _attention(harness)
    async with harness.registry.communications._sessions() as s:  # noqa: SLF001
        comm_ids = sorted((await s.execute(select(CommunicationRow.id))).scalars().all())

    provider = _provider(harness)
    done = asyncio.Event()
    passes = 0

    async def sleep(_: float) -> None:
        nonlocal passes
        passes += 1
        if passes >= 2:
            done.set()
        await asyncio.sleep(0)

    runner = MailSecurityPipelineRunner(
        repository=repo,
        worker_factory=lambda scope: H.security_worker(harness),
        sources_provider=provider,
        interval_seconds=5.0,
        clock=lambda: H.CLOCK,
        sleep=sleep,
        env=_ON,
    )
    assert runner.start() is not None
    await asyncio.wait_for(done.wait(), timeout=10)
    await runner.stop()

    jobs = await repo.list_jobs(harness.scope)
    assert len(jobs) == 3
    assert {j.state for j in jobs} == {"DONE"}
    assert {j.meta[ACQUISITION_META_KEY] for j in jobs} == {"LEGACY_BODY_ONLY"}
    rows = await repo.list_assessments(harness.scope)
    assert len(rows) == 3
    assert {r.processing_state for r in rows} == {"ASSESSED"}
    assert {r.evidence_fidelity for r in rows} == {"BODY_ONLY"}
    assert all(r.disposition != "SAFE_TO_PROCESS" for r in rows)
    # Communications map 1:1 to the backfilled refs (I-11, no duplicates).
    expected = sorted(_communication_id(harness.conn.id, f"<m-{i}@example.com>") for i in range(3))
    assert comm_ids == expected
    assert await _attention(harness) == attention_before
    assert runner.failure_counts and not any(runner.failure_counts.values())
    blob = json.dumps([r.assessment for r in rows]) + json.dumps([j.meta for j in jobs])
    assert H.CANARY not in blob
