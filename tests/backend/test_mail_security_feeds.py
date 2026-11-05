"""Tests for the NerveShield feed adapters (Phase 2, Task 2.3).

Offline only: a fake fetcher and synthetic fixtures; SQLite scratch DBs. No
real feed or network is exercised. Invariants: R-006.1/.3, R-017.3, R-022.5 /
T-27 (integrity + license), T-19 (poisoning / staleness). T-09 (SSRF) is not
applicable here (no network client; egress policy is Task 2.5).
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import inspect
import json
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.mail_security import feeds
from backend.mail_security.feeds import (
    FEED_SCHEMA,
    FeedConfig,
    FeedErrorCode,
    FeedFormat,
    FeedRegistry,
    FetchedFeed,
    IntegrityPin,
    IntegrityScheme,
    RunStatus,
    _json_depth_ok,
    ingest_feed,
    parse_payload,
    validate_config,
)
from backend.mail_security.intel import MAX_TTL, IndicatorType
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailIntelIndicatorRow,
    MailIntelOverrideRow,
)
from backend.persistence.repositories.mail_intel_repo import MailIntelRepository

_TABLES = [MailIntelIndicatorRow.__table__, MailIntelOverrideRow.__table__]
_CANARY = "CANARY-SECRET-dont-persist-me-9f3b2a"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
ON = {"NERVE_MAIL_SECURITY_ENABLED": "1"}
HMAC_KEY = b"synthetic-test-key"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _lines(*domains: str) -> bytes:
    return ("# synthetic fixture\n" + "\n".join(domains) + "\n").encode()


def _json(indicators, **over) -> bytes:
    doc = {
        "schema": FEED_SCHEMA,
        "source": "feed:alpha",
        "license": "CC0-1.0",
        "indicators": indicators,
    }
    doc.update(over)
    return json.dumps(doc).encode()


def _cfg(payload: bytes | None = None, **over) -> FeedConfig:
    base = {
        "source": "feed:alpha",
        "license": "CC0-1.0",
        "format": FeedFormat.DOMAIN_LINES,
        "integrity": IntegrityPin(IntegrityScheme.SHA256_PIN, _sha(payload or b"")),
        "max_payload_bytes": 4096,
        "max_records": 50,
        "ttl": timedelta(days=7),
        "enabled": True,
    }
    base.update(over)
    return FeedConfig(**base)


class FakeFetcher:
    def __init__(self, payload: bytes | None = None, *, exc: BaseException | None = None, **kw):
        self.payload = payload
        self.exc = exc
        self.kw = kw
        self.calls: list[tuple] = []

    async def __call__(self, source: str) -> FetchedFeed:
        self.calls.append((source,))
        if self.exc is not None:
            raise self.exc
        assert self.payload is not None
        return FetchedFeed(self.payload, **{"tls_verified": True, **self.kw})


async def _make(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'feeds.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(_TABLES[0].metadata.create_all, tables=_TABLES)
    clock = Clock(T0)
    return engine, MailIntelRepository(make_session_factory(engine), clock=clock), clock


async def _run(cfg, fetcher, repo, clock, **kw):
    # Initial loads need an explicit approval ref; default it so unrelated tests
    # stay focused. Pass initial_load_approval_ref=None to exercise the guard.
    kw.setdefault("initial_load_approval_ref", "owner-approved-initial-1")
    return await ingest_feed(cfg, fetcher, repo, now=clock, env=kw.pop("env", ON), **kw)


async def _rowcount(engine) -> int:
    async with make_session_factory(engine)() as s:
        return len((await s.execute(select(MailIntelIndicatorRow))).scalars().all())


GOOD = _lines("evil-one.example", "evil-two.example", "evil-three.example")


# -- disabled by default / config --------------------------------------------


def test_feed_config_defaults_disabled():
    cfg = FeedConfig(
        source="feed:alpha",
        license="CC0-1.0",
        format=FeedFormat.DOMAIN_LINES,
        integrity=None,
        max_payload_bytes=10,
        max_records=1,
        ttl=timedelta(days=1),
    )
    assert cfg.enabled is False and cfg.require_tls is True


@pytest.mark.asyncio
async def test_disabled_flag_or_master_off_refuses_and_ingests_nothing(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    f = FakeFetcher(GOOD)
    r = await _run(_cfg(GOOD, enabled=False), f, repo, clock)
    assert (r.status, r.code) == (RunStatus.REFUSED, FeedErrorCode.FEED_DISABLED)
    r = await _run(_cfg(GOOD), f, repo, clock, env={})
    assert (r.status, r.code) == (RunStatus.REFUSED, FeedErrorCode.MASTER_FLAG_OFF)
    r = await _run(_cfg(GOOD), f, repo, clock, env={"NERVE_MAIL_SECURITY_ENABLED": "0"})
    assert r.code is FeedErrorCode.MASTER_FLAG_OFF
    assert f.calls == [] and await _rowcount(engine) == 0  # fetcher never invoked
    await engine.dispose()


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"license": ""}, FeedErrorCode.MISSING_LICENSE),
        ({"license": "Proprietary"}, FeedErrorCode.LICENSE_NOT_ALLOWED),
        ({"integrity": None}, FeedErrorCode.MISSING_INTEGRITY),
        (
            {"integrity": IntegrityPin(IntegrityScheme.SHA256_PIN, "abc")},
            FeedErrorCode.MISSING_INTEGRITY,
        ),
        ({"integrity": IntegrityPin(IntegrityScheme.HMAC_SHA256)}, FeedErrorCode.MISSING_INTEGRITY),
        ({"source": "bad source!"}, FeedErrorCode.CONFIG_INVALID),
        ({"max_payload_bytes": 10**9}, FeedErrorCode.CONFIG_INVALID),
        ({"max_records": 10**6}, FeedErrorCode.CONFIG_INVALID),
        ({"ttl": MAX_TTL + timedelta(seconds=1)}, FeedErrorCode.CONFIG_INVALID),
        ({"ttl": timedelta(0)}, FeedErrorCode.CONFIG_INVALID),
        ({"max_change_ratio": 1.5}, FeedErrorCode.CONFIG_INVALID),
    ],
)
@pytest.mark.asyncio
async def test_incomplete_config_refused(tmp_path, over, code):
    with pytest.raises(feeds.FeedError) as e:
        validate_config(_cfg(GOOD, **over))
    assert e.value.code is code
    engine, repo, clock = await _make(tmp_path)
    f = FakeFetcher(GOOD)
    r = await _run(_cfg(GOOD, **over), f, repo, clock)
    assert (r.status, r.code) == (RunStatus.REFUSED, code)
    assert f.calls == [] and await _rowcount(engine) == 0
    await engine.dispose()


def test_registry_validates_and_never_enables():
    reg = FeedRegistry()
    reg.register(_cfg(GOOD, enabled=False))
    assert reg.enabled_sources() == []
    with pytest.raises(feeds.FeedError):
        reg.register(_cfg(GOOD, enabled=False))  # duplicate source
    with pytest.raises(feeds.FeedError):
        FeedRegistry().register(_cfg(GOOD, license=""))


# -- integrity ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_sha256_pin_mismatch_rejects_whole_payload(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    tampered = GOOD + b"extra-evil.example\n"
    r = await _run(_cfg(GOOD), FakeFetcher(tampered), repo, clock)
    assert (r.status, r.code) == (RunStatus.REJECTED, FeedErrorCode.INTEGRITY_MISMATCH)
    assert await _rowcount(engine) == 0
    ok = await _run(_cfg(GOOD), FakeFetcher(GOOD), repo, clock)
    assert ok.status is RunStatus.INGESTED and ok.accepted == 3
    await engine.dispose()


@pytest.mark.asyncio
async def test_hmac_signature_scheme(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    cfg = _cfg(integrity=IntegrityPin(IntegrityScheme.HMAC_SHA256, hmac_key=HMAC_KEY))
    sig = hmac.new(HMAC_KEY, GOOD, hashlib.sha256).hexdigest()
    assert "synthetic-test-key" not in repr(cfg)
    for bad in (None, "0" * 64, sig[:-1] + ("0" if sig[-1] != "0" else "1")):
        r = await _run(cfg, FakeFetcher(GOOD, signature_hex=bad), repo, clock)
        assert r.code is FeedErrorCode.INTEGRITY_MISMATCH
    assert await _rowcount(engine) == 0
    r = await _run(cfg, FakeFetcher(GOOD, signature_hex=sig), repo, clock)
    assert r.status is RunStatus.INGESTED
    await engine.dispose()


@pytest.mark.asyncio
async def test_insecure_transport_rejected(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    r = await _run(_cfg(GOOD), FakeFetcher(GOOD, tls_verified=False), repo, clock)
    assert r.code is FeedErrorCode.TRANSPORT_INSECURE and await _rowcount(engine) == 0
    await engine.dispose()


# -- bounded parsing ----------------------------------------------------------


def test_oversized_payload_rejected_before_parse(monkeypatch):
    called = []
    monkeypatch.setattr(feeds, "_decode", lambda p: called.append(1) or "")
    with pytest.raises(feeds.FeedError) as e:
        parse_payload(_cfg(max_payload_bytes=16), b"a" * 17)
    assert e.value.code is FeedErrorCode.PAYLOAD_TOO_LARGE and called == []


@pytest.mark.asyncio
async def test_oversized_payload_rejected_before_integrity_check(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    big = b"a.example\n" * 1000
    r = await _run(_cfg(big, max_payload_bytes=100), FakeFetcher(big), repo, clock)
    assert r.code is FeedErrorCode.PAYLOAD_TOO_LARGE and await _rowcount(engine) == 0
    await engine.dispose()


@pytest.mark.parametrize(
    ("cfg_over", "payload", "code"),
    [
        (
            {"max_records": 2},
            _lines("a.example", "b.example", "c.example"),
            FeedErrorCode.TOO_MANY_RECORDS,
        ),
        ({}, _lines("a" * 300 + ".example"), FeedErrorCode.RECORD_TOO_LONG),
        ({}, b"a.example\nb\x00.example\n", FeedErrorCode.CONTROL_CHARACTERS),
        ({}, b"a.example\x1b[31m\n", FeedErrorCode.CONTROL_CHARACTERS),
        ({}, b"a.example\n\xff\xfe\n", FeedErrorCode.MALFORMED_PAYLOAD),
        ({}, _lines("com"), FeedErrorCode.RECORD_INVALID),  # TLD-wide
        ({}, _lines("*.example.com"), FeedErrorCode.RECORD_INVALID),
        ({"format": FeedFormat.JSON_LIST}, b"{not json", FeedErrorCode.MALFORMED_PAYLOAD),
        ({"format": FeedFormat.JSON_LIST}, b'{"a": NaN}', FeedErrorCode.MALFORMED_PAYLOAD),
        (
            {"format": FeedFormat.JSON_LIST, "max_payload_bytes": 10000},
            b"[" * 3000 + b"]" * 3000,
            FeedErrorCode.NESTING_TOO_DEEP,
        ),
        ({"format": FeedFormat.JSON_LIST}, b'{"a":[[[[[1]]]]]}', FeedErrorCode.NESTING_TOO_DEEP),
        (
            {"format": FeedFormat.JSON_LIST},
            _json([{"value": "a.example", "x": 1}]),
            FeedErrorCode.SCHEMA_VIOLATION,
        ),
        ({"format": FeedFormat.JSON_LIST}, _json("nope"), FeedErrorCode.SCHEMA_VIOLATION),
        (
            {"format": FeedFormat.JSON_LIST},
            _json([], schema="other"),
            FeedErrorCode.SCHEMA_VIOLATION,
        ),
        (
            {"format": FeedFormat.JSON_LIST},
            _json([], source="feed:other"),
            FeedErrorCode.SCHEMA_VIOLATION,
        ),
        (
            {"format": FeedFormat.JSON_LIST},
            _json([], license="MIT"),
            FeedErrorCode.LICENSE_MISMATCH,
        ),
        ({"format": FeedFormat.JSON_LIST}, _json([{"value": 5}]), FeedErrorCode.SCHEMA_VIOLATION),
        (
            {"format": FeedFormat.JSON_LIST},
            _json([{"value": "a\u0000b.example"}]),
            FeedErrorCode.CONTROL_CHARACTERS,
        ),
        (
            {"format": FeedFormat.JSON_LIST},
            _json([{"value": "a.example", "ttl_hours": True}]),
            FeedErrorCode.SCHEMA_VIOLATION,
        ),
        (
            {"format": FeedFormat.JSON_LIST},
            b'{"schema":"x","schema":"y","source":"s","license":"l","indicators":[]}',
            FeedErrorCode.MALFORMED_PAYLOAD,
        ),
        (
            {"format": FeedFormat.JSON_LIST, "max_records": 1},
            _json([{"value": "a.example"}, {"value": "b.example"}]),
            FeedErrorCode.TOO_MANY_RECORDS,
        ),
    ],
)
@pytest.mark.asyncio
async def test_parser_failures_are_typed_and_write_nothing(tmp_path, cfg_over, payload, code):
    with pytest.raises(feeds.FeedError) as e:
        parse_payload(_cfg(payload, **cfg_over), payload)
    assert e.value.code is code
    engine, repo, clock = await _make(tmp_path)
    r = await _run(_cfg(payload, **cfg_over), FakeFetcher(payload), repo, clock)
    assert (r.status, r.code) == (RunStatus.REJECTED, code)
    assert await _rowcount(engine) == 0
    await engine.dispose()


def test_json_valid_roundtrip_and_deep_nesting_in_strings_ok():
    payload = _json([{"value": "Evil.Example"}])
    assert FeedFormat.JSON_LIST and _json_depth_ok('{"k": "[[[[[[[[[["}')
    entries, dup = parse_payload(_cfg(payload, format=FeedFormat.JSON_LIST), payload)
    assert [e.value for e in entries] == ["evil.example"] and dup == 0


def test_duplicates_collapse_deterministically_shortest_ttl_wins():
    payload = _json(
        [
            {"value": "b.example", "ttl_hours": 48},
            {"value": "A.example"},
            {"value": "B.example.", "ttl_hours": 24},
            {"value": "b.example", "ttl_hours": 9999},
        ]
    )
    cfg = _cfg(payload, format=FeedFormat.JSON_LIST)
    entries, dup = parse_payload(cfg, payload)
    assert dup == 2
    assert [e.value for e in entries] == ["a.example", "b.example"]  # sorted
    assert entries[1].ttl == timedelta(hours=24) and entries[0].ttl is None
    # same input, same output
    assert parse_payload(cfg, payload) == (entries, dup)


# -- poisoning guards ---------------------------------------------------------


@pytest.mark.asyncio
async def test_protected_domain_rejects_whole_feed(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    for p in (
        _lines("evil.example", "Google.COM."),
        _lines("evil.example", "corp.internal.example"),
    ):
        extra = frozenset({"corp.internal.example"})
        r = await _run(_cfg(p, extra_protected_domains=extra), FakeFetcher(p), repo, clock)
        assert (r.status, r.code) == (RunStatus.REJECTED, FeedErrorCode.PROTECTED_DOMAIN)
    assert await _rowcount(engine) == 0  # even the benign record was not ingested
    await engine.dispose()


@pytest.mark.asyncio
async def test_change_ratio_guard_blocks_mass_replacement_then_owner_approval(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    base = _lines(*[f"old-{i}.example" for i in range(10)])
    assert (await _run(_cfg(base), FakeFetcher(base), repo, clock)).status is RunStatus.INGESTED
    before = await _rowcount(engine)
    version = await repo.current_version()

    swap = _lines(*[f"new-{i}.example" for i in range(10)])
    cfg = _cfg(swap, max_change_ratio=0.3)
    r = await _run(cfg, FakeFetcher(swap), repo, clock)
    assert (r.status, r.code) == (RunStatus.NEEDS_OWNER_REVIEW, FeedErrorCode.CHANGE_RATIO_EXCEEDED)
    assert r.change_ratio == 1.0 and r.plan is not None and r.plan.dry_run is True
    assert r.plan.count("ADDED") == 10
    assert await _rowcount(engine) == before and await repo.current_version() == version
    assert await repo.lookup(IndicatorType.DOMAIN, "new-1.example") == []

    # approval of a different version does not unlock it
    r = await _run(cfg, FakeFetcher(swap), repo, clock, approved_feed_version="feed-other")
    assert r.status is RunStatus.NEEDS_OWNER_REVIEW
    # explicit owner approval of that exact version does
    r = await _run(cfg, FakeFetcher(swap), repo, clock, approved_feed_version=r.feed_version)
    assert r.status is RunStatus.INGESTED
    assert await repo.lookup(IndicatorType.DOMAIN, "new-1.example")
    await engine.dispose()


@pytest.mark.asyncio
async def test_change_ratio_allows_small_growth_and_dry_run_writes_nothing(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    base = _lines(*[f"old-{i}.example" for i in range(10)])
    await _run(_cfg(base), FakeFetcher(base), repo, clock)
    before = await _rowcount(engine)
    grown = _lines(*[f"old-{i}.example" for i in range(10)], "extra.example")
    r = await _run(_cfg(grown, max_change_ratio=0.2), FakeFetcher(grown), repo, clock, dry_run=True)
    assert r.status is RunStatus.DRY_RUN and r.plan is not None and r.plan.dry_run
    assert await _rowcount(engine) == before
    r = await _run(_cfg(grown, max_change_ratio=0.2), FakeFetcher(grown), repo, clock)
    assert r.status is RunStatus.INGESTED and r.change_ratio == pytest.approx(0.1)
    await engine.dispose()


@pytest.mark.asyncio
async def test_poisoned_feed_can_be_rolled_back(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    await _run(_cfg(GOOD), FakeFetcher(GOOD), repo, clock)
    good_version = await repo.current_version()
    poisoned = _lines("evil-one.example", "innocent-shop.example")
    cfg = _cfg(poisoned, max_change_ratio=1.0, max_removal_ratio=1.0)
    r = await _run(cfg, FakeFetcher(poisoned), repo, clock)
    assert r.status is RunStatus.INGESTED
    assert await repo.lookup(IndicatorType.DOMAIN, "innocent-shop.example")
    plan = await repo.rollback(good_version)
    assert plan.count("ROLLED_BACK") >= 1
    assert await repo.lookup(IndicatorType.DOMAIN, "innocent-shop.example") == []
    restored = await repo.lookup(IndicatorType.DOMAIN, "evil-two.example")
    assert restored and restored[0].feed_version == "feed-" + _sha(GOOD)[:32]
    assert await repo.audit_history(IndicatorType.DOMAIN, "innocent-shop.example")  # audit kept
    await engine.dispose()


# -- G2 gap closure: removal / initial-load / single-clock guards ---------------


def _state_dump(rows) -> list[tuple]:
    return sorted(
        (r.canonical_indicator, r.feed_version, r.promotion_version, r.superseded_in_version,
         r.rolled_back, r.expires_at)
        for r in rows
    )


async def _all_rows(engine):
    async with make_session_factory(engine)() as s:
        return (await s.execute(select(MailIntelIndicatorRow))).scalars().all()


@pytest.mark.asyncio
async def test_mass_removal_rejected_pending_review_and_writes_nothing(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    base = _lines(*[f"old-{i}.example" for i in range(10)])
    await _run(_cfg(base), FakeFetcher(base), repo, clock)
    before = _state_dump(await _all_rows(engine))
    version = await repo.current_version()
    # drops 8 of 10 live indicators, adds none (additions ratio is 0)
    shrunk = _lines("old-0.example", "old-1.example")
    cfg = _cfg(shrunk, max_removal_ratio=0.5)
    r = await _run(cfg, FakeFetcher(shrunk), repo, clock)
    assert r.status is RunStatus.NEEDS_OWNER_REVIEW
    assert r.code is FeedErrorCode.REMOVAL_RATIO_EXCEEDED
    assert r.removed == 8 and r.removal_ratio == pytest.approx(0.8) and r.change_ratio == 0.0
    assert r.plan is not None and r.plan.dry_run is True
    assert _state_dump(await _all_rows(engine)) == before
    assert await repo.current_version() == version
    for i in range(10):
        assert await repo.lookup(IndicatorType.DOMAIN, f"old-{i}.example")
    # approval of another version does not unlock; the exact version does
    r = await _run(cfg, FakeFetcher(shrunk), repo, clock, approved_feed_version="feed-other")
    assert r.status is RunStatus.NEEDS_OWNER_REVIEW
    r = await _run(cfg, FakeFetcher(shrunk), repo, clock, approved_feed_version=r.feed_version)
    assert r.status is RunStatus.INGESTED
    await engine.dispose()


@pytest.mark.asyncio
async def test_removal_within_threshold_accepted_and_dry_run_still_guarded(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    base = _lines(*[f"old-{i}.example" for i in range(10)])
    await _run(_cfg(base), FakeFetcher(base), repo, clock)
    small = _lines(*[f"old-{i}.example" for i in range(8)])  # removes 2 of 10
    r = await _run(_cfg(small, max_removal_ratio=0.2), FakeFetcher(small), repo, clock)
    assert r.status is RunStatus.INGESTED and r.removed == 2
    assert r.removal_ratio == pytest.approx(0.2)
    big = _lines("old-0.example")  # dry_run does not bypass the guard
    r = await _run(_cfg(big, max_removal_ratio=0.2), FakeFetcher(big), repo, clock, dry_run=True)
    assert r.status is RunStatus.NEEDS_OWNER_REVIEW
    await engine.dispose()


@pytest.mark.asyncio
async def test_mass_early_expiry_counts_as_removal_and_writes_nothing(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    domains = [f"old-{i}.example" for i in range(10)]
    base = _json([{"value": d} for d in domains])
    cfg = _cfg(base, format=FeedFormat.JSON_LIST, ttl=timedelta(days=30), max_removal_ratio=0.3)
    await _run(cfg, FakeFetcher(base), repo, clock)
    before = _state_dump(await _all_rows(engine))
    # same indicators, but the update expires 6 of them within an hour
    short = _json([{"value": d, "ttl_hours": 1} if i < 6 else {"value": d}
                   for i, d in enumerate(domains)])
    cfg2 = _cfg(short, format=FeedFormat.JSON_LIST, ttl=timedelta(days=30), max_removal_ratio=0.3)
    r = await _run(cfg2, FakeFetcher(short), repo, clock)
    assert r.status is RunStatus.NEEDS_OWNER_REVIEW
    assert r.code is FeedErrorCode.REMOVAL_RATIO_EXCEEDED
    assert r.expired_early == 6 and r.removed == 0 and r.change_ratio == 0.0
    assert _state_dump(await _all_rows(engine)) == before
    await engine.dispose()


@pytest.mark.asyncio
async def test_rollback_after_accepted_removal_update_restores_live_set(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    base = _lines(*[f"old-{i}.example" for i in range(10)])
    await _run(_cfg(base), FakeFetcher(base), repo, clock)
    good_version = await repo.current_version()
    # owner-approved mass removal + replacement of two entries
    shrunk = _lines("old-0.example", "new-a.example")
    cfg = _cfg(shrunk, max_change_ratio=1.0, max_removal_ratio=0.5)
    r = await _run(cfg, FakeFetcher(shrunk), repo, clock)
    assert r.status is RunStatus.NEEDS_OWNER_REVIEW
    r = await _run(cfg, FakeFetcher(shrunk), repo, clock, approved_feed_version=r.feed_version)
    assert r.status is RunStatus.INGESTED
    assert await repo.lookup(IndicatorType.DOMAIN, "new-a.example")
    plan = await repo.rollback(good_version)
    assert plan.count("ROLLED_BACK") >= 1 and plan.count("RESTORED") >= 1
    assert await repo.lookup(IndicatorType.DOMAIN, "new-a.example") == []
    old_feed = "feed-" + _sha(base)[:32]
    for i in range(10):
        m = await repo.lookup(IndicatorType.DOMAIN, f"old-{i}.example")
        assert m and m[0].feed_version == old_feed
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", [None, "", "has space", "x" * 201, "a/b", "ref\n"])
async def test_initial_load_requires_valid_opaque_approval_ref(tmp_path, ref):
    engine, repo, clock = await _make(tmp_path)
    r = await _run(_cfg(GOOD), FakeFetcher(GOOD), repo, clock, initial_load_approval_ref=ref)
    assert (r.status, r.code) == (RunStatus.REJECTED, FeedErrorCode.INITIAL_LOAD_APPROVAL_REQUIRED)
    assert r.plan is None
    assert await _rowcount(engine) == 0 and await repo.current_version() == 0
    r = await _run(_cfg(GOOD), FakeFetcher(GOOD), repo, clock, initial_load_approval_ref=ref,
                   dry_run=True)
    assert r.code is FeedErrorCode.INITIAL_LOAD_APPROVAL_REQUIRED and await _rowcount(engine) == 0
    ok = await _run(_cfg(GOOD), FakeFetcher(GOOD), repo, clock,
                    initial_load_approval_ref="owner-approved:2026-01_a")
    assert ok.status is RunStatus.INGESTED and await _rowcount(engine) == 3
    # a populated live set no longer needs it
    more = _lines("evil-one.example", "evil-two.example", "evil-three.example", "evil-four.example")
    r = await _run(_cfg(more), FakeFetcher(more), repo, clock, initial_load_approval_ref=None)
    assert r.status is RunStatus.INGESTED
    await engine.dispose()


@pytest.mark.asyncio
async def test_initial_load_approval_required_again_after_live_set_fully_expires(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    await _run(_cfg(GOOD, ttl=timedelta(days=1)), FakeFetcher(GOOD), repo, clock)
    clock.now = T0 + timedelta(days=1, seconds=1)
    r = await _run(_cfg(GOOD, ttl=timedelta(days=1)), FakeFetcher(GOOD), repo, clock,
                   initial_load_approval_ref=None)
    assert r.code is FeedErrorCode.INITIAL_LOAD_APPROVAL_REQUIRED
    await engine.dispose()


@pytest.mark.asyncio
async def test_single_clock_boundary_is_consistent_across_read_and_promote(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    p = _lines("a.example")
    cfg = _cfg(p, ttl=timedelta(hours=1))
    await _run(cfg, FakeFetcher(p), repo, clock)
    # exactly at expiry (strictly-before rule): not live for the guard read, so
    # the next load is an initial load and is rejected without approval, with
    # the injected clock and the repository clock agreeing on the boundary.
    clock.now = T0 + timedelta(hours=1)
    assert await repo.live_keys("feed:alpha") == frozenset()
    r = await _run(cfg, FakeFetcher(p), repo, clock, initial_load_approval_ref=None)
    assert r.code is FeedErrorCode.INITIAL_LOAD_APPROVAL_REQUIRED
    # one tick earlier it is live and the repeat load is an UNCHANGED no-op
    clock.now = T0 + timedelta(hours=1) - timedelta(microseconds=1)
    assert len(await repo.live_keys("feed:alpha")) == 1
    r = await _run(cfg, FakeFetcher(p), repo, clock, initial_load_approval_ref=None)
    assert r.status is RunStatus.INGESTED and r.plan is not None
    assert r.plan.count("UNCHANGED") == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_uses_one_instant_from_the_injected_clock(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    ticks = []

    def drifting():
        ticks.append(1)
        return T0 + timedelta(seconds=len(ticks))  # would differ on every call

    p = _lines("a.example")
    cfg = _cfg(p)
    r = await ingest_feed(cfg, FakeFetcher(p), repo, now=drifting, env=ON,
                          initial_load_approval_ref="owner-approved-1")
    assert r.status is RunStatus.INGESTED and len(ticks) == 1
    (row,) = await repo.audit_history(IndicatorType.DOMAIN, "a.example")
    assert row.first_seen.replace(tzinfo=UTC) == T0 + timedelta(seconds=1)
    # without ``now`` the repository clock is the single source
    p2 = _lines("a.example", "b.example")
    clock.now = T0 + timedelta(days=1)
    r = await ingest_feed(_cfg(p2, max_change_ratio=1.0), FakeFetcher(p2), repo, env=ON)
    assert r.status is RunStatus.INGESTED
    (b,) = await repo.audit_history(IndicatorType.DOMAIN, "b.example")
    assert b.first_seen.replace(tzinfo=UTC) == clock.now
    await engine.dispose()


@pytest.mark.asyncio
async def test_new_guards_log_and_rows_carry_no_raw_content(tmp_path, caplog):
    engine, repo, clock = await _make(tmp_path)
    bad_ref = _CANARY + " x"
    r = await _run(_cfg(GOOD), FakeFetcher(GOOD), repo, clock, initial_load_approval_ref=bad_ref)
    assert r.code is FeedErrorCode.INITIAL_LOAD_APPROVAL_REQUIRED
    assert _CANARY not in repr(r) and _CANARY not in caplog.text
    await engine.dispose()


def test_config_validates_removal_ratio():
    for bad in (-0.1, 1.1):
        with pytest.raises(feeds.FeedError):
            validate_config(_cfg(GOOD, max_removal_ratio=bad))
    assert _cfg(GOOD).max_removal_ratio == feeds.DEFAULT_MAX_REMOVAL_RATIO


# -- failure / staleness ------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_failure_keeps_previous_live_set_and_is_typed(tmp_path, caplog):
    engine, repo, clock = await _make(tmp_path)
    await _run(_cfg(GOOD), FakeFetcher(GOOD), repo, clock)
    before, version = await _rowcount(engine), await repo.current_version()
    r = await _run(_cfg(GOOD), FakeFetcher(exc=RuntimeError(_CANARY)), repo, clock)
    assert (r.status, r.code) == (RunStatus.FAILED, FeedErrorCode.FETCH_FAILED)
    assert _CANARY not in repr(r) and _CANARY not in caplog.text
    assert await _rowcount(engine) == before and await repo.current_version() == version
    assert await repo.lookup(IndicatorType.DOMAIN, "evil-one.example")  # still live
    # never "clean": an unlisted domain stays UNKNOWN (empty), not a safe verdict
    assert await repo.lookup(IndicatorType.DOMAIN, "unlisted.example") == []
    assert not hasattr(r, "safe") and not hasattr(r, "clean")
    await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_timeout_is_typed(tmp_path):
    import asyncio

    engine, repo, clock = await _make(tmp_path)

    async def slow(source: str) -> FetchedFeed:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")

    r = await _run(_cfg(GOOD, fetch_timeout_s=0.05), slow, repo, clock)
    assert (r.status, r.code) == (RunStatus.FAILED, FeedErrorCode.FETCH_TIMEOUT)
    assert await _rowcount(engine) == 0
    r = await _run(_cfg(GOOD), FakeFetcher(exc=TimeoutError()), repo, clock)
    assert r.code is FeedErrorCode.FETCH_TIMEOUT
    r = await _run(_cfg(GOOD), lambda s: _async_value("not-a-FetchedFeed"), repo, clock)
    assert r.status is RunStatus.FAILED
    await engine.dispose()


async def _async_value(v):
    return v


@pytest.mark.asyncio
async def test_ttl_cap_enforced_and_aged_out_feed_stops_being_live(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    payload = _json(
        [{"value": "a.example", "ttl_hours": 100000}, {"value": "b.example", "ttl_hours": 2}]
    )
    cfg = _cfg(payload, format=FeedFormat.JSON_LIST, ttl=timedelta(days=3))
    r = await _run(cfg, FakeFetcher(payload), repo, clock)
    assert r.status is RunStatus.INGESTED
    for dom in ("a.example", "b.example"):
        (row,) = await repo.audit_history(IndicatorType.DOMAIN, dom)
        assert row.expires_at.replace(tzinfo=UTC) - T0 <= timedelta(days=3)
    (b,) = await repo.audit_history(IndicatorType.DOMAIN, "b.example")
    assert b.expires_at.replace(tzinfo=UTC) - T0 == timedelta(hours=2)
    clock.now = T0 + timedelta(hours=3)
    assert await repo.lookup(IndicatorType.DOMAIN, "b.example") == []
    assert await repo.lookup(IndicatorType.DOMAIN, "a.example")
    # the feed stops updating: everything ages out; rows are kept for audit
    clock.now = T0 + timedelta(days=3, seconds=1)
    assert await repo.lookup(IndicatorType.DOMAIN, "a.example") == []
    assert await repo.audit_history(IndicatorType.DOMAIN, "a.example")
    await engine.dispose()


# -- privacy / supply chain ---------------------------------------------------


def test_no_mail_derived_input_accepted():
    sig = inspect.signature(ingest_feed)
    allowed = {"config", "fetcher", "repo", "now", "env", "dry_run",
               "approved_feed_version", "initial_load_approval_ref"}
    assert set(sig.parameters) == allowed
    fsig = inspect.signature(feeds.FeedFetcher.__call__)
    assert list(fsig.parameters) == ["self", "source"]
    assert {f for f in feeds.FeedConfig.__dataclass_fields__}.isdisjoint(
        {"url", "message", "sender", "recipient", "subject", "body", "account_scope", "link"}
    )


@pytest.mark.asyncio
async def test_fetcher_receives_only_configured_source(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    f = FakeFetcher(GOOD)
    await _run(_cfg(GOOD), f, repo, clock)
    assert f.calls == [("feed:alpha",)]
    await engine.dispose()


def test_new_modules_import_no_network_libraries():
    banned = {
        "socket",
        "urllib.request",
        "requests",
        "http.client",
        "httpx",
        "aiohttp",
        "ssl",
        "ftplib",
        "smtplib",
    }
    root = pathlib.Path(feeds.__file__).resolve().parent
    for path in (root / "feeds.py",):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for n in names:
                assert n not in banned and n.split(".")[0] not in {
                    "requests",
                    "httpx",
                    "aiohttp",
                    "socket",
                }, n
    from backend.mail_security.ingestion_seam import mail_security_enabled

    assert mail_security_enabled({}) is False


@pytest.mark.asyncio
async def test_canary_absent_from_rows_and_logs(tmp_path, caplog):
    engine, repo, clock = await _make(tmp_path)
    payload = _lines("evil.example", f"{_CANARY}.example", "x" * 400 + ".example")
    r = await _run(_cfg(payload), FakeFetcher(payload), repo, clock)
    assert r.status is RunStatus.REJECTED and _CANARY not in repr(r) and _CANARY not in caplog.text
    ok = _lines("evil.example")
    await _run(_cfg(ok), FakeFetcher(ok), repo, clock)
    async with make_session_factory(engine)() as s:
        rows = (await s.execute(select(MailIntelIndicatorRow))).scalars().all()
    dump = " ".join(
        str(getattr(row, c.name)) for row in rows for c in MailIntelIndicatorRow.__table__.columns
    )
    assert _CANARY not in dump
    await engine.dispose()
