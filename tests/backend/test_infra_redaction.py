"""Shared output redaction: secrets go, JSON structure and key names stay."""
from __future__ import annotations

import json

from backend.infra.policy import MAX_OUTPUT_CHARS, redact


def test_plain_text_credentials_are_still_redacted() -> None:
    text = ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc123\n-----END OPENSSH PRIVATE KEY-----\n"
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlc2ln\n"
            "Authorization: Bearer abcdefghijklmnopqrstu\n"
            "key sk-abcdefghijklmnop1234 and ghp_abcdefghijklmnop1234 and AKIAABCDEFGHIJKLMNOP\n"
            "password=hunter2\ntoken: s3cr3tvalue\nSECRET = topsecret\napi_key: abc\nok line\n")
    out = redact(text)
    for leaked in ("abc123", "eyJhbGci", "abcdefghijklmnopqrstu", "sk-abcdefghijklmnop1234",
                   "ghp_abcdefghijklmnop1234", "AKIAABCDEFGHIJKLMNOP", "hunter2", "s3cr3tvalue",
                   "topsecret", "api_key: abc"):
        assert leaked not in out
    assert "ok line" in out and "password=[redacted]" in out and "token: [redacted]" in out


def test_kubectl_json_stays_valid_and_keeps_key_names() -> None:
    doc = {"spec": {
        "automountServiceAccountToken": False,
        "serviceAccountToken": {"expirationSeconds": 3600, "path": "token"},
        "volumes": [{"name": "w", "secret": {"secretName": "webhook-tls", "defaultMode": 420}}],
        "containers": [{"name": "c", "image": "nginx:1.27", "env": [{"name": "A", "value": "1"}],
                        "apiKey": "AbCd1234", "password": "hunter2", "token": "tok-123456",
                        "accessKey": "k", "retries": 3}]}}
    out = redact(json.dumps(doc, indent=2))
    parsed = json.loads(out)  # still valid JSON
    c = parsed["spec"]["containers"][0]
    assert parsed["spec"]["automountServiceAccountToken"] is False
    assert parsed["spec"]["volumes"][0]["secret"]["defaultMode"] == 420
    assert c["image"] == "nginx:1.27" and c["retries"] == 3
    for field in ("apiKey", "password", "token", "accessKey"):
        assert c[field] == "[redacted]"  # key name kept, value hidden
    assert parsed["spec"]["volumes"][0]["secret"]["secretName"] == "[redacted]"
    assert "hunter2" not in out and "AbCd1234" not in out and "tok-123456" not in out


def test_base64_data_values_in_json_and_yaml_are_redacted() -> None:
    blob = "dGhpcyBpcyBhIHNlY3JldCB2YWx1ZSB0aGF0IGlzIGxvbmc3NjU0MzIx"
    out = redact(json.dumps({"data": {"a": "cGFzc3dvcmQ=", "n": 5}, "caBundle": blob}, indent=2))
    parsed = json.loads(out)
    assert parsed["data"]["a"] == "[redacted]" and parsed["caBundle"] == "[redacted]"
    assert "cGFzc3dvcmQ" not in out and blob not in out
    yaml = f"data:\n  tls.key: LS0tLS1CRUdJTg==\n  other: {blob}\nnext: kept\n"
    out = redact(yaml)
    assert "LS0tLS1CRUdJTg" not in out and blob not in out and "next: kept" in out


def test_long_paths_and_names_are_not_mistaken_for_base64() -> None:
    line = '"mountPath": "/var/lib/kubelet/pods/volumes/kubernetes.io~projected/kube-api-access",'
    assert redact(line) == line


def test_output_cap_applies_by_default_and_can_be_raised() -> None:
    text = "x" * (MAX_OUTPUT_CHARS + 10)
    assert redact(text).endswith("[truncated]")
    assert redact(text, limit=MAX_OUTPUT_CHARS + 20) == text


def test_json_string_elements_with_credential_flags_stay_valid() -> None:
    doc = {"args": ["--secret-name=abc123", "--token=xyz789", "--namespace=argocd"], "ok": 1}
    out = redact(json.dumps(doc, indent=2))
    parsed = json.loads(out)
    assert parsed["args"][0] == "--secret-name=[redacted]"
    assert parsed["args"][1] == "--token=[redacted]" and parsed["args"][2] == "--namespace=argocd"
    assert "abc123" not in out and "xyz789" not in out
