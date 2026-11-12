"""NerveShield evidence-fidelity tests (NS-ADR-024, Defect 1).

Live defect: every real Gmail object is decoded body text with no RFC822 headers,
yet assessments persisted ``evidence_fidelity=ORIGINAL_RFC822``. Fidelity must come
from the TRUSTED acquisition mode recorded by the ingest seam in the job meta,
never from the attacker-controlled content, and a missing/unknown/garbled mode
must fail safe to body-only.

Invariants: R-002.1-.3, R-018, I-02, I-05, I-11, T-14, T-15, T-17, T-28.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import _mail_security_harness_helpers as H
import pytest

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.mail_security import acquisition, telemetry
from backend.mail_security.acquisition import (
    ACQUISITION_META_KEY,
    BODY_ONLY_MISSING_PARTS,
    effective_acquisition_mode,
    fidelity_for_mode,
    trusted_mode_for_write,
)
from backend.mail_security.identifiers import logical_source_key, outbox_job_key
from backend.mail_security.ingestion_seam import (
    DEFAULT_ANALYSIS_PROFILE_VERSION,
    ENV_MAIL_SECURITY_ENABLED,
    MailSecurityIngestionSeam,
)
from backend.mail_security.models import AcquisitionMode, EvidenceFidelity
from backend.mail_security.observability import build_assessment_distributions
from backend.mail_security.pipeline_runner import MailSecurityPipelineRunner
from backend.persistence.models.personal import AttentionItemRow

_ON = {ENV_MAIL_SECURITY_ENABLED: "1"}

#: Realistic live shape: valid UTF-8 plain text, no RFC822 header names at all.
HEADERLESS = (
    f"Hi there,\n\nYour order has shipped {H.CANARY}. Track it in your account.\n"
).encode()

#: Hostile body: begins with realistic RFC822 headers incl. a forged
#: Authentication-Results that claims everything passed.
SPOOFED = (
    "From: Bank <no-reply@mx.trusted.example>\r\n"
    "To: user@example.com\r\n"
    f"Subject: Statement {H.CANARY}\r\n"
    "Authentication-Results: mx.trusted.example; dkim=pass; spf=pass; dmarc=pass\r\n"
    "DKIM-Signature: v=1; a=rsa-sha256; d=trusted.example; s=sel; b=AAAA\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Your statement is ready.\r\n"
).encode()


@pytest.fixture
async def harness(tmp_path):
    h = await H.make_harness(tmp_path / "fidelity.sqlite")
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


async def _run_runner(harness: Any) -> None:
    """Drive the REAL runner (no direct worker call) until the pass completes."""
    done = asyncio.Event()
    passes = 0

    async def sleep(_: float) -> None:
        nonlocal passes
        passes += 1
        if passes >= 2:
            done.set()
        await asyncio.sleep(0)

    runner = MailSecurityPipelineRunner(
        repository=harness.registry.mail_security,
        worker_factory=lambda scope: H.security_worker(harness),
        interval_seconds=5.0,
        clock=lambda: H.CLOCK,
        sleep=sleep,
        env=_ON,
    )
    assert runner.start() is not None
    await asyncio.wait_for(done.wait(), timeout=10)
    await runner.stop()


async def _the_assessment(harness: Any):
    rows = await harness.registry.mail_security.list_assessments(harness.scope)
    assert len(rows) == 1
    return rows[0]


async def _manifest(harness: Any, row: Any):
    jobs = await harness.registry.mail_security.list_jobs(harness.scope)
    return await harness.registry.mail_security.get_evidence(
        harness.scope, jobs[0].source_key, 1
    )


async def _attention_snapshot(harness: Any) -> list[tuple[Any, ...]]:
    from sqlalchemy import select

    async with harness.registry.communications._sessions() as s:  # noqa: SLF001
        rows = (await s.execute(select(AttentionItemRow))).scalars().all()
    return sorted((r.id, r.state if hasattr(r, "state") else None) for r in rows)


def _auth_axes(row: Any) -> dict[str, Any]:
    auth = row.assessment["authentication"]
    return {k: auth[k] for k in ("spf", "dkim", "dmarc", "arc")}


# --------------------------------------------------------------------------
# 1. regression: the live defect, through ingest seam -> runner -> worker
# --------------------------------------------------------------------------
async def test_headerless_text_object_persists_body_only_not_original(
    harness, monkeypatch
) -> None:
    await H.ingest(harness, monkeypatch, H.mailitem("<live-1@example.com>", HEADERLESS))
    attention_before = await _attention_snapshot(harness)
    jobs = await harness.registry.mail_security.list_jobs(harness.scope)
    # The trusted seam site recorded the mode in the job meta.
    assert jobs[0].meta[ACQUISITION_META_KEY] == "LEGACY_BODY_ONLY"

    await _run_runner(harness)

    row = await _the_assessment(harness)
    assert row.evidence_fidelity == "BODY_ONLY"
    assert row.assessment["evidence_fidelity"] == "BODY_ONLY"
    assert row.evidence_fidelity != "ORIGINAL_RFC822"
    manifest = await _manifest(harness, row)
    assert manifest.evidence_fidelity == "BODY_ONLY"
    assert manifest.acquisition_mode == "LEGACY_BODY_ONLY"
    # R-002.3: honest missing parts, and no cross-effect on attention (R-001.4).
    assert set(BODY_ONLY_MISSING_PARTS) <= set(manifest.missing_parts)
    assert await _attention_snapshot(harness) == attention_before


# --------------------------------------------------------------------------
# 2. spoof: content can never promote fidelity or forge auth
# --------------------------------------------------------------------------
async def test_body_with_forged_headers_stays_body_only_and_auth_unknown(
    harness, monkeypatch
) -> None:
    await H.ingest(harness, monkeypatch, H.mailitem("<spoof-1@example.com>", SPOOFED))
    await _run_runner(harness)

    row = await _the_assessment(harness)
    assert row.evidence_fidelity == "BODY_ONLY"
    for axis in _auth_axes(row).values():
        assert axis["result"] == "UNKNOWN"
        assert axis["provenance"] == "UNKNOWN"
    # T-28 / NS-ADR-013: never INDEPENDENT, never SAFE_TO_PROCESS from body text.
    assert all(a["provenance"] != "INDEPENDENT" for a in _auth_axes(row).values())
    assert row.disposition != "SAFE_TO_PROCESS"
    # T-14: the canary (in subject/body) is in no persisted surface.
    manifest = await _manifest(harness, row)
    blob = json.dumps(row.assessment) + json.dumps(manifest.missing_parts)
    assert H.CANARY not in blob


# --------------------------------------------------------------------------
# 3. missing / garbled / raw-without-seam modes fail safe
# --------------------------------------------------------------------------
_BAD_MODES: list[Any] = [
    pytest.param("__missing__", id="missing"),
    pytest.param(None, id="none"),
    pytest.param("", id="empty"),
    pytest.param("garbage", id="unknown-string"),
    pytest.param("ORIGINAL_RFC822", id="fidelity-name-not-a-mode"),
    pytest.param("legacy_body_only", id="wrong-case"),
    pytest.param(12345, id="int"),
    pytest.param(["IMAP_RFC822"], id="list"),
    pytest.param({"m": "EML"}, id="dict"),
    pytest.param("IMAP_RFC822", id="raw-mode-no-raw-seam"),
    pytest.param("GMAIL_RAW", id="gmail-raw-no-raw-seam"),
    pytest.param("GMAIL_FULL", id="gmail-full-no-raw-seam"),
    pytest.param("EML", id="eml-no-raw-seam"),
    pytest.param("TAKEOUT_MBOX", id="mbox-no-raw-seam"),
]


@pytest.mark.parametrize("bad", _BAD_MODES)
def test_unusable_modes_resolve_to_legacy_body_only(bad: Any) -> None:
    meta: dict[str, Any] = {} if bad == "__missing__" else {ACQUISITION_META_KEY: bad}
    mode = effective_acquisition_mode(meta)
    assert mode is AcquisitionMode.LEGACY_BODY_ONLY
    assert fidelity_for_mode(mode) is EvidenceFidelity.BODY_ONLY


@pytest.mark.parametrize("bad_meta", [None, "x", 5, [], ()])
def test_non_mapping_meta_fails_safe(bad_meta: Any) -> None:
    assert effective_acquisition_mode(bad_meta) is AcquisitionMode.LEGACY_BODY_ONLY


@pytest.mark.parametrize("bad", _BAD_MODES)
async def test_worker_persists_body_only_for_unusable_job_mode(
    harness, monkeypatch, bad: Any
) -> None:
    """Even content with realistic headers: a job whose mode is unusable (or a
    raw mode no seam can attest) persists BODY_ONLY."""
    await H.ingest(harness, monkeypatch, H.mailitem("<fs-1@example.com>", SPOOFED))
    repo = harness.registry.mail_security
    base = (await repo.list_jobs(harness.scope))[0]
    meta = {k: v for k, v in base.meta.items() if k != ACQUISITION_META_KEY}
    if bad != "__missing__":
        meta[ACQUISITION_META_KEY] = bad
    # A second job (distinct key) on the same stored object with the bad meta;
    # drain the original first so only the bad-meta job's verdict is judged.
    await H.security_worker(harness).process_one()
    await repo.upsert_job(
        account_scope=harness.scope,
        outbox_job_key="k-bad-mode",
        source_key="src-bad-mode",
        analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
        state="PENDING",
        meta=meta,
    )
    await H.security_worker(harness).process_one()
    rows = [r for r in await repo.list_assessments(harness.scope)]
    assert len(rows) == 2
    assert {r.evidence_fidelity for r in rows} == {"BODY_ONLY"}
    manifest = await repo.get_evidence(harness.scope, "src-bad-mode", 1)
    assert manifest.evidence_fidelity == "BODY_ONLY"
    assert manifest.acquisition_mode == "LEGACY_BODY_ONLY"


# --------------------------------------------------------------------------
# 4. raw fidelities are unreachable today, reachable only via a future seam
# --------------------------------------------------------------------------
def test_only_gmail_raw_has_a_trusted_raw_seam() -> None:
    """NS-ADR-064: GMAIL_RAW is the only allow-listed raw seam, and only with a
    well-formed raw ref. Every other raw mode stays demoted even with one."""
    assert acquisition.RAW_SEAM_MODES == frozenset({AcquisitionMode.GMAIL_RAW})
    good = "content://raw/c1/abc"
    for mode in ("IMAP_RFC822", "GMAIL_FULL", "EML", "TAKEOUT_MBOX"):
        assert trusted_mode_for_write(mode) is AcquisitionMode.LEGACY_BODY_ONLY
        assert trusted_mode_for_write(mode, raw_ref=good) is AcquisitionMode.LEGACY_BODY_ONLY
    assert trusted_mode_for_write("GMAIL_RAW") is AcquisitionMode.LEGACY_BODY_ONLY
    for bad in (None, "", "content://raw/", "content://mail/c1/abc", "raw/c1/x",
                "content://raw/../mail/x", 7):
        assert (
            trusted_mode_for_write("GMAIL_RAW", raw_ref=bad)
            is AcquisitionMode.LEGACY_BODY_ONLY
        )
    assert trusted_mode_for_write("GMAIL_RAW", raw_ref=good) is AcquisitionMode.GMAIL_RAW


async def test_seam_demotes_raw_mode_on_write(harness, monkeypatch) -> None:
    """A caller asking the seam for IMAP_RFC822 still records LEGACY_BODY_ONLY."""
    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
    seam = MailSecurityIngestionSeam(
        repository=harness.registry.mail_security, account_scope=harness.scope
    )
    row = await seam.schedule(
        provider="imap", provider_uid="u1", rfc_message_id="u1", folder="INBOX",
        content_ref="content://mail/x/u1", flags={}, acquisition_mode="IMAP_RFC822",
    )
    assert row.meta[ACQUISITION_META_KEY] == "LEGACY_BODY_ONLY"
    row2 = await seam.schedule(
        provider="imap", provider_uid="u2", rfc_message_id="u2", folder="INBOX",
        content_ref="content://mail/x/u2", flags={},
    )
    assert row2.meta[ACQUISITION_META_KEY] == "LEGACY_BODY_ONLY"


async def test_gmail_raw_seam_is_the_only_way_to_original_fidelity(
    harness, monkeypatch
) -> None:
    """Only GMAIL_RAW with a raw ref reaches ORIGINAL_RFC822 (NS-ADR-064); an
    IMAP/provider-full mode with the same raw ref stays body-only, and header
    auth evidence is still never INDEPENDENT for a forged header."""
    await H.ingest(harness, monkeypatch, H.mailitem("<raw-1@example.com>", H.forged_auth_mime()))
    repo = harness.registry.mail_security
    base = (await repo.list_jobs(harness.scope))[0]
    await harness.store.put("raw/test/raw1", H.forged_auth_mime())
    raw_ref = "content://raw/test/raw1"
    for key, mode in (("raw", "GMAIL_RAW"), ("imap", "IMAP_RFC822"), ("full", "GMAIL_FULL")):
        await repo.upsert_job(
            account_scope=harness.scope,
            outbox_job_key=f"k-{key}",
            source_key=f"src-{key}",
            analysis_profile_version=DEFAULT_ANALYSIS_PROFILE_VERSION,
            state="PENDING",
            meta={**base.meta, ACQUISITION_META_KEY: mode, "raw_ref": raw_ref},
        )
    for _ in range(4):
        await H.security_worker(harness).process_one()
    by_key = {
        r.assessment["source_ref"]: r
        for r in await repo.list_assessments(harness.scope)
    }
    assert by_key["src-raw"].evidence_fidelity == "ORIGINAL_RFC822"
    assert by_key["src-imap"].evidence_fidelity == "BODY_ONLY"
    assert by_key["src-full"].evidence_fidelity == "BODY_ONLY"
    assert by_key[base.source_key].evidence_fidelity == "BODY_ONLY"
    raw_manifest = await repo.get_evidence(harness.scope, "src-raw", 1)
    assert raw_manifest.acquisition_mode == "GMAIL_RAW"
    assert raw_manifest.missing_parts == []
    assert raw_manifest.raw_content_ref == "content:raw_test_raw1"
    assert raw_manifest.normalized_content_ref is not None
    imap_manifest = await repo.get_evidence(harness.scope, "src-imap", 1)
    assert imap_manifest.acquisition_mode == "LEGACY_BODY_ONLY"
    assert imap_manifest.normalized_content_ref is None
    # Even with raw fidelity, a forged non-boundary header is never INDEPENDENT.
    assert all(
        a["provenance"] != "INDEPENDENT" for a in _auth_axes(by_key["src-raw"]).values()
    )


# --------------------------------------------------------------------------
# 5. both trusted seam sites record the mode
# --------------------------------------------------------------------------
async def test_api_path_records_legacy_body_only(monkeypatch) -> None:
    from backend.api.endpoints import communications as comm
    from backend.mail_security.ingestion_seam import set_mail_security_repository

    captured: dict[str, Any] = {}

    class Repo:
        async def upsert_job(self, **kw: Any) -> Any:
            captured.update(kw)
            return SimpleNamespace(**kw)

    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
    set_mail_security_repository(Repo())
    try:
        await comm._schedule_security_job(  # noqa: SLF001
            SimpleNamespace(
                source="api", channel="email", communication_id="c-1",
                content_ref="content://x/c-1", classification={"flags": {}},
            )
        )
    finally:
        set_mail_security_repository(None)
    assert captured["meta"][ACQUISITION_META_KEY] == "LEGACY_BODY_ONLY"


# --------------------------------------------------------------------------
# 6. observability counts body-only; flag OFF is inert
# --------------------------------------------------------------------------
def test_observability_counts_body_only_fidelity() -> None:
    rows = [SimpleNamespace(evidence_fidelity="BODY_ONLY", processing_state="ASSESSED")] * 3
    fidelity, _completeness, total = build_assessment_distributions(rows)
    assert total == 3
    assert fidelity["BODY_ONLY"] == 3
    assert fidelity["ORIGINAL_RFC822"] == 0


def test_api_projection_exposes_body_only() -> None:
    from datetime import UTC, datetime

    from backend.api.endpoints.mail_security import _summary

    row = SimpleNamespace(
        assessment_id="a", account_scope="conn:x", disposition="LOW_PRIORITY",
        mode="SHADOW", processing_state="ASSESSED", evidence_fidelity="BODY_ONLY",
        policy_version="p", analysis_profile_version="v",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert _summary(row)["evidence_fidelity"] == "BODY_ONLY"


async def test_flag_off_schedules_nothing_and_records_no_mode(harness, monkeypatch) -> None:
    await H.ingest(
        harness, monkeypatch, H.mailitem("<off-1@example.com>", HEADERLESS), flag_on=False
    )
    assert await harness.registry.mail_security.list_jobs(harness.scope) == []


def test_logical_key_unchanged_by_acquisition_mode() -> None:
    """The mode lives in job meta only: it does not alter the Task 1.2 keys, so
    Defect-2 backfill dedupes against live jobs (I-11)."""
    sk = logical_source_key("conn:abc", provider="imap", provider_uid="1",
                            rfc_message_id="1", folder="INBOX")
    assert outbox_job_key("conn:abc", sk, DEFAULT_ANALYSIS_PROFILE_VERSION) == outbox_job_key(
        "conn:abc", sk, DEFAULT_ANALYSIS_PROFILE_VERSION
    )
    assert "LEGACY_BODY_ONLY" not in sk


@pytest.mark.parametrize("content_type", ["message/rfc822", "MESSAGE/RFC822", "message/global"])
def test_rfc822_content_type_never_promotes_fidelity(content_type: str) -> None:
    """NS-ADR-024: fidelity is independent of any object content type.

    Even if job meta (or stored object metadata) claims ``message/rfc822``, only the
    trusted acquisition mode counts, and raw modes stay demoted (no raw seam)."""
    import inspect

    for meta in (
        {"content_type": content_type},
        {"content_type": content_type, ACQUISITION_META_KEY: "garbage"},
        {"content_type": content_type, ACQUISITION_META_KEY: "LEGACY_BODY_ONLY"},
        {"content_type": content_type, ACQUISITION_META_KEY: "IMAP_RFC822"},
    ):
        mode = effective_acquisition_mode(meta)
        assert mode is AcquisitionMode.LEGACY_BODY_ONLY
        assert fidelity_for_mode(mode) is EvidenceFidelity.BODY_ONLY
    # No fidelity-deciding function accepts a content type at all.
    for fn in (effective_acquisition_mode, trusted_mode_for_write, fidelity_for_mode):
        assert not any("content_type" in p for p in inspect.signature(fn).parameters)
