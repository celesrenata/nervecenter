"""NerveShield: observer alias comes from NerveCenter's ``Reader`` combo (NS-ADR-033).

Fixtures only. The combo source and OmniRoute registry are in-memory fakes.
The registry fixture mirrors the live shapes recorded in
``evidence/phase4-live-registry-check.md``: ``hybrid/reader`` has a cloud hop,
``local/reader`` is a single private vllm leaf.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.mail_security.observer_route import (
    ObserverRouteSelection,
    SelectionReason,
    resolve_observer_route,
    select_alias,
)
from backend.mail_security.route_guard import (
    PATH_COMBOS,
    PATH_MODELS,
    PATH_PROVIDERS,
    LocalOnlyRouteResolver,
    RouteReason,
)

BASE = "http://omniroute.omniroute.svc:20128"
SECRET = "sk-SENTINEL-observer-route-0123456789"  # noqa: S105 - fixture sentinel


def _leaf(provider: str, model: str) -> dict[str, Any]:
    return {"kind": "model", "providerId": provider, "model": f"{provider}/{model}"}


def _ref(name: str) -> dict[str, str]:
    return {"kind": "combo-ref", "comboName": name}


REGISTRY: dict[str, Any] = {
    PATH_MODELS: {
        "data": [
            {"id": n} for n in ("secure/reader", "local/reader", "hybrid/reader", "local/granite")
        ]
    },
    PATH_COMBOS: {
        "combos": [
            {"name": "local/granite", "models": [_leaf("vllm", "granite-4.2-8b")]},
            {"name": "local/reader", "models": [_ref("local/granite")]},
            {
                "name": "secure/reader",
                "models": [_ref("local/granite")],
                "config": {"secure": True},
            },
            {
                "name": "hybrid/reader",
                "models": [_ref("local/granite"), _leaf("mistral", "mistral-small")],
            },
        ]
    },
    PATH_PROVIDERS: {
        "connections": [
            {
                "provider": "vllm",
                "apiKey": SECRET,
                "providerSpecificData": {"baseUrl": "http://10.1.1.12:2701/v1"},
            },
            {"provider": "mistral", "apiKey": SECRET, "providerSpecificData": {}},
        ]
    },
}


class Registry:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_json(self, path: str) -> Any:
        self.calls.append(path)
        return REGISTRY[path]


class Combos:
    def __init__(self, rows: Any = None, error: Exception | None = None) -> None:
        self.rows = rows
        self.error = error
        self.calls = 0

    async def list(self) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rows


def _row(name: str = "Reader", model: Any = "secure/reader", provider: Any = "Omniroute"):
    return {
        "id": "c1",
        "name": name,
        "provider_id": "p1",
        "provider_name": provider,
        "model_id": model,
        "note": None,
    }


def _resolver(registry: Registry | None = None) -> LocalOnlyRouteResolver:
    return LocalOnlyRouteResolver(registry or Registry(), omniroute_base_url=BASE)


async def test_reader_pointing_at_secure_combo_is_allowed() -> None:
    sel = await resolve_observer_route(Combos([_row(), _row("Coder", "hybrid/code")]), _resolver())
    assert sel.allowed and sel.alias == "secure/reader"
    assert sel.reason is SelectionReason.SELECTED
    assert sel.route is not None and sel.route.reason is RouteReason.ALLOWED_LOCAL_ONLY
    assert [(x.provider, x.model) for x in sel.route.identity.leaves] == [
        ("vllm", "granite-4.2-8b")
    ]


async def test_reader_pointing_at_hybrid_reader_is_denied_by_the_guard() -> None:
    """Today's live mapping: Reader -> hybrid/reader, which falls back to cloud."""
    sel = await resolve_observer_route(Combos([_row(model="hybrid/reader")]), _resolver())
    assert not sel.allowed
    assert sel.reason is SelectionReason.ROUTE_DENIED
    assert sel.alias == "hybrid/reader"
    assert sel.route is not None and not sel.route.allowed
    assert sel.route.reason is RouteReason.COMBO_NOT_SECURE


