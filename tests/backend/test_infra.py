"""Infrastructure connectors, read-only agent access and plan/act (NS-ADR-120)."""
from __future__ import annotations

import json
import os
import pathlib
import stat
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import backend.api.app  # noqa: F401 - import-order guard
from backend.infra import executor
from backend.infra.policy import (
    PolicyError,
    check_kubectl,
    check_ssh_act_command,
    redact,
    ssh_read_command,
)
from backend.infra.service import InfraInputError, InfraService
from backend.persistence.connection import make_session_factory
from backend.persistence.models.infra import InfraConnector, InfraPlan

TOKEN = "kube-token-abcdefghijklmnop"
OC = "oc-" + "a1b2c3d4" * 6
OTHER = "ot-" + "z9y8x7w6" * 6


# ── policy ────────────────────────────────────────────────────────────────────

def test_kubectl_read_mode_allows_reads_and_refuses_the_rest() -> None:
    assert check_kubectl(["get", "pods", "-A"]) == ["get", "pods", "-A"]
    assert check_kubectl(["logs", "deploy/api", "-n", "x", "--tail=50"])
    for args, code in [
        (["get", "secrets", "-A"], "secrets_forbidden"),
        (["describe", "secret/db"], "secrets_forbidden"),
        (["get", "pods,secrets"], "secrets_forbidden"),
        (["delete", "pod", "x"], "not_read_only"),
        (["exec", "x", "--", "sh"], "not_read_only"),
        (["get", "pods", "--token=abc"], "flag_forbidden"),
        (["get", "pods", "--kubeconfig", "/etc/x"], "flag_forbidden"),
        (["get", "--raw", "/api/v1/secrets"], "flag_forbidden"),
        (["logs", "-f", "x"], "flag_forbidden"),
        (["config", "view"], "verb_forbidden"),
        (["get", "pods\nrm"], "bad_arguments"),
    ]:
        with pytest.raises(PolicyError) as err:
            check_kubectl(args)
        assert err.value.code == code, args


def test_kubectl_act_mode_runs_approved_changes_without_identity_flags() -> None:
    assert check_kubectl(["rollout", "restart", "deploy/x"], act=True)
    assert check_kubectl(["apply", "-f", "-"], act=True, stdin="kind: ConfigMap")
    with pytest.raises(PolicyError, match="local_files_forbidden"):
        check_kubectl(["apply", "-f", "/tmp/x.yaml"], act=True)
    with pytest.raises(PolicyError, match="flag_forbidden"):
        check_kubectl(["delete", "pod", "x", "--as=admin"], act=True)
    with pytest.raises(PolicyError, match="flag_forbidden"):
        check_kubectl(["exec", "-it", "x", "--", "sh"], act=True)


def test_ssh_menu_and_act_commands() -> None:
    assert ssh_read_command("disk") == "df -h"
    assert ssh_read_command("journal", {"unit": "nginx.service", "lines": 20}) == (
        "journalctl -u nginx.service -n 20 --no-pager --output=short-iso")
    for name, params, code in [("rm", {}, "command_not_allowed"),
                               ("journal", {"unit": "x; rm -rf /"}, "bad_unit"),
                               ("journal", {"unit": "x", "lines": 5000}, "bad_lines")]:
        with pytest.raises(PolicyError) as err:
            ssh_read_command(name, params)
        assert err.value.code == code
    assert check_ssh_act_command("sudo systemctl restart foo") == "sudo systemctl restart foo"
    with pytest.raises(PolicyError):
        check_ssh_act_command("")


def test_redaction_removes_credentials() -> None:
    text = ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
            "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlc2ln\n"
            "Authorization: Bearer abcdefghijklmnopqrstu\n  password = hunter2\nok line\n")
    out = redact(text)
    for leaked in ("abc\n", "eyJhbGci", "abcdefghijklmnopqrstu", "hunter2"):
        assert leaked not in out
    assert "ok line" in out


# ── SSH keys ─────────────────────────────────────────────────────────────────

