"""NerveShield Task 4.2: fail-closed local-only OmniRoute route resolver.

Fixtures only. The registry transport is an in-memory fake and the "content
sender" is a recording stub; no network, cluster, host, model or real mail is
touched. The cloud-fallback attack tests prove the content sender is never
invoked when any hop, at any depth, is not provably local.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from backend.mail_security import models
from backend.mail_security.route_guard import (
    PATH_COMBOS,
    PATH_MODELS,
    PATH_PROVIDERS,
    HostClass,
    LocalOnlyRouteResolver,
    ResolverLimits,
    RouteDeniedError,
    RouteIdentity,
    RouteReason,
    classify_host,
    dispatch_if_local,
)

BASE = "http://omniroute.omniroute.svc:20128"
SECRET = "sk-SENTINEL-do-not-leak-0123456789"  # noqa: S105 - fixture sentinel
MAIL = "MAIL-BODY-SENTINEL: wire $5000 to account 12345"


class FakeTransport:
    """In-memory registry. ``fail`` maps path -> exception; ``delay`` stalls."""

    def __init__(
        self,
        models_doc: Any,
        combos_doc: Any,
        providers_doc: Any,
        *,
        fail: dict[str, Exception] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.docs = {
            PATH_MODELS: models_doc,
            PATH_COMBOS: combos_doc,
            PATH_PROVIDERS: providers_doc,
        }
        self.fail = fail or {}
        self.delay = delay
        self.calls: list[str] = []

    async def get_json(self, path: str) -> Any:
        self.calls.append(path)
        if self.delay:
            await asyncio.sleep(self.delay)
        if path in self.fail:
            raise self.fail[path]
        return self.docs[path]


def _provider(pid: str, url: str | None, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"provider": pid, "apiKey": SECRET, "accessToken": SECRET, **extra}
    if url is not None:
        item["baseUrl"] = url
    return item


def _combo(
    name: str, entries: list[Any], strategy: str = "fill-first", *, secure: Any = True
) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "strategy": strategy, "models": entries}
    if secure is not None:
        item["config"] = {"secure": secure}
    return item


def _ref(name: str) -> dict[str, str]:
    return {"kind": "combo-ref", "comboName": name}


def _models(*ids: str) -> dict[str, Any]:
    return {"data": [{"id": i, "owned_by": "combo"} for i in ids]}


LOCAL_PROVIDERS = [
    _provider("llamacpp", "http://llama.models.svc.cluster.local:8080"),
    _provider("arcforge", "http://10.1.1.50:8100"),
    _provider("lmstudio", "http://127.0.0.1:1234/v1"),
]
CLOUD_PROVIDERS = [
    _provider("bedrock", "https://bedrock-runtime.us-east-1.amazonaws.com"),
    _provider("cfp", "https://api.cloudflare.com/client/v4"),
    _provider("aihorde", "https://aihorde.net/api"),
]


def _resolver(
    transport: FakeTransport,
    *,
    limits: ResolverLimits | None = None,
    allowed: frozenset[str] | None = None,
    base: str = BASE,
    require_secure: bool = True,
) -> LocalOnlyRouteResolver:
    return LocalOnlyRouteResolver(
        transport,
        omniroute_base_url=base,
        limits=limits,
        allowed_provider_ids=allowed,
        require_secure_combo=require_secure,
    )


def _registry(
    combos: list[dict[str, Any]],
    providers: list[dict[str, Any]],
    extra_models: tuple[str, ...] = (),
) -> FakeTransport:
    names = tuple(c["name"] for c in combos)
    return FakeTransport(
        _models(*names, *extra_models), {"combos": combos}, {"connections": providers}
    )


def _nested_hybrid_code(tier5_leaf: str) -> FakeTransport:
    """Mirror of the observed shape: fill-first with nested multi-tier pools."""
    combos = [
        _combo("hybrid/code", [_ref(f"pool/tier{n}/code") for n in ("1", "1b", "2", "5")]),
        _combo("pool/tier1/code", ["llamacpp/qwen-27b"]),
        _combo("pool/tier1b/code", ["arcforge/granite-4.2"]),
        _combo("pool/tier2/code", [_ref("pool/tier2/inner")]),
        _combo("pool/tier2/inner", ["lmstudio/glm"]),
        _combo("pool/tier5/code", [tier5_leaf]),
    ]
    return _registry(combos, LOCAL_PROVIDERS + CLOUD_PROVIDERS)


class Sender:
    """Stands in for the code that would transmit mail-derived content."""

    def __init__(self) -> None:
        self.received: list[str] = []

    async def __call__(self, identity: RouteIdentity) -> str:
        self.received.append(MAIL)
        return "ok"


# -- host classification ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8080", HostClass.LOOPBACK),
        ("http://localhost:11434/v1", HostClass.LOOPBACK),
        ("http://localhost.:11434", HostClass.LOOPBACK),
        ("http://[::1]:8080", HostClass.LOOPBACK),
        ("http://10.1.1.12:8100", HostClass.PRIVATE),
        ("https://192.168.0.5", HostClass.PRIVATE),
        ("http://172.16.4.4", HostClass.PRIVATE),
        ("http://omniroute.omniroute.svc:20128", HostClass.IN_CLUSTER),
        ("http://x.ns.svc.cluster.local", HostClass.IN_CLUSTER),
        ("https://api.openai.com/v1", HostClass.REMOTE),
        ("https://bedrock-runtime.us-east-1.amazonaws.com", HostClass.REMOTE),
        ("http://8.8.8.8", HostClass.REMOTE),
        ("http://[::ffff:8.8.8.8]", HostClass.REMOTE),
        ("http://evil.svc.attacker.com", HostClass.REMOTE),
        ("http://localhost.attacker.com", HostClass.REMOTE),
        ("http://169.254.169.254/latest", HostClass.UNKNOWN),
        ("http://0.0.0.0:80", HostClass.UNKNOWN),
        ("http://100.64.1.1", HostClass.UNKNOWN),
        ("http://ollama:11434", HostClass.UNKNOWN),
        ("http://2130706433", HostClass.UNKNOWN),
        ("http://0x7f.1", HostClass.UNKNOWN),
        ("http://user:pw@127.0.0.1", HostClass.UNKNOWN),
        ("http://127.0.0.1:notaport", HostClass.UNKNOWN),
        ("ftp://127.0.0.1", HostClass.UNKNOWN),
        ("file:///etc/passwd", HostClass.UNKNOWN),
        ("127.0.0.1:8080", HostClass.UNKNOWN),
        ("", HostClass.UNKNOWN),
        (None, HostClass.UNKNOWN),
        (42, HostClass.UNKNOWN),
        ("http://a..svc", HostClass.UNKNOWN),
        ("http://[fe80::1%25eth0]", HostClass.UNKNOWN),
    ],
)
def test_classify_host(url: object, expected: HostClass) -> None:
    assert classify_host(url) is expected


# -- allow path -------------------------------------------------------------------------------


async def test_all_local_nested_route_allowed_with_log_safe_identity() -> None:
    transport = _nested_hybrid_code("lmstudio/fallback")
    decision = await _resolver(transport).resolve("hybrid/code")
    assert decision.allowed and decision.reason is RouteReason.ALLOWED_LOCAL_ONLY
    ident = decision.require_allowed()
    assert ident.alias == "hybrid/code"
    assert {(leaf.provider, leaf.model) for leaf in ident.leaves} == {
        ("llamacpp", "qwen-27b"),
        ("arcforge", "granite-4.2"),
        ("lmstudio", "glm"),
        ("lmstudio", "fallback"),
    }
    assert all(leaf.host_class is not HostClass.REMOTE for leaf in ident.leaves)
    assert ident.max_depth == 3 and ident.combo_count == 6
    assert re.fullmatch(r"[0-9a-f]{32}", ident.digest)
    assert decision.model_run_status is models.ModelRunStatus.NOT_RUN
    blob = repr(decision) + repr(decision.log_fields())
    assert SECRET not in blob and "http" not in blob and "10.1.1.50" not in blob


async def test_direct_local_model_id_allowed() -> None:
    transport = _registry([], LOCAL_PROVIDERS, extra_models=("llamacpp/qwen-27b",))
    decision = await _resolver(transport, require_secure=False).resolve("llamacpp/qwen-27b")
    assert decision.allowed
    assert decision.require_allowed().max_depth == 1


async def test_direct_model_id_denied_when_secure_combo_required() -> None:
    """A bare model id has no OmniRoute isolation flag, so it cannot prove isolation."""
    transport = _registry([], LOCAL_PROVIDERS, extra_models=("llamacpp/qwen-27b",))
    decision = await _resolver(transport).resolve("llamacpp/qwen-27b")
    assert not decision.allowed and decision.reason is RouteReason.COMBO_NOT_SECURE


@pytest.mark.parametrize("flag", [None, False, "true", 1, {"on": True}])
async def test_local_combo_without_strict_secure_flag_denied(flag: Any) -> None:
    transport = _registry([_combo("local/reader", ["llamacpp/a"], secure=flag)], LOCAL_PROVIDERS)
    decision = await _resolver(transport).resolve("local/reader")
    assert not decision.allowed and decision.reason is RouteReason.COMBO_NOT_SECURE


async def test_secure_flag_on_top_combo_covers_unflagged_nested_pools() -> None:
    transport = _registry(
        [
            _combo("pool/tier1/reader", ["llamacpp/a"], secure=None),
            _combo("secure/reader", [_ref("pool/tier1/reader")]),
        ],
        LOCAL_PROVIDERS,
    )
    assert (await _resolver(transport).resolve("secure/reader")).allowed


async def test_secure_flag_never_overrides_locality() -> None:
    transport = _registry(
        [_combo("secure/reader", ["llamacpp/a", "openai/gpt"])],
        [*LOCAL_PROVIDERS, _provider("openai", "https://api.openai.com/v1")],
    )
    decision = await _resolver(transport).resolve("secure/reader")
    assert not decision.allowed and decision.reason is RouteReason.LEAF_NON_LOCAL


async def test_digest_changes_when_secure_flag_changes() -> None:
    def reg(flag: bool) -> FakeTransport:
        return _registry([_combo("secure/reader", ["llamacpp/a"], secure=flag)], LOCAL_PROVIDERS)

    on = await _resolver(reg(True), require_secure=False).resolve("secure/reader")
    off = await _resolver(reg(False), require_secure=False).resolve("secure/reader")
    assert on.require_allowed().digest != off.require_allowed().digest


async def test_digest_stable_and_changes_when_route_changes() -> None:
    a = await _resolver(_nested_hybrid_code("lmstudio/fallback")).resolve("hybrid/code")
    b = await _resolver(_nested_hybrid_code("lmstudio/fallback")).resolve("hybrid/code")
    c = await _resolver(_nested_hybrid_code("lmstudio/other")).resolve("hybrid/code")
    assert a.require_allowed().digest == b.require_allowed().digest
    assert a.require_allowed().digest != c.require_allowed().digest


async def test_shared_subpool_diamond_does_not_blow_up() -> None:
    combos = [_combo("leaf", ["llamacpp/m"])]
    for level in range(6):
        below = "leaf" if level == 0 else f"mid{level - 1}"
        combos.append(_combo(f"mid{level}", [_ref(below)] * 5))
    combos.append(_combo("root", [_ref("mid5")]))
    decision = await _resolver(_registry(combos, LOCAL_PROVIDERS)).resolve("root")
    assert decision.allowed


# -- cloud-fallback attack: no content leaves when any hop is remote ----------------


@pytest.mark.parametrize(
    ("leaf", "reason"),
    [
        ("bedrock/claude", RouteReason.LEAF_NON_LOCAL),
        ("cfp/llama", RouteReason.LEAF_NON_LOCAL),
        ("aihorde/anything", RouteReason.LEAF_NON_LOCAL),
        ("zc/unknown-provider", RouteReason.PROVIDER_MISSING),
    ],
)
async def test_cloud_fallback_hop_denies_and_no_content_is_sent(
    leaf: str, reason: RouteReason
) -> None:
    sender = Sender()
    transport = _nested_hybrid_code(leaf)
    result = await dispatch_if_local(_resolver(transport), "hybrid/code", sender)
    assert not result.sent and result.value is None
    assert not result.decision.allowed and result.decision.reason is reason
    assert sender.received == []
    assert result.decision.model_run_status is models.ModelRunStatus.DENIED_BY_POLICY
    assert MAIL not in repr(result) and SECRET not in repr(result)
    with pytest.raises(RouteDeniedError):
        result.decision.require_allowed()


async def test_remote_hop_first_middle_or_last_all_deny() -> None:
    for position in range(3):
        entries: list[Any] = ["llamacpp/a", "lmstudio/b", "arcforge/c"]
        entries[position] = "bedrock/claude"
        transport = _registry([_combo("r", entries)], LOCAL_PROVIDERS + CLOUD_PROVIDERS)
        sender = Sender()
        result = await dispatch_if_local(_resolver(transport), "r", sender)
        assert not result.sent and sender.received == []


async def test_local_looking_alias_name_proves_nothing() -> None:
    transport = _registry(
        [_combo("local/only-trust-me", ["bedrock/claude"])], LOCAL_PROVIDERS + CLOUD_PROVIDERS
    )
    decision = await _resolver(transport).resolve("local/only-trust-me")
    assert not decision.allowed and decision.reason is RouteReason.LEAF_NON_LOCAL


async def test_provider_with_one_remote_connection_denies() -> None:
    providers = [
        _provider("mixed", "http://10.0.0.5:8000"),
        _provider("mixed", "https://api.example.com"),
    ]
    decision = await _resolver(_registry([_combo("c", ["mixed/m"])], providers)).resolve("c")
    assert decision.reason is RouteReason.LEAF_NON_LOCAL


async def test_local_provider_by_alias_prefix_allowed_and_remote_by_alias_denied() -> None:
    providers = [
        _provider("openai-compatible-1", "http://10.0.0.5:8000", alias="zc"),
        _provider("cloud-1", "https://api.example.com", alias="cfp"),
    ]
    ok = await _resolver(_registry([_combo("a", ["zc/m"])], providers)).resolve("a")
    bad = await _resolver(_registry([_combo("b", ["cfp/m"])], providers)).resolve("b")
    assert ok.allowed and not bad.allowed


# -- unknown / invalid aliases ----------------------------------------------------------------


@pytest.mark.parametrize("alias", ["hybrid/reader", "hybrid/coder", "nope", "hybrid/CODE"])
async def test_unknown_alias_denied(alias: str) -> None:
    transport = _nested_hybrid_code("lmstudio/fallback")
    decision = await _resolver(transport).resolve(alias)
    assert not decision.allowed and decision.reason is RouteReason.ALIAS_UNKNOWN


@pytest.mark.parametrize(
    "alias", ["", " ", "a b", "x\n", "../etc", "a" * 500, "hybrid/code;rm", None, 7]
)
async def test_invalid_alias_denied_without_contacting_registry(alias: Any) -> None:
    transport = _nested_hybrid_code("lmstudio/fallback")
    decision = await _resolver(transport).resolve(alias)
    assert decision.reason is RouteReason.ALIAS_INVALID and transport.calls == []


async def test_alias_only_in_models_but_not_a_valid_leaf_denied() -> None:
    transport = _registry([], LOCAL_PROVIDERS, extra_models=("justaname",))
    decision = await _resolver(transport, require_secure=False).resolve("justaname")
    assert not decision.allowed and decision.reason is RouteReason.ENTRY_MALFORMED


# -- structural attacks -----------------------------------------------------------------------


async def test_self_cycle_denied() -> None:
    transport = _registry([_combo("loop", ["llamacpp/a", _ref("loop")])], LOCAL_PROVIDERS)
    assert (await _resolver(transport).resolve("loop")).reason is RouteReason.COMBO_CYCLE


async def test_mutual_cycle_denied_even_with_local_leaves() -> None:
    combos = [
        _combo("a", ["llamacpp/x", _ref("b")]),
        _combo("b", ["lmstudio/y", _ref("c")]),
        _combo("c", [_ref("a")]),
    ]
    decision = await _resolver(_registry(combos, LOCAL_PROVIDERS)).resolve("a")
    assert decision.reason is RouteReason.COMBO_CYCLE


async def test_depth_bomb_denied() -> None:
    combos = [_combo(f"d{i}", [_ref(f"d{i + 1}")]) for i in range(60)]
    combos.append(_combo("d60", ["llamacpp/x"]))
    decision = await _resolver(_registry(combos, LOCAL_PROVIDERS)).resolve("d0")
    assert decision.reason is RouteReason.COMBO_TOO_DEEP


async def test_depth_exactly_at_limit_allowed_and_one_over_denied() -> None:
    combos = [_combo(f"d{i}", [_ref(f"d{i + 1}")]) for i in range(3)]
    combos.append(_combo("d3", ["llamacpp/x"]))  # depth 4
    transport = _registry(combos, LOCAL_PROVIDERS)
    assert (await _resolver(transport, limits=ResolverLimits(max_depth=4)).resolve("d0")).allowed
    over = await _resolver(transport, limits=ResolverLimits(max_depth=3)).resolve("d0")
    assert over.reason is RouteReason.COMBO_TOO_DEEP


async def test_memoized_subtree_still_depth_checked() -> None:
    combos = [
        _combo("deep0", [_ref("deep1")]),
        _combo("deep1", [_ref("deep2")]),
        _combo("deep2", ["llamacpp/x"]),
        _combo("root", ["llamacpp/y", _ref("deep0"), {"kind": "combo-ref", "comboName": "deep0"}]),
    ]
    decision = await _resolver(
        _registry(combos, LOCAL_PROVIDERS), limits=ResolverLimits(max_depth=3)
    ).resolve("root")
    assert decision.reason is RouteReason.COMBO_TOO_DEEP


async def test_wide_entry_bomb_denied() -> None:
    combos = [_combo("wide", [f"llamacpp/m{i}" for i in range(50)])]
    decision = await _resolver(
        _registry(combos, LOCAL_PROVIDERS), limits=ResolverLimits(max_entries=10)
    ).resolve("wide")
    assert decision.reason is RouteReason.COMBO_TOO_LARGE


async def test_empty_combo_and_empty_subcombo_denied() -> None:
    combos = [_combo("empty", []), _combo("root", ["llamacpp/a", _ref("empty")])]
    transport = _registry(combos, LOCAL_PROVIDERS)
    assert (await _resolver(transport).resolve("empty")).reason is RouteReason.COMBO_EMPTY
    assert (await _resolver(transport).resolve("root")).reason is RouteReason.COMBO_EMPTY


@pytest.mark.parametrize(
    "entry",
    [
        42,
        None,
        ["llamacpp/a"],
        {"kind": "script", "model": "llamacpp/a"},
        {"kind": "model"},
        {"kind": "model", "model": 5},
        {"kind": "combo-ref"},
        {"kind": "combo-ref", "comboName": "does-not-exist"},
        "no-slash",
        "/leading",
        "trailing/",
        {"kind": "model", "model": "m", "providerId": ""},
    ],
)
async def test_malformed_entries_denied(entry: Any) -> None:
    transport = _registry([_combo("c", ["llamacpp/ok", entry])], LOCAL_PROVIDERS)
    decision = await _resolver(transport).resolve("c")
    assert not decision.allowed and decision.reason is RouteReason.ENTRY_MALFORMED


async def test_structured_leaf_with_provider_id_resolved() -> None:
    entry = {"kind": "model", "providerId": "llamacpp", "model": "llamacpp/qwen"}
    decision = await _resolver(_registry([_combo("c", [entry])], LOCAL_PROVIDERS)).resolve("c")
    assert decision.require_allowed().leaves[0].model == "qwen"


async def test_both_models_and_steps_or_neither_denied() -> None:
    both = {"name": "c", "models": ["llamacpp/a"], "steps": ["llamacpp/a"]}
    neither = {"name": "c"}
    for combo in (both, neither):
        transport = FakeTransport(
            _models("c"), {"combos": [combo]}, {"connections": LOCAL_PROVIDERS}
        )
        assert (await _resolver(transport).resolve("c")).reason is RouteReason.ENTRY_MALFORMED


# -- providers --------------------------------------------------------------------------------


async def test_missing_provider_denied() -> None:
    transport = _registry([_combo("c", ["ghost/m"])], LOCAL_PROVIDERS)
    decision = await _resolver(transport).resolve("c")
    assert decision.reason is RouteReason.PROVIDER_MISSING and decision.detail == "ghost"


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "not a url",
        "http://user:pw@127.0.0.1",
        "http://169.254.169.254",
        "http://ollama:1",
    ],
)
async def test_provider_without_provable_local_url_denied(url: str | None) -> None:
    providers = [_provider("p", url)]
    decision = await _resolver(_registry([_combo("c", ["p/m"])], providers)).resolve("c")
    assert decision.reason is RouteReason.LEAF_UNKNOWN_LOCALITY


async def test_operator_allowlist_is_additional_gate() -> None:
    transport = _nested_hybrid_code("lmstudio/fallback")
    allowed = frozenset({"llamacpp", "arcforge", "lmstudio"})
    assert (await _resolver(transport, allowed=allowed).resolve("hybrid/code")).allowed
    narrow = await _resolver(transport, allowed=frozenset({"llamacpp"})).resolve("hybrid/code")
    assert narrow.reason is RouteReason.PROVIDER_NOT_ALLOWLISTED


async def test_omniroute_endpoint_itself_must_be_local() -> None:
    transport = _nested_hybrid_code("lmstudio/fallback")
    for base in ("https://omniroute.example.com", "http://ollama:20128", "http://8.8.8.8", ""):
        decision = await _resolver(transport, base=base).resolve("hybrid/code")
        assert decision.reason is RouteReason.ENDPOINT_NOT_LOCAL
    assert transport.calls == []


# -- unreachable / malformed registry ---------------------------------------------------------


@pytest.mark.parametrize("path", [PATH_MODELS, PATH_COMBOS, PATH_PROVIDERS])
async def test_unreachable_registry_denies_and_hides_secret(path: str) -> None:
    exc = ConnectionError(f"Authorization: Bearer {SECRET} refused")
    transport = _nested_hybrid_code("lmstudio/fallback")
    transport.fail = {path: exc}
    sender = Sender()
    result = await dispatch_if_local(_resolver(transport), "hybrid/code", sender)
    assert result.decision.reason is RouteReason.REGISTRY_UNREACHABLE
    assert not result.sent and sender.received == []
    assert SECRET not in repr(result) + repr(result.decision.log_fields())


async def test_registry_timeout_denies() -> None:
    transport = _nested_hybrid_code("lmstudio/fallback")
    transport.delay = 0.5
    resolver = _resolver(transport, limits=ResolverLimits(timeout_seconds=0.05))
    assert (await resolver.resolve("hybrid/code")).reason is RouteReason.REGISTRY_TIMEOUT


@pytest.mark.parametrize(
    ("models_doc", "combos_doc", "providers_doc"),
    [
        (None, {"combos": []}, {"connections": []}),
        ({"data": "x"}, {"combos": []}, {"connections": []}),
        ({"data": [{"no_id": 1}]}, {"combos": []}, {"connections": []}),
        (_models("c"), "html", {"connections": []}),
        (_models("c"), {"combos": [{"models": []}]}, {"connections": []}),
        (_models("c"), {"combos": [{"name": "c", "models": ["x/y"]}] * 2}, {"connections": []}),
        (_models("c"), {"combos": []}, 5),
        (_models("c"), {"combos": []}, {"connections": [{"baseUrl": "http://127.0.0.1"}]}),
        (_models("c"), {"combos": []}, {"connections": ["str"]}),
        (_models("c"), {"combos": [{"name": "c", "models": ["x/y"]}]}, {"nothing": []}),
    ],
)
async def test_malformed_registry_denies(
    models_doc: Any, combos_doc: Any, providers_doc: Any
) -> None:
    transport = FakeTransport(models_doc, combos_doc, providers_doc)
    decision = await _resolver(transport).resolve("c")
    assert not decision.allowed
    assert decision.reason in {RouteReason.REGISTRY_MALFORMED, RouteReason.ENTRY_MALFORMED}


async def test_oversized_registry_lists_denied() -> None:
    huge = {"combos": [{"name": f"c{i}", "models": []} for i in range(2001)]}
    transport = FakeTransport(_models("c0"), huge, {"connections": LOCAL_PROVIDERS})
    assert (await _resolver(transport).resolve("c0")).reason is RouteReason.REGISTRY_MALFORMED


async def test_registry_change_between_calls_is_not_cached() -> None:
    transport = _nested_hybrid_code("lmstudio/fallback")
    resolver = _resolver(transport)
    first = await resolver.resolve("hybrid/code")
    assert first.allowed
    transport.docs = _nested_hybrid_code("bedrock/claude").docs  # attacker edits the registry
    second = await resolver.resolve("hybrid/code")
    assert not second.allowed and second.reason is RouteReason.LEAF_NON_LOCAL


# -- guarded dispatch / secrecy ---------------------------------------------------------------


async def test_dispatch_sends_only_when_allowed() -> None:
    sender = Sender()
    result = await dispatch_if_local(
        _resolver(_nested_hybrid_code("lmstudio/fallback")), "hybrid/code", sender
    )
    assert result.sent and result.value == "ok" and len(sender.received) == 1


async def test_never_leaks_keys_urls_or_content_in_any_output() -> None:
    transport = _nested_hybrid_code("bedrock/claude")
    decisions = [
        await _resolver(transport).resolve(a) for a in ("hybrid/code", "missing", "pool/tier1/code")
    ]
    for d in decisions:
        blob = repr(d) + repr(d.log_fields()) + str(d.identity)
        for forbidden in (SECRET, MAIL, "amazonaws", "10.1.1.50", "svc.cluster.local"):
            assert forbidden not in blob


async def test_hostile_registry_labels_are_redacted() -> None:
    providers = [_provider("evil\nprovider", "https://api.example.com")]
    transport = _registry([_combo("c", ["evil\nprovider/m"])], providers)
    decision = await _resolver(transport).resolve("c")
    assert decision.detail == "REDACTED" and "\n" not in repr(decision)


def test_limits_validated() -> None:
    for bad in (
        {"max_depth": 0},
        {"max_depth": 33},
        {"max_entries": 0},
        {"timeout_seconds": 0},
    ):
        with pytest.raises(ValueError):
            ResolverLimits(**bad)


# -- static properties ------------------------------------------------------------------------


def test_module_has_no_network_or_subprocess_imports_and_is_not_wired() -> None:
    root = Path(__file__).resolve().parents[2] / "backend"
    source = (root / "mail_security" / "route_guard.py").read_text()
    for pattern in (
        r"^\s*(import|from)\s+(socket|ssl|http|requests|httpx|aiohttp|subprocess|urllib\.request)\b",
    ):
        assert not re.search(pattern, source, re.MULTILINE)
    # NS-ADR-073: the SHADOW observer is wired by exactly these modules; the
    # route guard stays mandatory on every observer call.
    approved = {
        root / "api" / "wiring.py",
        root / "mail_security" / "observer_wiring.py",
        root / "owner_tools" / "observe_existing.py",
    }
    for path in root.rglob("*.py"):
        if path.name in (
            "route_guard.py",
            "granite_observer.py",
            "qwen_challenger.py",
            "observer_route.py",
        ) or path in approved:
            continue
        assert "route_guard" not in path.read_text(), f"{path} wires route_guard"