async def test_reader_pointing_at_unflagged_local_combo_is_denied() -> None:
    """local/reader is local-only but OmniRoute still injects memory/tools there."""
    sel = await resolve_observer_route(Combos([_row(model="local/reader")]), _resolver())
    assert not sel.allowed and sel.reason is SelectionReason.ROUTE_DENIED
    assert sel.route is not None and sel.route.reason is RouteReason.COMBO_NOT_SECURE


async def test_reader_alias_unknown_to_omniroute_is_denied() -> None:
    sel = await resolve_observer_route(Combos([_row(model="hybrid/coder")]), _resolver())
    assert not sel.allowed and sel.route is not None
    assert sel.route.reason is RouteReason.ALIAS_UNKNOWN


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([], SelectionReason.ROLE_MISSING),
        ([_row("Coder")], SelectionReason.ROLE_MISSING),
        ([_row("reader")], SelectionReason.ROLE_MISSING),
        ([_row(), _row()], SelectionReason.ROLE_AMBIGUOUS),
        ([_row(model=None)], SelectionReason.ROLE_MALFORMED),
        ([_row(model="")], SelectionReason.ROLE_MALFORMED),
        ([_row(model=7)], SelectionReason.ROLE_MALFORMED),
        ([_row(model="local/reader\nX-Inject: 1")], SelectionReason.ROLE_MALFORMED),
        ([_row(model="x" * 200)], SelectionReason.ROLE_MALFORMED),
        ([_row(provider="OpenAI")], SelectionReason.PROVIDER_NOT_OMNIROUTE),
        ([_row(provider=None)], SelectionReason.PROVIDER_NOT_OMNIROUTE),
        (["not-a-row", 3, None], SelectionReason.ROLE_MISSING),
    ],
)
async def test_unusable_reader_row_denies_without_touching_the_registry(
    rows: list[Any], reason: SelectionReason
) -> None:
    registry = Registry()
    sel = await resolve_observer_route(Combos(rows), _resolver(registry))
    assert not sel.allowed and sel.reason is reason
    assert sel.route is None and sel.alias == ""
    assert registry.calls == []


@pytest.mark.parametrize("rows", [None, {"Reader": "local/reader"}, "Reader"])
async def test_non_list_combo_table_denies(rows: Any) -> None:
    sel = await resolve_observer_route(Combos(rows), _resolver())
    assert not sel.allowed and sel.reason is SelectionReason.COMBOS_UNREADABLE


async def test_combo_source_failure_denies_and_does_not_leak() -> None:
    combos = Combos(error=RuntimeError(f"db password={SECRET}"))
    sel = await resolve_observer_route(combos, _resolver())
    assert not sel.allowed and sel.reason is SelectionReason.COMBOS_UNREADABLE
    assert SECRET not in repr(sel) and SECRET not in str(sel.log_fields())


async def test_selection_is_reread_on_every_call() -> None:
    combos = Combos([_row()])
    resolver = _resolver()
    first = await resolve_observer_route(combos, resolver)
    combos.rows = [_row(model="local/reader")]
    second = await resolve_observer_route(combos, resolver)
    assert first.allowed and not second.allowed and combos.calls == 2


async def test_custom_role_and_provider_names() -> None:
    rows = [_row("Mail", provider="omniroute-prod")]
    sel = await resolve_observer_route(
        Combos(rows), _resolver(), role="Mail", provider_names=frozenset({"omniroute-prod"})
    )
    assert sel.allowed and sel.role == "Mail"


async def test_log_fields_are_bounded_and_secret_free() -> None:
    sel = await resolve_observer_route(Combos([_row()]), _resolver())
    fields = sel.log_fields()
    assert fields["observer_role_combo"] == "Reader"
    assert fields["observer_selection"] == "SELECTED"
    assert fields["observer_route_allowed"] is True
    assert SECRET not in str(fields) and "10.1.1.12" not in str(fields)


def test_allowed_requires_both_selection_and_route() -> None:
    assert not ObserverRouteSelection(SelectionReason.SELECTED, "Reader", "local/reader").allowed


def test_select_alias_is_case_sensitive_on_role_name() -> None:
    assert select_alias([_row("READER")])[0] is SelectionReason.ROLE_MISSING
