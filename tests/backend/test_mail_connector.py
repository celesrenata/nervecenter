"""Mail-connector plane tests (prompt2 §46 — registry + liveness probe).

Phase 0 integrity (H-1/G-0.2): the mail plane was committed at ``a3c07da``
*without* its tests and *with* a broken import chain (``MailConnector`` was
missing from the re-export in ``backend/connectors/mail/__init__.py``, so a
clean checkout of HEAD could not ``import backend.api.app``). This file pins
the plane's contract so the fix and its tests land together (AC-P03).

Covers:
- IMAP liveness probe → connector-status mapping (OK→CONNECTED,
  AUTH_FAILED→AUTH_EXPIRED, CONNECT_FAILED→ERROR).
- OAuth URL building, callback parsing, token-exchange wire shape, and the
  token-ref round-trip + rejection of raw-secret blobs.
- Registry upsert semantics, 404 on a missing id, and the reset hook.
- HTTP list/create/verify/delete, asserting the secret is *never* persisted
  or returned — only the opaque ``token_ref`` is.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.models import MailAuthMethod, MailConnectorStatus
from backend.connectors import (
    get_mail_connector_service,
    reset_mail_connector_service,
)
from backend.connectors.mail import (
    ImapProbeResult,
    MailConnectorNotFoundError,
    MailConnectorService,
    ProbeResult,
    build_authorization_url,
    exchange_request,
    make_token_ref,
    parse_callback,
    provider,
    validate_token_ref,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _svc(outcome: ImapProbeResult = ImapProbeResult.OK) -> MailConnectorService:
    """A fresh, deterministic service with a scripted probe client."""
    return MailConnectorService(client=_ScriptedProbe(outcome))


class _ScriptedProbe:
    """Minimal ImapClient stub that always returns a fixed outcome."""

    def __init__(self, outcome: ImapProbeResult) -> None:
        self._outcome = outcome

    def probe(self, host: str, port: int, ssl: bool, username: str, password: str) -> ProbeResult:
        return ProbeResult(outcome=self._outcome, host=host, port=port)


@pytest.fixture
def client():
    reset_mail_connector_service()
    app = create_app()
    # Inject a deterministic probe client so the HTTP ``verify`` endpoint never
    # opens a real IMAP socket (the app-level service defaults to ImapImapClient,
    # which would hit the network). The endpoint's ``_service()`` resolves to
    # this same singleton, so the stub is in effect for the whole request.
    get_mail_connector_service()._client = _ScriptedProbe(ImapProbeResult.OK)
    with TestClient(app) as c:
        yield c
    reset_mail_connector_service()


# ---------------------------------------------------------------------------
# (a) IMAP probe → status mapping
# ---------------------------------------------------------------------------


def test_probe_ok_maps_to_connected():
    svc = _svc(ImapProbeResult.OK)
    c = svc.create({"connector_id": "m1", "host": "imap.x", "mailbox": "a@x"})
    c = svc.verify("m1", username="a@x", password="topsecret")
    assert c.status is MailConnectorStatus.CONNECTED
    assert c.last_checked_at is not None
    assert c.last_error is None


def test_probe_auth_failed_maps_to_auth_expired():
    svc = _svc(ImapProbeResult.AUTH_FAILED)
    svc.create({"connector_id": "m1", "host": "imap.x"})
    c = svc.verify("m1", username="a@x", password="stale")
    assert c.status is MailConnectorStatus.AUTH_EXPIRED


def test_probe_connect_failed_maps_to_error():
    svc = _svc(ImapProbeResult.CONNECT_FAILED)
    svc.create({"connector_id": "m1", "host": "imap.x"})
    c = svc.verify("m1", username="a@x", password="x")
    assert c.status is MailConnectorStatus.ERROR


def test_verify_stores_only_status_not_secret():
    svc = _svc(ImapProbeResult.OK)
    c = svc.create({"connector_id": "m1", "host": "imap.x"})
    svc.verify("m1", username="a@x", password="super-secret-token")
    stored = svc.get("m1")
    # The credential is supplied per-call and never persisted.
    assert stored.token_ref is None
    # And the raw secret never leaks into any connector field.
    assert "super-secret-token" not in stored.model_dump(mode="json").__str__()


def test_probe_ok_records_capabilities_not_leaked():
    """The raw IMAP CAPABILITY string is not surfaced on the connector."""
    svc = _svc(ImapProbeResult.OK)
    svc.create({"connector_id": "m1", "host": "imap.x"})
    c = svc.verify("m1", username="a@x", password="x")
    dumped = c.model_dump(mode="json")
    assert "IMAP4rev1" not in str(dumped)


# ---------------------------------------------------------------------------
# (b) OAuth authorization URL
# ---------------------------------------------------------------------------


def test_build_authorization_url_carries_client_id_redirect_state():
    url = build_authorization_url(
        "google", client_id="cid", redirect_uri="https://cb", state="nonce123"
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    q = parse_qs(parsed.query)
    assert q["client_id"] == ["cid"]
    assert q["redirect_uri"] == ["https://cb"]
    assert q["state"] == ["nonce123"]
    assert q["response_type"] == ["code"]
    # The Gmail API readonly scope is the minimal read-only set. The legacy
    # "https://mail.google.com/imap" scope was retired by Google (it is
    # rejected as invalid_scope for new clients) and must NOT be requested.
    scope = q["scope"][0]
    assert "https://www.googleapis.com/auth/gmail.readonly" in scope
    assert "https://mail.google.com/imap" not in scope


def test_build_authorization_url_microsoft_uses_imap_scope():
    url = build_authorization_url(
        "microsoft", client_id="cid", redirect_uri="https://cb", state="s"
    )
    q = parse_qs(urlparse(url).query)
    assert "https://outlook.office365.com/IMAP.AccessAsUser.All" in q["scope"][0]


def test_provider_lookup_rejects_unknown_name():
    assert provider("google").name == "google"
    with pytest.raises(ValueError):
        provider("outlook")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# (c) OAuth callback parsing
# ---------------------------------------------------------------------------


def test_parse_callback_success():
    cb = parse_callback({"code": "code42", "state": "nonce123"})
    assert cb.code == "code42"
    assert cb.state == "nonce123"
    assert cb.error is None


def test_parse_callback_error_surface():
    cb = parse_callback({"error": "access_denied", "state": "nonce123"})
    assert cb.code == ""
    assert cb.state == "nonce123"
    assert cb.error == "access_denied"


def test_parse_callback_state_must_match():
    """The CSRF guard: a mismatched state must be detected by the caller."""
    cb = parse_callback({"code": "c", "state": "attacker-state"})
    # parse_callback surfaces the state; a real flow compares it to the nonce
    # it generated. Here we assert it round-trips so the check is possible.
    assert cb.state == "attacker-state"
    assert cb.state != "my-original-nonce"


# ---------------------------------------------------------------------------
# (d) Token exchange request shape
# ---------------------------------------------------------------------------


def test_exchange_request_wire_shape():
    body = exchange_request(
        "google",
        code="code42",
        redirect_uri="https://cb",
        client_id="cid",
        client_secret="shh",
    )
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "code42"
    assert body["client_id"] == "cid"
    assert body["client_secret"] == "shh"
    assert body["redirect_uri"] == "https://cb"


# ---------------------------------------------------------------------------
# (e) token-ref round-trip + rejection of raw secrets
# ---------------------------------------------------------------------------


def test_make_token_ref_round_trip():
    for store in ("keychain", "vault", "env"):
        ref = make_token_ref(store, "item-1")
        assert ref == f"{store}:item-1"
        assert validate_token_ref(ref) is True


def test_make_token_ref_strips_whitespace():
    assert make_token_ref("keychain", "  item-1  ") == "keychain:item-1"


def test_make_token_ref_rejects_empty_id():
    with pytest.raises(ValueError):
        make_token_ref("keychain", "   ")


def test_validate_token_ref_rejects_raw_secret_blob():
    # A long unstructured token (no known prefix) must be rejected so the
    # registry can never persist the secret itself (§47).
    assert validate_token_ref("A" * 40) is False
    assert validate_token_ref("") is False


def test_validate_token_ref_accepts_prefixed_short_refs():
    assert validate_token_ref("env:SOME_VAR") is True


# ---------------------------------------------------------------------------
# (f) registry upsert / 404
# ---------------------------------------------------------------------------


def test_create_defaults_and_status_pending():
    svc = _svc()
    c = svc.create({"connector_id": "m1", "host": "imap.x"})
    assert c.account == "Default"
    assert c.port == 993
    assert c.ssl is True
    assert c.auth_method is MailAuthMethod.OAUTH2
    assert c.status is MailConnectorStatus.PENDING
    assert c.last_checked_at is None


def test_create_duplicate_id_raises():
    svc = _svc()
    svc.create({"connector_id": "m1"})
    with pytest.raises(ValueError):
        svc.create({"connector_id": "m1"})


def test_create_assigns_uuid_when_id_omitted():
    svc = _svc()
    c = svc.create({"host": "imap.x"})
    assert c.connector_id.startswith("mc-")
    assert len(c.connector_id) > 3


def test_get_missing_raises_not_found():
    svc = _svc()
    with pytest.raises(MailConnectorNotFoundError):
        svc.get("nope")


def test_update_partial_and_resets_status_to_pending():
    svc = _svc(ImapProbeResult.OK)
    svc.create({"connector_id": "m1", "host": "imap.x"})
    svc.verify("m1", username="a@x", password="x")
    updated = svc.update("m1", {"host": "imap2.x", "port": 994})
    assert updated.host == "imap2.x"
    assert updated.port == 994
    # Changing host/port invalidates the last probe → status back to PENDING.
    assert updated.status is MailConnectorStatus.PENDING
    assert updated.last_error is None


def test_update_disallowed_field_ignored():
    svc = _svc()
    svc.create({"connector_id": "m1", "host": "imap.x", "account": "Work"})
    updated = svc.update("m1", {"status": "disabled", "account": "Home"})
    assert updated.account == "Home"
    # status is not in the updatable allow-list → unchanged (still PENDING).
    assert updated.status is MailConnectorStatus.PENDING


def test_delete_missing_raises_not_found():
    svc = _svc()
    with pytest.raises(MailConnectorNotFoundError):
        svc.delete("nope")


# ---------------------------------------------------------------------------
# (g) verify() transitions + set_status
# ---------------------------------------------------------------------------


def test_verify_then_set_status_disabled():
    svc = _svc(ImapProbeResult.OK)
    svc.create({"connector_id": "m1", "host": "imap.x"})
    svc.verify("m1", username="a@x", password="x")
    c = svc.set_status("m1", MailConnectorStatus.DISABLED)
    assert c.status is MailConnectorStatus.DISABLED


def test_verify_missing_raises_not_found():
    svc = _svc(ImapProbeResult.OK)
    with pytest.raises(MailConnectorNotFoundError):
        svc.verify("nope", username="a@x", password="x")


def test_seed_demo_idempotent_and_connects():
    svc = _svc(ImapProbeResult.OK)
    svc.seed_demo()
    svc.seed_demo()  # no-op on a non-empty registry
    rows = svc.list()
    assert len(rows) == 1
    assert rows[0].connector_id == "mc-demo-work"
    assert rows[0].status is MailConnectorStatus.CONNECTED


def test_list_sorted_by_updated_desc():
    svc = _svc()
    svc.create({"connector_id": "a", "host": "imap.x"})
    svc.create({"connector_id": "b", "host": "imap.x"})
    # verify b so its updated_at becomes the most recent.
    svc.verify("b", username="b@x", password="x")
    rows = svc.list()
    assert [r.connector_id for r in rows][0] == "b"


# ---------------------------------------------------------------------------
# (h) reset hook
# ---------------------------------------------------------------------------


def test_reset_mail_connector_service_clears_registry():
    reset_mail_connector_service()
    get_mail_connector_service().create({"connector_id": "m1"})
    assert len(get_mail_connector_service().list()) == 1
    reset_mail_connector_service()
    assert len(get_mail_connector_service().list()) == 0


# ---------------------------------------------------------------------------
# (i) HTTP endpoints
# ---------------------------------------------------------------------------


def test_http_list_empty(client):
    res = client.get("/api/mail-connectors")
    assert res.status_code == 200
    body = res.json()
    assert body["data"] == []
    assert body["meta"]["pagination"]["total"] == 0


def test_http_create_returns_201_with_token_ref_only(client):
    res = client.post(
        "/api/mail-connectors",
        json={
            "connector_id": "work",
            "account": "Work",
            "host": "imap.gmail.com",
            "mailbox": "work@corp.com",
            "token_ref": "keychain:work-imap-token",
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["connector_id"] == "work"
    assert data["status"] == "pending"
    # Only the opaque ref is returned, never a raw secret field.
    assert data["token_ref"] == "keychain:work-imap-token"
    assert "password" not in data
    assert "secret" not in data


def test_http_create_duplicate_409(client):
    payload = {"connector_id": "dup", "host": "imap.x"}
    assert client.post("/api/mail-connectors", json=payload).status_code == 201
    assert client.post("/api/mail-connectors", json=payload).status_code == 409


def test_http_get_404(client):
    assert client.get("/api/mail-connectors/nope").status_code == 404


def test_http_verify_probes_and_maps_status(client):
    client.post(
        "/api/mail-connectors",
        json={"connector_id": "work", "host": "imap.gmail.com", "mailbox": "w@corp"},
    )
    # Fixture injected an OK probe client → the probe outcome maps to CONNECTED.
    res = client.post(
        "/api/mail-connectors/work/verify",
        json={"username": "w@corp", "password": "the-raw-credential"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "connected"
    assert data["last_checked_at"] is not None
    assert data["last_error"] is None
    # The credential was accepted only to run the probe; it is never stored.
    assert data["token_ref"] is None
    assert "the-raw-credential" not in res.text
    # Re-list: the stored record also omits the secret.
    listed = client.get("/api/mail-connectors").json()["data"][0]
    assert "the-raw-credential" not in str(listed)
    assert listed["status"] == "connected"


def test_http_verify_unknown_connector_404(client):
    res = client.post(
        "/api/mail-connectors/nope/verify",
        json={"username": "a@x", "password": "x"},
    )
    assert res.status_code == 404


def test_http_delete_204_then_404(client):
    client.post("/api/mail-connectors", json={"connector_id": "d", "host": "imap.x"})
    assert client.delete("/api/mail-connectors/d").status_code == 204
    assert client.get("/api/mail-connectors/d").status_code == 404


def test_http_patch_updates_field(client):
    client.post(
        "/api/mail-connectors",
        json={"connector_id": "p", "host": "imap.x", "account": "Work"},
    )
    res = client.patch("/api/mail-connectors/p", json={"account": "Personal"})
    assert res.status_code == 200, res.text
    assert res.json()["account"] == "Personal"
