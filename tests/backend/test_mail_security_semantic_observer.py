"""SHADOW semantic observer wiring (NS-ADR-073, G8 item 3).

Pinned here:
- opt-in per account; nothing is sent for an account that is not listed;
- the evidence packet is bounded and never carries filenames or raw MIME;
- every call goes through the real LocalOnlyRouteResolver: a non-secure or
  cloud-reaching Reader combo is denied and the chat transport is never called;
- the request is tool-free and uses the guarded alias;
- the stored observation is enum codes only (no ``gist`` text);
- the hook runs after the verdict commits and never changes it;
- the HTTP transports: exact registry paths, no redirects, tool-call replies
  are handed to the validator (which rejects them).
"""
from __future__ import annotations

import json
from typing import Any

import _mail_security_harness_helpers as H
import httpx
import pytest
from sqlalchemy import select
from test_mail_security_observer_route import BASE, Combos, Registry, _row

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.connectors import omniroute_observer as oo
from backend.mail_security import telemetry
from backend.mail_security.granite_observer import FORBIDDEN_REQUEST_KEYS, MAX_BODY_CHARS
from backend.mail_security.observer_wiring import (
    ENV_OBSERVER_SCOPES,
    ObserverRuntime,
    build_packet,
    observer_scopes,
    set_observer_runtime,
)
from backend.mail_security.parser import parse_mime_evidence
from backend.mail_security.route_guard import LocalOnlyRouteResolver
from backend.persistence.models.mail_security import (
    MailSecurityAssessmentRow,
    MailSemanticObservationRow,
)
from backend.persistence.repositories.mail_security_observation_repo import (
    MailObservationRepository,
)

SCOPE = "conn:11111111-2222-3333-4444-555555555555"
GIST_CANARY = "GIST-CANARY-do-not-store-4e1f"


def _mime() -> bytes:
    return (
        "From: Billing <billing@vendor.example>\r\nSubject: Invoice 4471 due\r\n"
        "MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n"
        "--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        "<p>Please pay the attached invoice today at "
        '<a href="https://pay.vendor.example/x">our portal</a>. ' + ("filler " * 900) + "</p>\r\n"
        "--b\r\nContent-Type: application/pdf\r\n"
        'Content-Disposition: attachment; filename="secret-salary-list.pdf"\r\n\r\n'
        "%PDF-1.4 fake\r\n--b--\r\n"
    ).encode()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", frozenset()), ("*", None), (SCOPE, frozenset({SCOPE})), ("conn:bad", frozenset())],
)
def test_observer_scopes(raw: str, expected) -> None:
    assert observer_scopes({ENV_OBSERVER_SCOPES: raw}) == expected


def test_packet_is_bounded_and_has_no_filenames() -> None:
    ev = parse_mime_evidence(_mime())
    p = build_packet("k1:conn:x:src:abc", ev, ["DECEPTIVE", "not a code", "REPLY_TO_MISMATCH"])
    assert p.subject == "Invoice 4471 due"
    assert len(p.body_excerpt) <= MAX_BODY_CHARS and "Please pay" in p.body_excerpt
    assert [link.host for link in p.links] == ["pay.vendor.example"]
    assert [a.kind for a in p.attachments] == ["application/pdf"]
    assert "salary" not in repr(p)
    assert p.deterministic_codes == ("DECEPTIVE", "REPLY_TO_MISMATCH")