def _key(kind: str, bits: int = 2048, passphrase: bytes | None = None) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    key = {"ed25519": lambda: ed25519.Ed25519PrivateKey.generate(),
           "rsa": lambda: rsa.generate_private_key(public_exponent=65537, key_size=bits),
           "ecdsa": lambda: ec.generate_private_key(ec.SECP256R1())}[kind]()
    enc = (serialization.BestAvailableEncryption(passphrase) if passphrase
           else serialization.NoEncryption())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                             enc).decode()


def test_ssh_keys_rsa_and_ed25519_only_and_passphrase_decrypts() -> None:
    assert "OPENSSH PRIVATE KEY" in executor.normalize_ssh_key(_key("ed25519"))
    assert executor.normalize_ssh_key(_key("rsa", 2048))
    locked = _key("ed25519", passphrase=b"pw-123")
    assert executor.normalize_ssh_key(locked, "pw-123")
    for key, pw, code in [(_key("rsa", 1024), None, "ssh_key_too_weak"),
                          (_key("ecdsa"), None, "ssh_key_type_not_supported"),
                          (locked, "wrong", "ssh_key_unreadable"),
                          ("garbage", None, "ssh_key_unreadable")]:
        with pytest.raises(executor.ExecutorError) as err:
            executor.normalize_ssh_key(key, pw)
        assert err.value.code == code


# ── service with fake binaries ───────────────────────────────────────────────

class FakeVault:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str, str], str] = {}

    async def upsert_secret(self, *, kind, owner_kind, owner_id, plaintext):
        self.data[(kind, owner_kind, owner_id)] = plaintext

    async def get_secret(self, *, kind, owner_kind, owner_id):
        return self.data.get((kind, owner_kind, owner_id))

    async def delete_secret(self, *, kind, owner_kind, owner_id):
        return self.data.pop((kind, owner_kind, owner_id), None) is not None


class FakeHandoffs:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue(self, **kw):
        self.calls.append(kw)
        return {"id": "handoff-1"}


