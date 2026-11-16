"""Guards for the OpenAI Secure MCP Tunnel manifests (NS-ADR-105)."""

from __future__ import annotations

from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parents[2] / "deployments" / "kubernetes" / "base"


def _docs(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((BASE / name).read_text()) if d]


def test_tunnel_client_is_pinned_authenticated_and_secret_backed():
    dep = next(d for d in _docs("tunnel-client.yaml") if d["kind"] == "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert "@sha256:" in c["image"] and ":latest" not in c["image"]
    env = {e["name"]: e for e in c["env"]}
    assert env["CONTROL_PLANE_TUNNEL_ID"]["value"].startswith("tunnel_")
    assert env["MCP_SERVER_URL"]["value"].endswith(":8000/api/mcp/projects")
    for name in ("CONTROL_PLANE_API_KEY", "MCP_TOKEN"):
        ref = env[name]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "nervecenter-openai-tunnel" and not ref.get("optional")
    assert env["MCP_EXTRA_HEADERS"]["value"] == "Authorization: env:MCP_AUTH_HEADER_VALUE"
    # no literal secret values anywhere in the manifest
    assert all("value" not in env[n] for n in ("CONTROL_PLANE_API_KEY", "MCP_TOKEN"))


def test_tunnel_client_egress_is_dns_api_and_443_to_public_only():
    pol = next(d for d in _docs("tunnel-client.yaml") if d["kind"] == "NetworkPolicy")
    assert pol["spec"]["policyTypes"] == ["Egress"]
    ports = [p["port"] for rule in pol["spec"]["egress"] for p in rule["ports"]]
    assert sorted(ports) == [53, 53, 443, 8000]
    ipblock = pol["spec"]["egress"][2]["to"][0]["ipBlock"]
    assert "10.0.0.0/8" in ipblock["except"]


def test_no_public_ingress_for_the_tunnel_or_the_mcp_projects_path():
    kustomization = (BASE / "kustomization.yaml").read_text()
    assert "tunnel-client.yaml" in kustomization
    assert "mcp-projects-public" not in kustomization
    for name in ("ingress.yaml", "relay-ingress.yaml"):
        assert "/api/mcp/projects" not in (BASE / name).read_text().replace("#", "")