class Chat:
    """Fake chat transport: records requests, returns a compact reply."""

    def __init__(self, reply: dict[str, Any] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply = reply or {
            "category": "BILL_OR_INVOICE",
            "gist": f"Invoice payment request {GIST_CANARY}",
            "sender_role": "BUSINESS",
            "requests": ["PAY HIGH 0"],
            "link_risk": [],
            "attachment_risk": [],
            "signals": ["PAYMENT_REQUEST"],
        }

    async def complete(self, request, route) -> str:
        self.requests.append(dict(request))
        return json.dumps(self.reply)


class MemRepo:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def exists(self, scope, source_key, revision) -> bool:
        return any((r["scope"], r["source_key"], r["revision"]) == (scope, source_key, revision)
                   for r in self.rows)

    async def record(self, scope, **kw) -> bool:
        self.rows.append({"scope": scope, **kw})
        return True


def _runtime(chat: Chat, repo: Any, combo_rows=None) -> ObserverRuntime:
    return ObserverRuntime(
        combos=Combos(combo_rows or [_row()]),
        resolver=LocalOnlyRouteResolver(Registry(), omniroute_base_url=BASE),
        transport=chat,
        repository=repo,
    )


async def test_runtime_observes_through_guard_and_stores_codes_only(monkeypatch) -> None:
    monkeypatch.setenv(ENV_OBSERVER_SCOPES, SCOPE)
    chat, repo = Chat(), MemRepo()
    ev = parse_mime_evidence(_mime())
    status = await _runtime(chat, repo).observe(
        SCOPE, source_key="src1", revision=2, evidence=ev, finding_codes=[]
    )
    assert status == "COMPLETED"
    (req,) = chat.requests
    assert req["model"] == "secure/reader"
    assert not FORBIDDEN_REQUEST_KEYS & req.keys()
    (row,) = repo.rows
    assert row["status"] == "COMPLETED" and row["revision"] == 2 and row["route_digest"]
    obs = row["observation"]
    assert obs["message_category"] == "BILL_OR_INVOICE"
    assert any(r["request_type"] == "PAY" for r in obs["requests"])
    assert GIST_CANARY not in json.dumps(row)  # the free-text gist is never stored
    # Replays are no-ops.
    again = await _runtime(chat, repo).observe(
        SCOPE, source_key="src1", revision=2, evidence=ev, finding_codes=[]
    )
    assert again == "ALREADY_OBSERVED" and len(chat.requests) == 1


async def test_non_secure_reader_is_denied_and_nothing_is_sent(monkeypatch) -> None:
    monkeypatch.setenv(ENV_OBSERVER_SCOPES, "*")
    chat, repo = Chat(), MemRepo()
    for model in ("hybrid/reader", "local/reader"):
        status = await _runtime(chat, repo, [_row(model=model)]).observe(
            SCOPE, source_key=f"s-{model}", revision=1,
            evidence=parse_mime_evidence(_mime()), finding_codes=[],
        )
        assert status.startswith("ROUTE_DENIED")
    assert chat.requests == []
    assert all(r["status"].startswith("ROUTE_DENIED") for r in repo.rows)


async def test_not_opted_in_sends_and_records_nothing(monkeypatch) -> None:
    monkeypatch.setenv(ENV_OBSERVER_SCOPES, "conn:99999999-2222-3333-4444-555555555555")
    chat, repo = Chat(), MemRepo()
    status = await _runtime(chat, repo).observe(
        SCOPE, source_key="s", revision=1, evidence=parse_mime_evidence(_mime()), finding_codes=[]
    )
    assert status == "NOT_OPTED_IN" and chat.requests == [] and repo.rows == []


async def test_invalid_model_output_is_recorded_as_typed_error(monkeypatch) -> None:
    monkeypatch.setenv(ENV_OBSERVER_SCOPES, "*")
    chat, repo = Chat(reply={"tool_calls": [{"function": {"name": "memory_save"}}]}), MemRepo()
    status = await _runtime(chat, repo).observe(
        SCOPE, source_key="s", revision=1, evidence=parse_mime_evidence(_mime()), finding_codes=[]
    )
    assert status != "COMPLETED"
    assert repo.rows[0]["status"] == status and repo.rows[0].get("observation") in (None, {})


# -- worker hook --------------------------------------------------------------------------
@pytest.fixture
async def harness(tmp_path):
    h = await H.make_harness(tmp_path / "observer.sqlite")
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


async def test_worker_hook_records_after_verdict_and_never_changes_it(harness, monkeypatch) -> None:
    monkeypatch.setenv(ENV_OBSERVER_SCOPES, "*")
    chat = Chat()
    repo = MailObservationRepository(harness.registry.session_factory)
    set_observer_runtime(_runtime(chat, repo))
    await H.ingest(harness, monkeypatch, H.mailitem("<obs-1@x>", _mime()))
    await H.security_worker(harness).process_one()
    async with harness.registry.session_factory() as s:
        (obs,) = (await s.execute(select(MailSemanticObservationRow))).scalars().all()
        (asmt,) = (await s.execute(select(MailSecurityAssessmentRow))).scalars().all()
    assert obs.status == "COMPLETED" and obs.account_scope == harness.scope
    assert asmt.assessment.get("model_runs") == []  # verdict is the deterministic one
    set_observer_runtime(None)
    # Without a runtime the same pipeline still assesses.
    await H.ingest(harness, monkeypatch, H.mailitem("<obs-2@x>", H.benign_mime()), uid=6)
    await H.security_worker(harness).process_one()
    assert len(chat.requests) == 1


# -- HTTP transports ------------------------------------------------------------------------
def _patch_httpx(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = httpx.AsyncClient

    def factory(**kw):
        return real(transport=httpx.MockTransport(wrapped), **kw)

    monkeypatch.setattr(oo.httpx, "AsyncClient", factory)
    return seen


async def test_registry_transport_paths_and_redirects(monkeypatch) -> None:
    seen = _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(302, headers={"location": "https://evil.example/"})
        if r.url.path == "/api/providers" else httpx.Response(200, json={"combos": []}),
    )
    t = oo.HttpRegistryTransport("http://omni:20128/v1", "sk-test")
    assert await t.get_json("/api/combos") == {"combos": []}
    assert seen[0].url.path == "/api/combos" and seen[0].method == "GET"
    with pytest.raises(ValueError):
        await t.get_json("/api/providers")  # redirect is not followed
    with pytest.raises(ValueError):
        await t.get_json("/api/settings")  # not one of the three paths
    assert all(r.url.host == "omni" for r in seen)


async def test_observer_transport_text_and_tool_calls(monkeypatch) -> None:
    replies = iter([
        httpx.Response(200, json={"choices": [{"message": {"content": "{\"a\": 1}"}}]}),
        httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]}),
        httpx.Response(500, json={"error": "sk-test leaked?"}),
    ])
    seen = _patch_httpx(monkeypatch, lambda r: next(replies))
    t = oo.HttpObserverTransport("http://omni:20128/v1", "sk-test")
    assert await t.complete({"model": "secure/reader"}, None) == "{\"a\": 1}"
    assert "tool_calls" in await t.complete({"model": "secure/reader"}, None)
    with pytest.raises(ValueError) as exc:
        await t.complete({"model": "secure/reader"}, None)
    assert "sk-test" not in str(exc.value)
    assert seen[0].url.path == "/v1/chat/completions"
    assert seen[0].headers["x-omniroute-no-memory"] == "true"


