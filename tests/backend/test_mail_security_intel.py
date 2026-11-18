"""Tests for the NerveShield threat-intel store (Phase 2, Task 2.2).

Proof is on SQLite scratch DBs (the house durable-test harness); Postgres is
not exercised. Invariants: R-006 (expiry/provenance/override/rollback), R-015.1,
R-023 (override account isolation), I-02/R-004.5 (no data => unknown), I-09
(match is evidence, never an action), T-19 (poisoning/stale), T-30 (no global
allowlist).
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.mail_security import intel
from backend.mail_security.intel import (
    MAX_OVERRIDE_TTL,
    MAX_TTL,
    ConfidenceType,
    IndicatorType,
    IntelError,
    IntelErrorCode,
    IntelRecord,
    canonicalize_indicator,
    match_to_finding,
    sender_key,
    validate_record,
)
from backend.mail_security.models import FindingCategory
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailIntelIndicatorRow,
    MailIntelOverrideRow,
)
from backend.persistence.repositories.mail_intel_repo import MailIntelRepository

_ALEMBIC_DIR = (
    pathlib.Path(__file__).resolve().parents[2] / "backend" / "persistence" / "alembic"
)
_TABLES = [MailIntelIndicatorRow.__table__, MailIntelOverrideRow.__table__]
_CANARY = "CANARY-SECRET-dont-persist-me-9f3b2a"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
ACCT_A = "acct:aaaa1111"
ACCT_B = "acct:bbbb2222"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _raw(value="evil.example.com", **over):
    base = {
        "indicator_type": "DOMAIN",
        "value": value,
        "source": "feed:alpha",
        "confidence_type": "FEED_ASSERTED",
        "license": "CC0-1.0",
        "provenance": "prov:sig-ok-1",
        "feed_version": "v1",
        "first_seen": T0 - timedelta(days=2),
        "last_seen": T0 - timedelta(hours=1),
        "expires_at": T0 + timedelta(days=7),
    }
    base.update(over)
    return base


def _rec(value="evil.example.com", **over) -> IntelRecord:
    return validate_record(_raw(value, **over), now=T0)


async def _make(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'intel.db'}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(_TABLES[0].metadata.create_all, tables=_TABLES)
    clock = Clock(T0)
    return engine, MailIntelRepository(make_session_factory(engine), clock=clock), clock


# -- validation / canonicalization -------------------------------------------


def test_canonicalization():
    assert canonicalize_indicator(IndicatorType.DOMAIN, "Evil.Example.COM.") == "evil.example.com"
    assert canonicalize_indicator(IndicatorType.DOMAIN, "x@Evil.Example.com") == "evil.example.com"
    assert canonicalize_indicator(IndicatorType.IP_LITERAL, "2001:0db8::0001") == "2001:db8::1"
    a = canonicalize_indicator(IndicatorType.URL_SHA256, "HTTPS://Evil.com:443/a?b=1#frag")
    b = canonicalize_indicator(IndicatorType.URL_SHA256, "https://evil.com/a?b=1")
    assert a == b and len(a) == 64
    with pytest.raises(IntelError):
        canonicalize_indicator(IndicatorType.DOMAIN, "com")  # TLD-wide
    with pytest.raises(IntelError):
        canonicalize_indicator(IndicatorType.DOMAIN, "*.example.com")


def test_missing_source_and_license_rejected():
    with pytest.raises(IntelError) as e:
        validate_record(_raw(source=""), now=T0)
    assert e.value.code is IntelErrorCode.MISSING_SOURCE
    raw = _raw()
    del raw["license"]
    with pytest.raises(IntelError) as e:
        validate_record(raw, now=T0)
    assert e.value.code is IntelErrorCode.MISSING_LICENSE


def test_ttl_bounds_rejected():
    with pytest.raises(IntelError) as e:
        validate_record(_raw(expires_at=T0), now=T0)
    assert e.value.code is IntelErrorCode.EXPIRES_IN_PAST
    with pytest.raises(IntelError) as e:
        validate_record(_raw(expires_at=T0 + MAX_TTL + timedelta(seconds=1)), now=T0)
    assert e.value.code is IntelErrorCode.TTL_TOO_LONG
    assert validate_record(_raw(expires_at=T0 + MAX_TTL), now=T0)  # boundary ok


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "a string",
        _raw(value="x" * 5000),
        _raw(value=""),
        _raw(indicator_type="NOPE"),
        _raw(confidence_type="0.99"),  # a fabricated probability is not a type
        _raw(source="bad source with spaces"),
        _raw(first_seen="not-a-date"),
        _raw(unexpected="field"),
        _raw(first_seen=T0, last_seen=T0 - timedelta(days=1)),
        _raw(last_seen=T0 + timedelta(days=30)),
    ],
)
def test_malformed_or_oversized_gives_typed_error(raw):
    with pytest.raises(IntelError) as e:
        validate_record(raw, now=T0)  # type: ignore[arg-type]
    assert isinstance(e.value.code, IntelErrorCode)
    assert str(e.value) == e.value.code.value  # content-free


def test_error_does_not_echo_value():
    with pytest.raises(IntelError) as e:
        validate_record(_raw(value=f"{_CANARY} bad"), now=T0)
    assert _CANARY not in str(e.value) and _CANARY not in repr(e.value)


def test_no_network_imports():
    import backend.mail_security.intel as mod
    import backend.persistence.repositories.mail_intel_repo as repo_mod

    for path in (mod.__file__, repo_mod.__file__):
        src = pathlib.Path(path).read_text()
        for banned in ("import socket", "import httpx", "import requests", "import aiohttp",
                       "import urllib.request", "from urllib.request", "import http.client"):
            assert banned not in src, (path, banned)


# -- store: expiry, disable, rollback, dry-run -------------------------------


async def test_default_empty_is_unknown(tmp_path):
    engine, repo, _ = await _make(tmp_path)
    try:
        assert await repo.lookup(IndicatorType.DOMAIN, "evil.example.com") == []
        assert await repo.lookup(IndicatorType.DOMAIN, "not a domain!!") == []
        ev = await repo.evaluate(
            account_scope=ACCT_A, sender="sender:x", indicator_type=IndicatorType.DOMAIN,
            value="evil.example.com",
        )
        assert ev.matches == () and ev.suppressed == ()
    finally:
        await engine.dispose()


async def test_expired_never_live_boundary_but_stored(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    try:
        rec = _rec(expires_at=T0 + timedelta(hours=1))
        await repo.promote([rec])
        clock.now = T0 + timedelta(hours=1) - timedelta(microseconds=1)
        assert len(await repo.lookup(IndicatorType.DOMAIN, "evil.example.com")) == 1
        clock.now = T0 + timedelta(hours=1)  # exactly expires_at => expired
        assert await repo.lookup(IndicatorType.DOMAIN, "evil.example.com") == []
        clock.now = T0 + timedelta(days=400)
        assert await repo.lookup(IndicatorType.DOMAIN, "evil.example.com") == []
        hist = await repo.audit_history(IndicatorType.DOMAIN, "evil.example.com")
        assert len(hist) == 1  # retained for audit (R-006.3)
    finally:
        await engine.dispose()


async def test_parent_domain_match_method(tmp_path):
    engine, repo, _ = await _make(tmp_path)
    try:
        await repo.promote([_rec("example.com")])
        got = await repo.lookup(IndicatorType.DOMAIN, "a.b.example.com")
        assert [m.match_method.value for m in got] == ["PARENT_DOMAIN"]
        got = await repo.lookup(IndicatorType.DOMAIN, "example.com")
        assert [m.match_method.value for m in got] == ["EXACT"]
        assert await repo.lookup(IndicatorType.DOMAIN, "com") == []
    finally:
        await engine.dispose()


async def test_disabled_not_live_but_stored(tmp_path):
    engine, repo, _ = await _make(tmp_path)
    try:
        await repo.promote([_rec()])
        n = await repo.set_enabled(
            IndicatorType.DOMAIN, "evil.example.com", "feed:alpha", enabled=False
        )
        assert n == 1
        assert await repo.lookup(IndicatorType.DOMAIN, "evil.example.com") == []
        assert len(await repo.audit_history(IndicatorType.DOMAIN, "evil.example.com")) == 1
        await repo.set_enabled(IndicatorType.DOMAIN, "evil.example.com", "feed:alpha", enabled=True)
        assert len(await repo.lookup(IndicatorType.DOMAIN, "evil.example.com")) == 1
    finally:
        await engine.dispose()


async def test_overlapping_versions_and_rollback_restore_prior_live_set(tmp_path):
    engine, repo, _ = await _make(tmp_path)
    try:
        p1 = await repo.promote(
            [_rec(feed_version="v1"), _rec("other.example.org", feed_version="v1")]
        )
        assert p1.to_version == 1
        p2 = await repo.promote(
            [
                _rec(feed_version="v2", confidence_type="ANALYST_REVIEWED"),
                _rec("third.example.net", feed_version="v2"),
            ]
        )
        assert p2.to_version == 2
        assert p2.count("REPLACED") == 1 and p2.count("ADDED") == 1
        live = await repo.lookup(IndicatorType.DOMAIN, "evil.example.com")
        assert [m.feed_version for m in live] == ["v2"]  # v1 superseded, not doubled
        assert len(await repo.lookup(IndicatorType.DOMAIN, "third.example.net")) == 1

        rb = await repo.rollback(1)
        assert rb.count("ROLLED_BACK") == 2 and rb.count("RESTORED") == 1
        live = await repo.lookup(IndicatorType.DOMAIN, "evil.example.com")
        assert [m.feed_version for m in live] == ["v1"]
        assert live[0].confidence_type is ConfidenceType.FEED_ASSERTED
        assert await repo.lookup(IndicatorType.DOMAIN, "third.example.net") == []
        assert len(await repo.lookup(IndicatorType.DOMAIN, "other.example.org")) == 1
        # rolled-back rows remain stored (audit)
        assert len(await repo.audit_history(IndicatorType.DOMAIN, "third.example.net")) == 1
        assert len(await repo.audit_history(IndicatorType.DOMAIN, "evil.example.com")) == 2
        assert await repo.current_version() == 1
        # versions stay monotonic after rollback
        p3 = await repo.promote([_rec("fourth.example.io", feed_version="v3")])
        assert p3.to_version == 3
    finally:
        await engine.dispose()


async def test_dry_run_writes_nothing_and_matches_real_run(tmp_path):
    engine, repo, _ = await _make(tmp_path)
    try:
        await repo.promote([_rec(feed_version="v1")])
        batch = [_rec(feed_version="v2"), _rec("new.example.org", feed_version="v2")]
        dry = await repo.promote(batch, dry_run=True)
        assert dry.dry_run and dry.to_version == 2 and dry.count("ADDED") == 1
        assert await repo.current_version() == 1
        assert await repo.lookup(IndicatorType.DOMAIN, "new.example.org") == []
        still = await repo.lookup(IndicatorType.DOMAIN, "evil.example.com")
        assert [m.feed_version for m in still] == ["v1"]
        real = await repo.promote(batch)
        assert real.changes == dry.changes and real.to_version == dry.to_version
        before = await repo.audit_history(IndicatorType.DOMAIN, "evil.example.com")
        dry_rb = await repo.rollback(1, dry_run=True)
        assert dry_rb.dry_run and dry_rb.count("ROLLED_BACK") == 2
        after = await repo.audit_history(IndicatorType.DOMAIN, "evil.example.com")
        assert [(r.rolled_back, r.superseded_in_version) for r in before] == [
            (r.rolled_back, r.superseded_in_version) for r in after
        ]
        assert await repo.current_version() == 2
    finally:
        await engine.dispose()


async def test_promote_idempotent_and_rejects_bad_batches(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    try:
        rec = _rec()
        await repo.promote([rec])
        again = await repo.promote([rec])
        assert again.count("UNCHANGED") == 1 and await repo.current_version() == 1
        with pytest.raises(IntelError) as e:
            await repo.promote([rec, rec])
        assert e.value.code is IntelErrorCode.DUPLICATE_IN_BATCH
        with pytest.raises(IntelError) as e:
            await repo.promote([_rec(feed_version="v9")] * 2)
        assert e.value.code is IntelErrorCode.DUPLICATE_IN_BATCH
        clock.now = T0 + timedelta(days=30)  # record validated earlier is now stale
        with pytest.raises(IntelError) as e:
            await repo.promote([_rec(feed_version="v10")])
        assert e.value.code is IntelErrorCode.EXPIRES_IN_PAST
        for bad in (-1, 1, 99):
            with pytest.raises(IntelError) as e:
                await repo.rollback(bad)
            assert e.value.code is IntelErrorCode.INVALID_ROLLBACK_TARGET
    finally:
        await engine.dispose()


# -- overrides ---------------------------------------------------------------


async def _override(
    repo, clock, *, acct=ACCT_A, sender="sender:s1", value="evil.example.com",
    ttl=timedelta(days=5),
):
    return await repo.add_override(
        account_scope=acct,
        sender=sender,
        indicator_type=IndicatorType.DOMAIN,
        indicator_value=value,
        approval_ref="approval:user-1",
        expires_at=clock.now + ttl,
    )


async def _eval(repo, acct, sender, value="evil.example.com"):
    return await repo.evaluate(
        account_scope=acct, sender=sender, indicator_type=IndicatorType.DOMAIN, value=value
    )


async def test_override_scoped_to_account_not_applied_to_other_account(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    try:
        await repo.promote([_rec()])
        ov = await _override(repo, clock)
        a = await _eval(repo, ACCT_A, "sender:s1")
        assert a.matches == () and len(a.suppressed) == 1
        assert a.suppressed[0].approval_ref == "approval:user-1"
        b = await _eval(repo, ACCT_B, "sender:s1")  # same exact sender + indicator ids
        assert len(b.matches) == 1 and b.suppressed == ()
        # account B cannot revoke account A's override even with its exact id
        assert await repo.revoke_override(ACCT_B, ov.id) is False
        assert (await _eval(repo, ACCT_A, "sender:s1")).matches == ()
    finally:
        await engine.dispose()


async def test_override_not_a_global_allowlist(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    try:
        await repo.promote([_rec(), _rec("other.example.org")])
        await _override(repo, clock)
        # other sender, same account: still matches (T-30)
        assert len((await _eval(repo, ACCT_A, "sender:s2")).matches) == 1
        # other indicator, same sender: still matches
        assert len((await _eval(repo, ACCT_A, "sender:s1", "other.example.org")).matches) == 1
        # the indicator store itself is untouched by the override
        assert len(await repo.lookup(IndicatorType.DOMAIN, "evil.example.com")) == 1
        # wildcard / missing scope / missing approval cannot be created
        for kwargs in (
            {"indicator_value": "*"},
            {"indicator_value": "*.example.com"},
            {"sender": "*"},
            {"sender": ""},
            {"account_scope": ""},
            {"account_scope": "*"},
        ):
            args = dict(
                account_scope=ACCT_A, sender="sender:s1", indicator_type=IndicatorType.DOMAIN,
                indicator_value="evil.example.com", approval_ref="approval:u",
                expires_at=clock.now + timedelta(days=1),
            )
            args.update(kwargs)
            with pytest.raises(IntelError):
                await repo.add_override(**args)
        with pytest.raises(IntelError) as e:
            await repo.add_override(
                account_scope=ACCT_A, sender="sender:s1", indicator_type=IndicatorType.DOMAIN,
                indicator_value="evil.example.com", approval_ref=None,
                expires_at=clock.now + timedelta(days=1),
            )
        assert e.value.code is IntelErrorCode.MISSING_APPROVAL
        for exp in (clock.now, clock.now + MAX_OVERRIDE_TTL + timedelta(seconds=1)):
            with pytest.raises(IntelError) as e:
                await _override(repo, clock, ttl=exp - clock.now)
            assert e.value.code is IntelErrorCode.OVERRIDE_TTL_INVALID
    finally:
        await engine.dispose()


async def test_expired_and_revoked_override_stops_applying(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    try:
        await repo.promote([_rec(expires_at=T0 + timedelta(days=60))])
        ov = await _override(repo, clock, ttl=timedelta(days=1))
        assert (await _eval(repo, ACCT_A, "sender:s1")).matches == ()
        clock.now = T0 + timedelta(days=1)  # exactly expiry
        assert len((await _eval(repo, ACCT_A, "sender:s1")).matches) == 1
        clock.now = T0
        assert (await _eval(repo, ACCT_A, "sender:s1")).matches == ()
        assert await repo.revoke_override(ACCT_A, ov.id) is True
        assert len((await _eval(repo, ACCT_A, "sender:s1")).matches) == 1
    finally:
        await engine.dispose()


def test_sender_key_is_opaque_and_account_bound():
    k1 = sender_key(ACCT_A, "Bob@Example.com")
    assert k1 == sender_key(ACCT_A, "bob@example.com")
    assert k1 != sender_key(ACCT_B, "bob@example.com")
    assert "bob" not in k1 and "@" not in k1


# -- finding -----------------------------------------------------------------


async def test_match_to_finding_safe_codes_only(tmp_path):
    engine, repo, _ = await _make(tmp_path)
    try:
        await repo.promote([_rec()])
        match = (await repo.lookup(IndicatorType.DOMAIN, "evil.example.com"))[0]
        finding = match_to_finding(match, evidence_ref="part:text:html:0:link:0")
        assert finding.rule_id == "THREAT_INTEL_MATCH"
        assert finding.category is FindingCategory.INTEL_MATCH
        refs = " ".join(finding.evidence_refs)
        for needle in ("intel:src:feed:alpha", "intel:method:EXACT", "intel:conf:FEED_ASSERTED",
                       "intel:feed:v1", "intel:obs:", "intel:exp:"):
            assert needle in refs
        dumped = finding.model_dump_json()
        assert "evil.example.com" not in dumped  # indicator text not echoed
        # match is evidence only: the finding has no action/disposition field
        assert not hasattr(finding, "action") and not hasattr(finding, "disposition")
    finally:
        await engine.dispose()


def test_intel_wired_only_through_worker_not_policy_or_inspections():
    """Item 2 (Phase 2 wiring) deliberately changed this guard: the worker now
    reaches intel, but ONLY via ``intel_wiring`` (read-only lookup -> findings).
    The pure policy and the offline inspections must still never import intel,
    so a match stays evidence the policy consumes as a typed finding."""
    import backend.mail_security.inspections as ins
    import backend.mail_security.policy as pol
    import backend.mail_security.worker as wrk

    for mod in (ins, pol):
        assert "mail_security.intel" not in pathlib.Path(mod.__file__).read_text()
    text = pathlib.Path(wrk.__file__).read_text()
    assert "mail_security.intel_wiring" in text
    assert "mail_security.intel import" not in text
    assert intel.INTEL_VERSION


# -- privacy -----------------------------------------------------------------


async def test_canary_absent_from_stored_rows(tmp_path):
    engine, repo, clock = await _make(tmp_path)
    try:
        # canary as hostile free text in rejected fields and as a sender address
        for bad in (_raw(value=f"{_CANARY}.example.com", source=f"{_CANARY} x"),
                    _raw(provenance=_CANARY + " x")):
            with pytest.raises(IntelError):
                validate_record(bad, now=T0)
        await repo.promote([_rec()])
        await _override(repo, clock, sender=sender_key(ACCT_A, f"{_CANARY}@mail.test"))
        async with engine.connect() as conn:
            for table in ("mail_intel_indicators", "mail_intel_overrides"):
                rows = (await conn.execute(text(f"SELECT * FROM {table}"))).fetchall()  # noqa: S608
                assert rows and _CANARY not in repr(rows)
    finally:
        await engine.dispose()


# -- migration ---------------------------------------------------------------


def _cfg(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _names(url: str) -> tuple[set[str], str]:
    eng = create_engine(url)
    with eng.connect() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    eng.dispose()
    return names, ver


def test_migration_up_then_down(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
                          "CONSTRAINT pk_alembic_version PRIMARY KEY (version_num))"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('016_mail_security')"))
    eng.dispose()
    command.upgrade(_cfg(url), "017_mail_intel")
    names, ver = _names(url)
    assert {"mail_intel_indicators", "mail_intel_overrides"} <= names
    assert ver == "017_mail_intel"
    eng = create_engine(url)
    with eng.connect() as c:
        idx = {r[1] for r in c.execute(text("PRAGMA index_list('mail_intel_overrides')"))}
    eng.dispose()
    assert "ix_mail_intel_overrides_account_scope" in idx
    command.downgrade(_cfg(url), "016_mail_security")
    names, ver = _names(url)
    assert not ({"mail_intel_indicators", "mail_intel_overrides"} & names)
    assert ver == "016_mail_security"


def test_migration_single_head_chained_on_016(tmp_path):
    script = ScriptDirectory.from_config(_cfg(f"sqlite:///{tmp_path / 'noop.db'}"))
    assert list(script.get_heads()) == ["049_voicemails"]
    assert (
        script.get_revision("025_mail_expect_feedback").down_revision
        == "024_mail_expectedness"
    )
    assert (
        script.get_revision("024_mail_expectedness").down_revision
        == "023_mail_semantic_obs"
    )
    assert (
        script.get_revision("023_mail_semantic_obs").down_revision
        == "022_mail_campaigns"
    )
    assert (
        script.get_revision("022_mail_campaigns").down_revision
        == "021_mail_thread_ledger"
    )
    assert (
        script.get_revision("021_mail_thread_ledger").down_revision
        == "020_mail_confirmations"
    )
    assert (
        script.get_revision("020_mail_confirmations").down_revision
        == "019_mail_held_promotions"
    )
    assert (
        script.get_revision("019_mail_held_promotions").down_revision
        == "018_mail_security_feedback"
    )
    assert script.get_revision("018_mail_security_feedback").down_revision == "017_mail_intel"
    assert script.get_revision("017_mail_intel").down_revision == "016_mail_security"
