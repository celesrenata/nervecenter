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
