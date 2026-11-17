"""Searchable OmniRoute catalog and the full-size combo lookup."""
from __future__ import annotations

import json

import httpx

import backend.api.app  # noqa: F401 - import-order guard
from backend.api.endpoints.agent_runner import _parse_combos
from backend.inference.catalog import Catalog

MODELS = [{"id": f"bedrock/model-{i:04d}", "owned_by": "bedrock"} for i in range(900)] + [
    {"id": "secure/coder", "owned_by": "combo"},
    {"id": "secure/planner", "owned_by": "combo"},
    {"id": "local/esnixi", "owned_by": "combo"},
    {"id": "vllm/qwen3.8-27b-nvfp4", "owned_by": "vllm"},
]


def _catalog(monkeypatch, calls: list[str]) -> Catalog:
    monkeypatch.setenv("NERVE_OMNIROUTE_URL", "http://omni.test/v1")
    monkeypatch.setattr("backend.inference.key_resolver.regular_key", lambda: "fake-key")

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        assert req.headers["authorization"] == "Bearer fake-key"
        return httpx.Response(200, json={"data": MODELS})

    return Catalog(transport=httpx.MockTransport(handler))


async def test_search_covers_the_whole_catalog_and_filters_combos(monkeypatch) -> None:
    calls: list[str] = []
    cat = _catalog(monkeypatch, calls)
    everything = await cat.search(limit=100)
    assert everything["total"] == len(MODELS) and len(everything["items"]) == 100
    assert (await cat.search("model-0899"))["items"][0]["id"] == "bedrock/model-0899"
    combos = await cat.search(kind="combo")
    assert [c["id"] for c in combos["items"]] == ["local/esnixi", "secure/coder", "secure/planner"]
    assert (await cat.search("secure plan", kind="combo"))["total"] == 1
    page2 = await cat.search("bedrock", limit=50, offset=850)
    assert page2["total"] == 900 and len(page2["items"]) == 50
    assert calls == ["http://omni.test/v1/models"]  # cached


def test_combo_list_longer_than_the_old_cap_still_parses() -> None:
    long = {"combos": [{"id": str(i), "name": f"combo/{i:03d}", "enabled": True,
                        "models": [{"provider": "p", "model": "x" * 80}]} for i in range(300)]}
    text = json.dumps(long, indent=2)
    assert len(text) > 20_000
    assert len(_parse_combos(text)) == 300
