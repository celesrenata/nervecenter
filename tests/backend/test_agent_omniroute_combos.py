"""OmniRoute combos for per-agent inference (names and models only)."""
import json

import backend.api.app  # noqa: F401 - import-order guard
from backend.api.endpoints.agent_runner import _parse_combos


def test_parse_combos_from_the_mcp_text_result() -> None:
    raw = json.dumps({"combos": [
        {"id": "1", "name": "local/esnixi", "enabled": True,
         "models": [{"provider": "vllm", "model": "vllm/qwen", "priority": 1}]},
        {"id": "2", "name": "secure/coder", "enabled": True, "models": []},
        {"id": "3", "name": "old/combo", "enabled": False},
        {"id": "4"},
    ]})
    combos = _parse_combos(raw)
    assert [c["name"] for c in combos] == ["secure/coder", "local/esnixi", "old/combo"]
    assert combos[1]["models"] == ["vllm/qwen"] and combos[2]["enabled"] is False
    assert "id" not in combos[0]
    assert _parse_combos("not json") == [] and _parse_combos(None) == []


def test_parse_combos_salvages_a_list_omniroute_truncated() -> None:
    full = json.dumps({"combos": [
        {"name": "local/esnixi", "models": [{"model": "vllm/qwen"}]},
        {"name": "secure/coder", "models": [{"model": "a"}, {"model": "b"}]},
    ]}, indent=2)
    cut = full[: full.rindex('"b"')] + "\n\n... [truncated 469 chars by OmniRoute MCP filter.]"
    assert [c["name"] for c in _parse_combos(cut)] == ["local/esnixi"]
