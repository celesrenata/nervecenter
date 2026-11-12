"""NerveShield full-email capture (NS-ADR-064, G8 item 1).

Gmail ``format=raw`` bytes are fetched (opt-in), stored as a SEPARATE encrypted
object under ``raw/<conn>/<digest>``, and scanned by the security worker. The
job ``meta.content_ref`` stays the decoded body ref so the 7.1 safety-card join
(``source_key_for_content_ref``) and the 7.4 gate join (``latest_verdict``)
are unchanged.

Pinned here:
- flag OFF is byte-identical: no raw request, no raw object, same job meta;
- ``ORIGINAL_RFC822`` is reachable only through the trusted Gmail raw seam;
- non-Gmail cursors never raw-capture; Takeout/EML/IMAP modes stay demoted;
- the size cap and every raw-fetch failure degrade to body-only, never fail;
- a missing raw object fails closed to INCOMPLETE (no silent body fallback);
- raw objects are encrypted at rest through the encrypted store.
"""
from __future__ import annotations

import base64
import os
from typing import Any

import _mail_security_harness_helpers as H
import httpx
import pytest
from test_mail_transport import (
    _ACCOUNT,
    FakeGmailClient,
    FakeGmailResp,
    _connector,
    _gentry,
    _gresource,
    _install_gmail,
)

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.connectors.mail.mail_transport import GmailClient, MailItem
from backend.ingest import supervisor as sup
from backend.ingest.mail_worker import (
    DefaultIngestSeam,
    MailIngestWorker,
    _content_key,
    _raw_content_key,
)
from backend.mail_security import telemetry
from backend.mail_security.acquisition import ACQUISITION_META_KEY, RAW_REF_META_KEY
from backend.mail_security.ingestion_seam import (
    ENV_MAIL_SECURITY_ENABLED,
    ENV_MAIL_SECURITY_FULL_CAPTURE,
    RAW_CAPTURE_MAX_BYTES,
    full_capture_enabled,
    set_mail_security_repository,
)
from backend.persistence.repositories.mail_security_gate_repo import (
    HeldPromotionRepository,
)
from backend.storage.encrypted import EncryptedObjectStore, keyring_from_value
from backend.storage.memory import InMemoryObjectStore

RAW_MIME = H.forged_auth_mime()


# --------------------------------------------------------------------------
# flag
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, False),
        ({ENV_MAIL_SECURITY_FULL_CAPTURE: "1"}, False),  # never on its own
        ({ENV_MAIL_SECURITY_ENABLED: "1"}, False),
        ({ENV_MAIL_SECURITY_ENABLED: "1", ENV_MAIL_SECURITY_FULL_CAPTURE: "0"}, False),
        ({ENV_MAIL_SECURITY_ENABLED: "1", ENV_MAIL_SECURITY_FULL_CAPTURE: "1"}, True),
    ],
)
def test_full_capture_requires_both_flags(env: dict[str, str], expected: bool) -> None:
    assert full_capture_enabled(env) is expected


def test_supervisor_builds_raw_client_only_when_opted_in(monkeypatch) -> None:
    monkeypatch.delenv(ENV_MAIL_SECURITY_FULL_CAPTURE, raising=False)
    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
    off = sup._build_transport("gmail", {})
    assert isinstance(off, GmailClient) and off._capture_raw is False
    monkeypatch.setenv(ENV_MAIL_SECURITY_FULL_CAPTURE, "1")
    on = sup._build_transport("gmail", {})
    assert on._capture_raw is True and on._raw_max_bytes == RAW_CAPTURE_MAX_BYTES
    # IMAP never gets a raw path.
    assert not hasattr(sup._build_transport("imap", {}), "_capture_raw")


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------
class RawFakeGmail(FakeGmailClient):
    """The Gmail fake plus ``format=raw`` (scripted per message)."""

    def __init__(self, *, raw: dict[str, Any], **kw: Any) -> None:
        super().__init__(**kw)
        self._raw = raw
        self.raw_calls: list[str] = []
        self.formats: list[str] = []

    async def get(self, url: str, *, headers=None, params=None):  # type: ignore[override]
        fmt = str((params or {}).get("format") or "")
        if "/messages/" in url and fmt:
            self.formats.append(fmt)
        if "/messages/" in url and fmt == "raw":
            mid = url.rsplit("/", 1)[-1]
            self.raw_calls.append(mid)
            entry = self._raw.get(mid)
            if isinstance(entry, Exception):
                raise entry
            if entry is None:
                return FakeGmailResp(404, {"error": {"code": 404}})
            status, body = entry
            return FakeGmailResp(status, body)
        return await super().get(url, headers=headers, params=params)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _fake(raw: dict[str, Any]) -> RawFakeGmail:
    return RawFakeGmail(
        raw=raw,
        entries=[_gentry(1, 5, "m1")],
        messages={"m1": (200, _gresource("m1", 1))},
        head_history_id=5,
    )


