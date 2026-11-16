"""Mac handoff agent script (NS-ADR-106): push denial, repo guard, summary parsing."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "apple" / "HandoffAgent" / "nerve_handoff_agent.py"
spec = importlib.util.spec_from_file_location("nerve_handoff_agent", SCRIPT)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def test_repo_guard(tmp_path):
    (tmp_path / "apple").mkdir()
    assert agent.repo_dir(tmp_path, "apple") == tmp_path / "apple"
    for bad in ("../etc", None, "missing"):
        with pytest.raises(ValueError):
            agent.repo_dir(tmp_path, bad)


def test_summary_from_ndjson():
    out = "\n".join(
        [
            json.dumps({"type": "text", "text": "step"}),
            "noise",
            json.dumps({"part": {"text": "Done: X"}}),
        ]
    )
    assert agent.summarize_json_events(out) == "Done: X"


def test_opencode_run_denies_push_unless_approved(tmp_path, monkeypatch):
    (tmp_path / "r").mkdir()
    seen = {}

    class P:
        returncode = 0
        stdout = json.dumps({"text": "ok"})

    def fake_run(cmd, cwd, env, **kw):
        seen["cfg"] = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
        seen["cmd"], seen["cwd"] = cmd, cwd
        return P()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    ok, summary = agent.run_opencode(
        {"title": "t", "brief": "b", "repo": "r", "push_approved": False}, tmp_path
    )
    assert ok and summary == "ok" and seen["cwd"] == tmp_path / "r"
    assert {"action": "shell", "resource": "git push *", "effect": "deny"} in seen["cfg"][
        "permissions"
    ]
    assert "Do not push" in seen["cmd"][-1]
    agent.run_opencode({"title": "t", "brief": "b", "repo": "r", "push_approved": True}, tmp_path)
    assert not any(p["resource"] == "git push *" for p in seen["cfg"]["permissions"])


def test_no_listener_in_agent():
    src = SCRIPT.read_text()
    assert "socket.bind" not in src and "HTTPServer" not in src and "listen(" not in src