# -- per-message registry snapshot (NS-ADR-074) ---------------------------------------------
class CountingRegistry(Registry):
    """The fixture registry, counting GETs and failing the first N reads."""

    def __init__(self, fail_first: int = 0) -> None:
        super().__init__()
        self.fail_first = fail_first

    async def get_json(self, path: str) -> Any:
        if self.fail_first > 0:
            self.fail_first -= 1
            self.calls.append(f"FAIL {path}")
            raise ValueError("registry blip")
        return await super().get_json(path)


def _snapshot_runtime(chat: Chat, repo: Any, registry: Any, rows=None) -> ObserverRuntime:
    return ObserverRuntime(
        combos=Combos(rows or [_row()]),
        registry=registry,
        resolver_factory=lambda reg: LocalOnlyRouteResolver(reg, omniroute_base_url=BASE),
        transport=chat,
        repository=repo,
    )


async def test_snapshot_shared_within_a_message_and_purged_after(monkeypatch) -> None:
    monkeypatch.setenv(ENV_OBSERVER_SCOPES, "*")
    reg, chat, repo = CountingRegistry(), Chat(), MemRepo()
    rt = _snapshot_runtime(chat, repo, reg)
    ev = parse_mime_evidence(_mime())
    assert await rt.observe(SCOPE, source_key="a", revision=1, evidence=ev,
                            finding_codes=[]) == "COMPLETED"
    # Two route checks, one snapshot: each registry path fetched exactly once.
    assert sorted(reg.calls) == ["/api/combos", "/api/providers", "/v1/models"]
    assert rt.last_snapshot is not None and rt.last_snapshot.cached_paths == frozenset()
    # The next message re-reads the registry (nothing survives between messages).
    await rt.observe(SCOPE, source_key="b", revision=1, evidence=ev, finding_codes=[])
    assert len(reg.calls) == 6


async def test_snapshot_retries_one_blip_and_still_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv(ENV_OBSERVER_SCOPES, "*")
    ev = parse_mime_evidence(_mime())
    ok = _snapshot_runtime(Chat(), MemRepo(), CountingRegistry(fail_first=1))
    assert await ok.observe(SCOPE, source_key="a", revision=1, evidence=ev,
                            finding_codes=[]) == "COMPLETED"
    chat = Chat()
    down = _snapshot_runtime(chat, MemRepo(), CountingRegistry(fail_first=99))
    status = await down.observe(SCOPE, source_key="b", revision=1, evidence=ev, finding_codes=[])
    assert status.startswith("ROUTE_DENIED") and chat.requests == []
    assert down.last_snapshot.cached_paths == frozenset()


async def test_snapshot_purged_even_when_observation_raises(monkeypatch) -> None:
    monkeypatch.setenv(ENV_OBSERVER_SCOPES, "*")

    class Boom(MemRepo):
        async def record(self, scope, **kw) -> bool:
            raise RuntimeError("db down")

    rt = _snapshot_runtime(Chat(), Boom(), CountingRegistry())
    with pytest.raises(RuntimeError):
        await rt.observe(SCOPE, source_key="a", revision=1,
                         evidence=parse_mime_evidence(_mime()), finding_codes=[])
    assert rt.last_snapshot.cached_paths == frozenset()


def test_runtime_requires_a_resolver_or_a_snapshot_factory() -> None:
    with pytest.raises(ValueError):
        ObserverRuntime(combos=Combos([]), transport=Chat(), repository=MemRepo())