async def _run(client: GmailClient, fake: RawFakeGmail, monkeypatch) -> list[MailItem]:
    _install_gmail(monkeypatch, fake)
    items, _ = await client.run(
        _connector(), {"history_id": 5, "account": _ACCOUNT.lower()},
        credentials={"access_token": "tok"},
    )
    return items


async def test_capture_off_makes_no_raw_request(monkeypatch) -> None:
    fake = _fake({"m1": (200, {"raw": _b64(RAW_MIME)})})
    items = await _run(GmailClient(), fake, monkeypatch)
    assert fake.raw_calls == [] and fake.formats == ["full"]
    assert [i.raw_bytes for i in items] == [None]


async def test_capture_on_fetches_raw_and_keeps_body_identical(monkeypatch) -> None:
    off = await _run(GmailClient(), _fake({}), monkeypatch)
    fake = _fake({"m1": (200, {"raw": _b64(RAW_MIME), "sizeEstimate": len(RAW_MIME)})})
    on = await _run(GmailClient(capture_raw=True), fake, monkeypatch)
    assert fake.formats == ["full", "raw"]
    assert on[0].raw_bytes == RAW_MIME
    # Everything else about the item is exactly the body-only item.
    for field in ("message_id", "account", "subject", "content_bytes", "timestamp", "headers"):
        assert getattr(on[0], field) == getattr(off[0], field)


async def test_capture_on_resync_path_also_fetches_raw(monkeypatch) -> None:
    fake = RawFakeGmail(
        raw={"m1": (200, {"raw": _b64(RAW_MIME)})},
        messages={"m1": (200, _gresource("m1", 1))},
        head_history_id=9,
        inbox_message_ids=["m1"],
    )
    _install_gmail(monkeypatch, fake)
    items, _ = await GmailClient(capture_raw=True).run(
        _connector(), {}, credentials={"access_token": "tok"}
    )
    assert [i.raw_bytes for i in items] == [RAW_MIME]


@pytest.mark.parametrize(
    "raw_entry",
    [
        (500, {"error": {"code": 500}}),
        (403, {"error": {"code": 403}}),
        (200, {"raw": "@@not base64@@"}),
        (200, {"raw": ""}),
        (200, {}),
        httpx.ReadTimeout("slow"),
        None,  # 404
    ],
)
async def test_raw_fetch_failure_degrades_to_body_only(monkeypatch, raw_entry) -> None:
    fake = _fake({"m1": raw_entry} if raw_entry is not None else {})
    items = await _run(GmailClient(capture_raw=True), fake, monkeypatch)
    assert len(items) == 1
    assert items[0].raw_bytes is None
    assert items[0].content_bytes  # the body-only item still ingests


async def test_raw_size_cap_is_enforced(monkeypatch) -> None:
    big = b"X" * 2048
    fake = _fake({"m1": (200, {"raw": _b64(big)})})
    items = await _run(GmailClient(capture_raw=True, raw_max_bytes=1024), fake, monkeypatch)
    assert items[0].raw_bytes is None
    fake = _fake({"m1": (200, {"raw": _b64(big)})})
    items = await _run(GmailClient(capture_raw=True, raw_max_bytes=2048), fake, monkeypatch)
    assert items[0].raw_bytes == big


# --------------------------------------------------------------------------
# ingest worker + security worker (end to end)
# --------------------------------------------------------------------------
@pytest.fixture
async def harness(tmp_path):
    h = await H.make_harness(tmp_path / "full_capture.sqlite")
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