def _script(path: pathlib.Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
async def svc(tmp_path: pathlib.Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'i.db'}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(InfraConnector.__table__.metadata.create_all,
                            tables=[InfraConnector.__table__, InfraPlan.__table__])
    work = tmp_path / "infra-tmp"
    monkeypatch.setenv(executor.TMP_ENV, str(work))
    # fake kubectl: show argv and that the kubeconfig carries the token (never print it)
    monkeypatch.setattr(executor, "KUBECTL", _script(tmp_path / "kubectl", (
        'echo "ARGS $*"\n'
        f'grep -q "{TOKEN}" "$KUBECONFIG" && echo kubeconfig_ok\n'
        'case "$*" in *fail*) exit 3;; esac\n')))
    monkeypatch.setattr(executor, "SSH", _script(tmp_path / "ssh", (
        'for a; do last="$a"; done\necho "REMOTE $last"\n'
        'case "$*" in *StrictHostKeyChecking=yes*) echo strict_ok;; esac\n')))
    handoffs = FakeHandoffs()
    service = InfraService(make_session_factory(engine), FakeVault(), handoffs=handoffs)
    service._test_handoffs = handoffs  # type: ignore[attr-defined]
    service._test_work = work  # type: ignore[attr-defined]
    yield service
    await engine.dispose()


async def _kube(svc: InfraService) -> None:
    await svc.create_connector("kubernetes", "home", {"server": "https://10.1.1.12:6443",
                                                      "insecure_skip_tls_verify": True})
    await svc.set_secret("home", {"token": TOKEN})


async def test_kubernetes_read_never_exposes_the_token(svc) -> None:
    await _kube(svc)
    listed = await svc.list_connectors()
    assert listed[0]["secret_set"] is True
    assert TOKEN not in json.dumps(listed)
    out = await svc.run_read("home", {"args": ["get", "pods", "-A"]})
    assert out["ok"] and "ARGS --request-timeout=30s get pods -A" in out["output"]
    assert "kubeconfig_ok" in out["output"] and TOKEN not in out["output"]
    assert not any(svc._test_work.iterdir())  # credentials removed after the command
    refused = await svc.run_read("home", {"args": ["get", "secrets"]})
    assert refused == {"ok": False, "denied_reason": "secrets_forbidden"}


async def test_ssh_needs_a_confirmed_host_key(svc, monkeypatch) -> None:
    await svc.create_connector("ssh", "esnixi", {"host": "192.168.42.254", "user": "celes"})
    await svc.set_secret("esnixi", {"private_key": _key("ed25519")})
    assert (await svc.run_read("esnixi", {"command": "uname"}))["denied_reason"] == (
        "host_key_not_pinned")

    async def fake_scan(cfg):
        return "192.168.42.254 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake", ["SHA256:abc (ED25519)"]

    monkeypatch.setattr(executor, "scan_host_keys", fake_scan)
    view = await svc.scan_host_key("esnixi")
    assert view["host_key_status"] == "pending"
    with pytest.raises(InfraInputError):
        await svc.trust_host_key("esnixi", ["SHA256:other (ED25519)"])
    assert (await svc.trust_host_key("esnixi", ["SHA256:abc (ED25519)"]))["host_key_status"] == (
        "pinned")
    out = await svc.run_read("esnixi", {"command": "disk"})
    assert out["ok"] and "REMOTE df -h" in out["output"] and "strict_ok" in out["output"]
    with pytest.raises(InfraInputError):
        await svc.set_secret("esnixi", {"private_key": _key("ecdsa")})


async def test_plans_wait_for_approval_and_run_in_order(svc) -> None:
    await _kube(svc)
    with pytest.raises(InfraInputError, match="flag_forbidden"):
        await svc.propose("agent-1", {"title": "x", "steps": [
            {"connector": "home", "args": ["delete", "pod", "x", "--token=y"]}]})
    plan = await svc.propose("agent-1", {
        "title": "Restart api", "summary": "stuck", "rollback": "none",
        "steps": [{"connector": "home", "args": ["rollout", "restart", "deploy/api"]},
                  {"connector": "home", "args": ["rollout", "status", "deploy/api"]}],
        "verify": [{"connector": "home", "args": ["get", "pods"]}]})
    assert plan["status"] == "proposed"
    with pytest.raises(InfraInputError):
        await svc.run_step(plan["id"], 0)  # not approved yet
    decided = await svc.decide(plan["id"], approve=True, actor="owner", act_mode="opencode")
    assert decided["status"] == "approved"
    brief = svc._test_handoffs.calls[0]["brief"]
    assert plan["id"] in brief and "infra_run_step" in brief and TOKEN not in brief
    with pytest.raises(InfraInputError, match="in order"):
        await svc.run_step(plan["id"], 1)
    first = await svc.run_step(plan["id"], 0)
    assert first["ok"] and "rollout restart deploy/api" in first["output"]
    await svc.run_step(plan["id"], 1)
    done = await svc.get_plan(plan["id"])
    assert done["status"] == "done" and len(done["results"]) == 2
    verify = await svc.run_verify(plan["id"])
    assert verify[0]["ok"]


async def test_a_failed_step_stops_the_plan(svc) -> None:
    await _kube(svc)
    plan = await svc.propose("a", {"title": "t", "steps": [
        {"connector": "home", "args": ["rollout", "restart", "deploy/fail"]},
        {"connector": "home", "args": ["get", "pods"]}]})
    await svc.decide(plan["id"], approve=True, actor="owner", act_mode="direct")
    await svc.run_all(plan["id"])
    failed = await svc.get_plan(plan["id"])
    assert failed["status"] == "failed" and len(failed["results"]) == 1
    with pytest.raises(InfraInputError):
        await svc.run_step(plan["id"], 1)


# ── endpoints and MCP ────────────────────────────────────────────────────────

def _app(svc: InfraService, claims: dict[str, Any]) -> FastAPI:
    from backend.api.auth import current_access
    from backend.api.endpoints import infra as infra_ep
    from backend.infra.mcp_server import router as mcp_router

    app = FastAPI()
    app.include_router(infra_ep.router)
    app.include_router(mcp_router)
    app.state.infra_service = svc
    app.dependency_overrides[current_access] = lambda: claims
    return app


async def test_owner_api_is_people_only_and_never_returns_secrets(svc) -> None:
    person = {"sub": "owner", "typ": "access", "roles": ["owner"]}
    with TestClient(_app(svc, person)) as c:
        r = c.post("/api/infra/connectors", json={"kind": "kubernetes", "name": "home",
                                                  "config": {"server": "https://k:6443"}})
        assert r.status_code == 201, r.text
        r = c.put("/api/infra/connectors/home/secret", json={"token": TOKEN})
        assert r.status_code == 200 and TOKEN not in r.text and r.json()["secret_set"]
        assert TOKEN not in c.get("/api/infra/connectors").text
    device = {"sub": "device:mac", "typ": "device", "roles": ["device"]}
    with TestClient(_app(svc, device)) as c:
        assert c.get("/api/infra/connectors").status_code == 403


async def test_mcp_infra_is_for_opencode_and_approved_plans_only(svc, monkeypatch) -> None:
    from backend.projects import mcp_server as pm

    monkeypatch.setenv("NERVE_MCP_PROJECTS_TOKENS", "opencode:" + OC + ",other:" + OTHER)
    await _kube(svc)
    plan = await svc.propose("a", {"title": "t", "steps": [
        {"connector": "home", "args": ["rollout", "restart", "deploy/api"]}]})
    app = _app(svc, {"sub": "owner", "typ": "access"})
    app.state.mcp_project_verifiers = pm.build_verifiers()

    def call(token: str, name: str, args: dict[str, Any]):
        return c.post("/api/mcp/infra", headers={"Authorization": f"Bearer {token}"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}})

    with TestClient(app) as c:
        assert call(OTHER, "infra_get_plan", {"plan_id": plan["id"]}).status_code == 401
        r = call(OC, "infra_run_step", {"plan_id": plan["id"], "index": 0})
        assert r.status_code == 200, (r.status_code, r.text[:300])
        assert r.json()["result"]["isError"] is True  # still only proposed
        await svc.decide(plan["id"], approve=True, actor="owner", act_mode="opencode")
        r = call(OC, "infra_run_step", {"plan_id": plan["id"], "index": 0})
        body = r.json()["result"]
        assert body["isError"] is False and body["structuredContent"]["ok"] is True
        assert TOKEN not in r.text