def _gmail_item(mid: str, raw: bytes | None) -> MailItem:
    base = H.mailitem(mid, b"Your statement is ready.\n")
    return MailItem(
        message_id=base.message_id, account=base.account, subject=base.subject,
        content_bytes=base.content_bytes, timestamp=base.timestamp,
        headers=dict(base.headers), source="gmail", raw_bytes=raw,
    )


async def _ingest(
    harness, monkeypatch, item: MailItem, *, full: bool,
    cursor_kind: str = "gmail_history", store: Any = None,
) -> None:
    from backend.connectors.mail.mail_transport import StubMailTransport

    monkeypatch.setenv(ENV_MAIL_SECURITY_ENABLED, "1")
    monkeypatch.setenv(ENV_MAIL_SECURITY_FULL_CAPTURE, "1" if full else "0")
    set_mail_security_repository(harness.registry.mail_security)
    worker = MailIngestWorker(
        connection_id=harness.conn.id,
        cursor_kind=cursor_kind,
        connections=harness.registry.connections,
        imports=harness.registry.imports,
        object_store=store or harness.store,
        transport=StubMailTransport(items=[item], next_cursor={"history_id": 9}),
        seam=DefaultIngestSeam(communications_store=harness.registry.communications),
        poll_interval_s=30.0,
    )
    await worker.poll_once()


async def _only_job(harness) -> Any:
    jobs = await harness.registry.mail_security.list_jobs(harness.scope)
    assert len(jobs) == 1
    return jobs[0]


def _body_ref(harness, mid: str) -> str:
    return f"content://{_content_key(harness.conn.id, mid)}"


def _raw_ref(harness, mid: str) -> str:
    return f"content://{_raw_content_key(harness.conn.id, mid)}"


async def test_flag_off_is_byte_identical_to_body_only(harness, monkeypatch) -> None:
    mid = "gmail:off1"
    await _ingest(harness, monkeypatch, _gmail_item(mid, RAW_MIME), full=False)
    job = await _only_job(harness)
    assert job.meta[ACQUISITION_META_KEY] == "LEGACY_BODY_ONLY"
    assert RAW_REF_META_KEY not in job.meta
    assert job.meta["content_ref"] == _body_ref(harness, mid)
    assert not await harness.store.exists(_raw_content_key(harness.conn.id, mid))


async def test_flag_off_meta_equals_no_raw_item(tmp_path, monkeypatch) -> None:
    metas = []
    for i, raw in enumerate((RAW_MIME, None)):
        h = await H.make_harness(tmp_path / f"p{i}.sqlite")
        try:
            await _ingest(h, monkeypatch, _gmail_item("gmail:same", raw), full=False)
            job = await _only_job(h)
            meta = dict(job.meta)
            # The body key digests the (per-harness) connection id; pin its
            # exact value, then compare the rest of the meta across runs.
            assert meta.pop("content_ref") == _body_ref(h, "gmail:same")
            metas.append(meta)
            assert await h.store.exists(_content_key(h.conn.id, "gmail:same"))
            assert not await h.store.exists(_raw_content_key(h.conn.id, "gmail:same"))
        finally:
            await H.teardown_harness(h)
    assert metas[0] == metas[1]


async def test_flag_on_stores_raw_and_reaches_original_fidelity(harness, monkeypatch) -> None:
    mid = "gmail:on1"
    await _ingest(harness, monkeypatch, _gmail_item(mid, RAW_MIME), full=True)
    job = await _only_job(harness)
    assert job.meta[ACQUISITION_META_KEY] == "GMAIL_RAW"
    assert job.meta["content_ref"] == _body_ref(harness, mid)  # 7.1/7.4 join key
    assert job.meta[RAW_REF_META_KEY] == _raw_ref(harness, mid)
    assert await harness.store.get(_raw_content_key(harness.conn.id, mid)) == RAW_MIME
    assert await harness.store.get(_content_key(harness.conn.id, mid)) == (
        b"Your statement is ready.\n"
    )

    await H.security_worker(harness).process_one()
    repo = harness.registry.mail_security
    (row,) = await repo.list_assessments(harness.scope)
    assert row.evidence_fidelity == "ORIGINAL_RFC822"
    manifest = await repo.get_evidence(harness.scope, job.source_key, 1)
    assert manifest.acquisition_mode == "GMAIL_RAW"
    assert manifest.missing_parts == []
    assert manifest.raw_content_ref.startswith("content:raw_")
    assert manifest.normalized_content_ref.startswith("content:mail_")

    # 7.1 safety-card join and 7.4 gate join still key on the BODY ref.
    body_ref = _body_ref(harness, mid)
    assert await repo.source_key_for_content_ref(harness.scope, body_ref) == job.source_key
    holds = HeldPromotionRepository(harness.registry.session_factory)
    verdict = await holds.latest_verdict(harness.scope, body_ref)
    assert verdict is not None and ":asmt:" in verdict[1]
    # The raw ref is NOT a join key: nothing resolves through it.
    raw_ref = _raw_ref(harness, mid)
    assert await repo.source_key_for_content_ref(harness.scope, raw_ref) is None
    assert await holds.latest_verdict(harness.scope, raw_ref) is None


async def test_non_gmail_cursor_never_raw_captures(harness, monkeypatch) -> None:
    mid = "<imap-1@example.com>"
    await _ingest(
        harness, monkeypatch, _gmail_item(mid, RAW_MIME), full=True, cursor_kind="imap_seq"
    )
    job = await _only_job(harness)
    assert job.meta[ACQUISITION_META_KEY] == "LEGACY_BODY_ONLY"
    assert RAW_REF_META_KEY not in job.meta
    assert not await harness.store.exists(_raw_content_key(harness.conn.id, mid))


class _RawPutFails(InMemoryObjectStore):
    async def put(self, key, data, **kw):  # type: ignore[override]
        if key.startswith("raw/"):
            raise OSError("disk full")
        return await super().put(key, data, **kw)


async def test_raw_store_failure_keeps_body_only_ingest(harness, monkeypatch) -> None:
    store = _RawPutFails()
    harness.store = store
    await _ingest(harness, monkeypatch, _gmail_item("gmail:f1", RAW_MIME), full=True, store=store)
    job = await _only_job(harness)
    assert job.meta[ACQUISITION_META_KEY] == "LEGACY_BODY_ONLY"
    assert RAW_REF_META_KEY not in job.meta
    assert len(await harness.registry.communications.list_communications()) == 1


async def test_oversized_raw_is_not_stored(harness, monkeypatch) -> None:
    import backend.mail_security.ingestion_seam as seam_mod

    monkeypatch.setattr(seam_mod, "RAW_CAPTURE_MAX_BYTES", 16)
    await _ingest(harness, monkeypatch, _gmail_item("gmail:big", RAW_MIME), full=True)
    job = await _only_job(harness)
    assert job.meta[ACQUISITION_META_KEY] == "LEGACY_BODY_ONLY"


async def test_missing_raw_object_fails_closed_not_to_body(harness, monkeypatch) -> None:
    mid = "gmail:gone"
    await _ingest(harness, monkeypatch, _gmail_item(mid, RAW_MIME), full=True)
    await harness.store.delete(_raw_content_key(harness.conn.id, mid))
    await H.security_worker(harness).process_one()
    (row,) = await harness.registry.mail_security.list_assessments(harness.scope)
    assert row.processing_state == "INCOMPLETE"
    assert row.evidence_fidelity != "ORIGINAL_RFC822"


async def test_raw_object_is_encrypted_at_rest(harness, monkeypatch) -> None:
    inner = InMemoryObjectStore()
    keyring = keyring_from_value("k1:" + base64.b64encode(os.urandom(32)).decode())
    store = EncryptedObjectStore(inner, keyring)
    harness.store = store
    mid = "gmail:enc"
    await _ingest(harness, monkeypatch, _gmail_item(mid, RAW_MIME), full=True, store=store)
    key = _raw_content_key(harness.conn.id, mid)
    sealed = await inner.get(key)
    assert RAW_MIME not in sealed and b"Authentication-Results" not in sealed
    assert await store.get(key) == RAW_MIME
    await H.security_worker(harness).process_one()
    (row,) = await harness.registry.mail_security.list_assessments(harness.scope)
    assert row.evidence_fidelity == "ORIGINAL_RFC822"