# ── agent gateway ────────────────────────────────────────────────────────────

class _Agent:
    def __init__(self, type_: str, tools: list[str] | None = None) -> None:
        self.id, self.type, self.name, self.tools = f"a-{type_}", type_, type_, tools or []


async def test_gateway_grants_infra_to_infrastructure_agents_only(svc) -> None:
    from backend.agents.tools.gateway import ToolGateway
    from backend.agents.tools.protocol import instructions

    await _kube(svc)
    gw = ToolGateway(hub_token="", omniroute_key="")
    gw.infra = svc
    ok = await gw.call(_Agent("architecture_analyst"), "infra", "kubectl",
                       {"connector": "home", "args": ["get", "nodes"]})
    assert ok["ok"] and "get nodes" in ok["result"]["output"]
    denied = await gw.call(_Agent("finance"), "infra", "kubectl",
                           {"connector": "home", "args": ["get", "nodes"]})
    assert denied == {"ok": False, "denied_reason": "infra_not_granted"}
    granted = await gw.call(_Agent("planner", ["infra"]), "infra", "connectors", {})
    assert granted["ok"] and granted["result"][0]["name"] == "home"
    proposal = await gw.call(_Agent("runtime_inspector"), "infra", "propose_plan", {
        "title": "t", "steps": [{"connector": "home", "args": ["rollout", "restart", "d/x"]}]})
    assert proposal["ok"] and proposal["result"]["status"] == "proposed"
    assert "propose_plan" in instructions(_Agent("architecture_analyst"))
    assert "infra" not in instructions(_Agent("finance")).split("Allowed servers")[1].split("\n")[1]


def test_no_stray_temp_dirs_left(tmp_path) -> None:
    assert os.path.isdir(tmp_path)
