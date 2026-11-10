"""Fail-closed local-only OmniRoute route resolver (NerveShield Task 4.2, NS-ADR-028).

Private mail content may only ever be sent to a model whose **entire** route is
local (R-009, R-010, R-017, I-04, T-13). An OmniRoute alias name proves nothing
about locality: a combo such as ``hybrid/code`` is a ``fill-first`` list whose
hops may be ``combo-ref`` entries to nested multi-tier pools that end at cloud
providers. This module therefore resolves the alias against the OmniRoute
registry, walks every nested hop to its leaf ``provider/model`` pair, classifies
each leaf by the **provider's configured host class** (loopback / private /
in-cluster versus anything else) and ALLOWS only when every leaf, fallback hops
included, is provably local.

Security stance: **default deny, when in doubt deny.** Any of these deny:
unknown alias, unreachable or slow registry, malformed registry data, unknown
entry kinds, cyclic combos, depth or size bombs, empty combos, missing
provider, provider without a configured URL, userinfo/odd URLs, any provider
host that is not provably local, and any leaf anywhere in the tree that is not
local. A deny means *not evaluated* (I-02), never "safe".

Library only: **no network code** (no ``socket``/``ssl``/``http``/``urllib.request``/
``requests``/``httpx``/``subprocess``). The registry is read through an injected
:class:`RegistryTransport`; a real transport is a deployment-time item. Nothing
imports this module from the pipeline, worker, inspections or policy.

Log safety: the registry JSON is parsed into a minimal typed snapshot that never
copies API keys, tokens, headers, or base URLs. Decisions, identities and error
reasons carry only enum codes, bounded provider/model labels and a digest; they
never carry mail content, credentials or exception text.

Known limits (see NS-ADR-028): classification trusts the provider's configured
host *name* (an in-cluster ``Service`` that proxies to a cloud, or an
``ExternalName`` service, is not detected because this module performs no DNS);
the registry may change between check and use, so callers must re-resolve per
request and compare :attr:`RouteIdentity.digest`.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, Protocol, TypeVar
from urllib.parse import urlsplit

from backend.mail_security.models import ModelRunStatus

T = TypeVar("T")

#: Registry endpoints read (GET only) through the injected transport.
PATH_MODELS = "/v1/models"
PATH_COMBOS = "/api/combos"
PATH_PROVIDERS = "/api/providers"

_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")
_REDACTED = "REDACTED"

_MAX_COMBOS = 2000
_MAX_PROVIDERS = 2000
_MAX_MODELS = 20000
_HARD_MAX_DEPTH = 32

_REF_KINDS = frozenset({"combo-ref", "combo_ref", "comboRef"})
_MODEL_KINDS = frozenset({"model"})


# -- types ---------------------------------------------------------------------


class HostClass(StrEnum):
    """Locality class of a provider host. Only the first three are local."""

    LOOPBACK = "LOOPBACK"
    PRIVATE = "PRIVATE"
    IN_CLUSTER = "IN_CLUSTER"
    REMOTE = "REMOTE"
    UNKNOWN = "UNKNOWN"


_LOCAL_CLASSES = frozenset({HostClass.LOOPBACK, HostClass.PRIVATE, HostClass.IN_CLUSTER})


class RouteReason(StrEnum):
    """Typed, log-safe outcome codes. Everything except ALLOWED is a deny."""

    ALLOWED_LOCAL_ONLY = "ALLOWED_LOCAL_ONLY"
    ENDPOINT_NOT_LOCAL = "ENDPOINT_NOT_LOCAL"
    REGISTRY_UNREACHABLE = "REGISTRY_UNREACHABLE"
    REGISTRY_TIMEOUT = "REGISTRY_TIMEOUT"
    REGISTRY_MALFORMED = "REGISTRY_MALFORMED"
    ALIAS_INVALID = "ALIAS_INVALID"
    ALIAS_UNKNOWN = "ALIAS_UNKNOWN"
    COMBO_CYCLE = "COMBO_CYCLE"
    COMBO_TOO_DEEP = "COMBO_TOO_DEEP"
    COMBO_TOO_LARGE = "COMBO_TOO_LARGE"
    COMBO_EMPTY = "COMBO_EMPTY"
    COMBO_NOT_SECURE = "COMBO_NOT_SECURE"
    ENTRY_MALFORMED = "ENTRY_MALFORMED"
    PROVIDER_MISSING = "PROVIDER_MISSING"
    PROVIDER_NOT_ALLOWLISTED = "PROVIDER_NOT_ALLOWLISTED"
    LEAF_NON_LOCAL = "LEAF_NON_LOCAL"
    LEAF_UNKNOWN_LOCALITY = "LEAF_UNKNOWN_LOCALITY"


class RegistryTransport(Protocol):
    """Injected read-only registry access (GET returning parsed JSON).

    A deployment-time implementation must: use GET only, never follow redirects
    to another origin, bound the response size, and keep credentials out of
    exceptions. This module never sees headers or keys.
    """

    async def get_json(self, path: str) -> Any: ...


class RouteDeniedError(Exception):
    """Raised by :meth:`RouteDecision.require_allowed`. Carries the code only."""

    def __init__(self, reason: RouteReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class ResolverLimits:
    """Bounds that keep resolution cheap and deny-on-bomb."""

    max_depth: int = 8
    max_entries: int = 1024
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_depth <= _HARD_MAX_DEPTH:
            raise ValueError("max_depth out of range")
        if self.max_entries < 1 or self.timeout_seconds <= 0:
            raise ValueError("limits must be positive")


@dataclass(frozen=True)
class LeafIdentity:
    """A resolved leaf: bounded labels and host class only (no URL, no key)."""

    provider: str
    model: str
    host_class: HostClass


@dataclass(frozen=True)
class RouteIdentity:
    """Log-safe identity of a fully resolved route."""

    alias: str
    leaves: tuple[LeafIdentity, ...]
    combo_count: int
    max_depth: int
    digest: str

    def log_fields(self) -> dict[str, object]:
        """Flat, bounded fields safe for logs/metrics (no content, no secrets)."""
        return {
            "route_alias": self.alias,
            "route_digest": self.digest,
            "route_leaf_count": len(self.leaves),
            "route_combo_count": self.combo_count,
            "route_max_depth": self.max_depth,
        }


@dataclass(frozen=True)
class RouteDecision:
    """Outcome of resolving one alias. ``identity`` is set only when allowed."""

    allowed: bool
    reason: RouteReason
    alias: str
    detail: str = ""
    identity: RouteIdentity | None = None

    @property
    def model_run_status(self) -> ModelRunStatus:
        """Status to record for a model run that was gated by this decision."""
        return ModelRunStatus.NOT_RUN if self.allowed else ModelRunStatus.DENIED_BY_POLICY

    def require_allowed(self) -> RouteIdentity:
        if not self.allowed or self.identity is None:
            raise RouteDeniedError(self.reason)
        return self.identity

    def log_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "route_allowed": self.allowed,
            "route_reason": self.reason.value,
            "route_alias": self.alias,
        }
        if self.detail:
            fields["route_detail"] = self.detail
        if self.identity is not None:
            fields.update(self.identity.log_fields())
        return fields


@dataclass(frozen=True)
class GuardedResult(Generic[T]):
    """Result of :func:`dispatch_if_local`. ``sent`` is False on every deny."""

    decision: RouteDecision
    sent: bool
    value: T | None = None


# -- host classification (pure, offline; no DNS) --------------------------------


def classify_host(url: object) -> HostClass:
    """Classify the host of a provider base URL. Unproven means not local."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        return HostClass.UNKNOWN
    try:
        parts = urlsplit(url.strip())
        port = parts.port  # raises ValueError on a bad port
    except ValueError:
        return HostClass.UNKNOWN
    del port
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return HostClass.UNKNOWN
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or "%" in host:
        return HostClass.UNKNOWN
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return _classify_name(host)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_loopback:  # before is_reserved: ::1 sits in the reserved ::/8
        return HostClass.LOOPBACK
    if addr.is_unspecified or addr.is_link_local or addr.is_multicast or addr.is_reserved:
        return HostClass.UNKNOWN
    if addr.is_private:
        return HostClass.PRIVATE
    if addr.is_global:
        return HostClass.REMOTE
    return HostClass.UNKNOWN  # e.g. CGNAT 100.64/10: neither proven private nor public


def _classify_name(host: str) -> HostClass:
    labels = host.split(".")
    if any(not label for label in labels):
        return HostClass.UNKNOWN
    if host == "localhost":
        return HostClass.LOOPBACK
    if host.endswith((".svc", ".svc.cluster.local", ".cluster.local")):
        return HostClass.IN_CLUSTER
    if len(labels) >= 2 and not labels[-1].isdigit():
        return HostClass.REMOTE
    return HostClass.UNKNOWN  # bare/ambiguous names (could be anything) are not proven


# -- registry snapshot ----------------------------------------------------------


class _Deny(Exception):
    """Internal control flow carrying a reason and a safe label."""

    def __init__(self, reason: RouteReason, detail: str = "") -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.detail = detail


def _label(value: object) -> str:
    """Bounded, charset-restricted label; anything else is redacted."""
    if isinstance(value, str) and _LABEL_RE.fullmatch(value):
        return value
    return _REDACTED


@dataclass(frozen=True)
class _Entry:
    kind: str  # "ref" | "leaf"
    name: str = ""  # combo name for refs
    provider: str = ""
    model: str = ""


@dataclass(frozen=True)
class _Combo:
    name: str
    strategy: str
    entries: tuple[_Entry, ...]
    secure: bool = False


@dataclass(frozen=True)
class _Snapshot:
    model_ids: frozenset[str]
    combos: Mapping[str, _Combo]
    provider_hosts: Mapping[str, tuple[HostClass, ...]]


def _items(doc: Any, keys: tuple[str, ...], limit: int) -> list[Any]:
    """Extract the item list from a list or a single-key container, else deny."""
    items: Any = None
    if isinstance(doc, list):
        items = doc
    elif isinstance(doc, dict):
        for key in keys:
            if key in doc:
                items = doc[key]
                break
    if not isinstance(items, list) or len(items) > limit:
        raise _Deny(RouteReason.REGISTRY_MALFORMED)
    return items


def _parse_models(doc: Any) -> frozenset[str]:
    ids: set[str] = set()
    for item in _items(doc, ("data", "models"), _MAX_MODELS):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise _Deny(RouteReason.REGISTRY_MALFORMED)
        ids.add(item["id"])
    return frozenset(ids)


def _parse_entry(raw: Any, combo_names: frozenset[str]) -> _Entry:
    if isinstance(raw, str):
        if raw in combo_names:
            return _Entry(kind="ref", name=raw)
        return _split_leaf(raw)
    if not isinstance(raw, dict):
        raise _Deny(RouteReason.ENTRY_MALFORMED)
    kind = raw.get("kind", "model")
    if kind in _REF_KINDS:
        name = next(
            (raw[k] for k in ("comboName", "combo", "name") if isinstance(raw.get(k), str)), None
        )
        if name is None or name not in combo_names:
            raise _Deny(RouteReason.ENTRY_MALFORMED)
        return _Entry(kind="ref", name=name)
    if kind not in _MODEL_KINDS or not isinstance(raw.get("model"), str):
        raise _Deny(RouteReason.ENTRY_MALFORMED)
    model = raw["model"]
    provider_id = raw.get("providerId", raw.get("provider"))
    if provider_id is None:
        return _split_leaf(model)
    if not isinstance(provider_id, str) or not provider_id:
        raise _Deny(RouteReason.ENTRY_MALFORMED)
    prefix = provider_id + "/"
    bare = model[len(prefix) :] if model.startswith(prefix) else model
    if not bare:
        raise _Deny(RouteReason.ENTRY_MALFORMED)
    return _Entry(kind="leaf", provider=provider_id, model=bare)


def _split_leaf(value: str) -> _Entry:
    provider, sep, model = value.partition("/")
    if not sep or not provider or not model:
        raise _Deny(RouteReason.ENTRY_MALFORMED)
    return _Entry(kind="leaf", provider=provider, model=model)


def _parse_combos(doc: Any) -> dict[str, _Combo]:
    raw_items = _items(doc, ("combos", "data"), _MAX_COMBOS)
    names: list[str] = []
    for item in raw_items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise _Deny(RouteReason.REGISTRY_MALFORMED)
        names.append(item["name"])
    if len(set(names)) != len(names):
        raise _Deny(RouteReason.REGISTRY_MALFORMED)  # ambiguous duplicate combo name
    combo_names = frozenset(names)
    combos: dict[str, _Combo] = {}
    for item in raw_items:
        has_models, has_steps = "models" in item, "steps" in item
        if has_models == has_steps:  # neither or both: ambiguous
            raise _Deny(RouteReason.ENTRY_MALFORMED, _label(item["name"]))
        raw_entries = item["models"] if has_models else item["steps"]
        if not isinstance(raw_entries, list):
            raise _Deny(RouteReason.ENTRY_MALFORMED, _label(item["name"]))
        try:
            entries = tuple(_parse_entry(e, combo_names) for e in raw_entries)
        except _Deny as exc:
            raise _Deny(exc.reason, _label(item["name"])) from None
        strategy = item.get("strategy")
        config = item.get("config")
        combos[item["name"]] = _Combo(
            name=item["name"],
            strategy=strategy if isinstance(strategy, str) else "",
            entries=entries,
            # Strict: only a JSON boolean true counts ("true", 1 do not).
            secure=isinstance(config, dict) and config.get("secure") is True,
        )
    return combos


def _provider_base_url(item: dict[str, Any]) -> object:
    data = item.get("providerSpecificData")
    nested = data.get("baseUrl") if isinstance(data, dict) else None
    for candidate in (item.get("baseUrl"), item.get("base_url"), nested):
        if candidate is not None:
            return candidate
    return None


def _parse_providers(doc: Any) -> dict[str, tuple[HostClass, ...]]:
    """Map provider id/alias to the host class of *every* connection.

    Only the id, alias and base URL are read. API keys, tokens and any other
    field are never copied.
    """
    hosts: dict[str, list[HostClass]] = {}
    for item in _items(doc, ("connections", "providers", "data"), _MAX_PROVIDERS):
        if not isinstance(item, dict):
            raise _Deny(RouteReason.REGISTRY_MALFORMED)
        keys = [
            k
            for k in (item.get("provider", item.get("id")), item.get("alias"), item.get("prefix"))
            if isinstance(k, str) and k
        ]
        if not keys:
            raise _Deny(RouteReason.REGISTRY_MALFORMED)
        host_class = classify_host(_provider_base_url(item))
        for key in keys:
            hosts.setdefault(key, []).append(host_class)
    return {key: tuple(value) for key, value in hosts.items()}


# -- resolver -------------------------------------------------------------------


class LocalOnlyRouteResolver:
    """Resolve an OmniRoute alias and ALLOW only a provably local-only route.

    ``allowed_provider_ids`` is an optional operator allowlist applied on top of
    host classification (defence in depth): when given, a leaf's provider must
    be in it as well as local. Every call reads a fresh registry snapshot; there
    is no cache, so a changed registry can never be served from stale state.
    """

    def __init__(
        self,
        transport: RegistryTransport,
        *,
        omniroute_base_url: str,
        limits: ResolverLimits | None = None,
        allowed_provider_ids: frozenset[str] | None = None,
        require_secure_combo: bool = True,
    ) -> None:
        self._transport = transport
        self._require_secure = require_secure_combo
        self._base_url = omniroute_base_url
        self._limits = limits or ResolverLimits()
        self._allowed = allowed_provider_ids

    async def resolve(self, alias: str) -> RouteDecision:
        """Never raises for a deny; every failure is a typed deny decision."""
        safe_alias = _label(alias)
        try:
            return await self._resolve(alias, safe_alias)
        except _Deny as exc:
            return RouteDecision(False, exc.reason, safe_alias, detail=exc.detail)
        except Exception:  # noqa: BLE001 - fail closed; never surface exception text
            return RouteDecision(False, RouteReason.REGISTRY_MALFORMED, safe_alias)

    async def _resolve(self, alias: str, safe_alias: str) -> RouteDecision:
        if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
            raise _Deny(RouteReason.ALIAS_INVALID)
        if classify_host(self._base_url) not in _LOCAL_CLASSES:
            raise _Deny(RouteReason.ENDPOINT_NOT_LOCAL)
        snapshot = await self._load()
        if alias not in snapshot.model_ids and alias not in snapshot.combos:
            raise _Deny(RouteReason.ALIAS_UNKNOWN)
        if self._require_secure:
            # OmniRoute's per-combo isolation flag (no memory, skills, styles or
            # injected context; whole tree). A bare model id or an unflagged
            # combo cannot prove isolation, so it is denied (NS-ADR-034).
            top = snapshot.combos.get(alias)
            if top is None or not top.secure:
                raise _Deny(RouteReason.COMBO_NOT_SECURE)
        walker = _Walker(snapshot, self._limits, self._allowed)
        leaves, depth = walker.walk_alias(alias)
        if not leaves:
            raise _Deny(RouteReason.COMBO_EMPTY)
        identity = RouteIdentity(
            alias=safe_alias,
            leaves=tuple(sorted(leaves, key=lambda leaf: (leaf.provider, leaf.model))),
            combo_count=len(walker.visited),
            max_depth=depth,
            digest=walker.digest(alias, leaves),
        )
        return RouteDecision(True, RouteReason.ALLOWED_LOCAL_ONLY, safe_alias, identity=identity)

    async def _load(self) -> _Snapshot:
        try:
            async with asyncio.timeout(self._limits.timeout_seconds):
                models_doc = await self._transport.get_json(PATH_MODELS)
                combos_doc = await self._transport.get_json(PATH_COMBOS)
                providers_doc = await self._transport.get_json(PATH_PROVIDERS)
        except TimeoutError:
            raise _Deny(RouteReason.REGISTRY_TIMEOUT) from None
        except Exception:  # noqa: BLE001 - transport errors may embed secrets: drop text
            raise _Deny(RouteReason.REGISTRY_UNREACHABLE) from None
        return _Snapshot(
            model_ids=_parse_models(models_doc),
            combos=_parse_combos(combos_doc),
            provider_hosts=_parse_providers(providers_doc),
        )


class _Walker:
    """Depth/size-bounded, cycle-detecting walk to every leaf."""

    def __init__(
        self, snapshot: _Snapshot, limits: ResolverLimits, allowed: frozenset[str] | None
    ) -> None:
        self._snap = snapshot
        self._limits = limits
        self._allowed = allowed
        self._entries_seen = 0
        self._memo: dict[str, tuple[tuple[LeafIdentity, ...], int]] = {}
        self.visited: set[str] = set()

    def walk_alias(self, alias: str) -> tuple[tuple[LeafIdentity, ...], int]:
        if alias in self._snap.combos:
            return self._walk_combo(alias, 1, ())
        # Not a combo: a direct provider/model id from the models listing.
        return (self._leaf(_split_leaf(alias)),), 1

    def _walk_combo(
        self, name: str, depth: int, stack: tuple[str, ...]
    ) -> tuple[tuple[LeafIdentity, ...], int]:
        if name in stack:
            raise _Deny(RouteReason.COMBO_CYCLE, _label(name))
        if name in self._memo:
            leaves, height = self._memo[name]
            if depth + height - 1 > self._limits.max_depth:
                raise _Deny(RouteReason.COMBO_TOO_DEEP, _label(name))
            return leaves, height
        if depth > self._limits.max_depth:
            raise _Deny(RouteReason.COMBO_TOO_DEEP, _label(name))
        combo = self._snap.combos[name]
        if not combo.entries:
            raise _Deny(RouteReason.COMBO_EMPTY, _label(name))
        self.visited.add(name)
        leaves: list[LeafIdentity] = []
        height = 1
        for entry in combo.entries:
            self._entries_seen += 1
            if self._entries_seen > self._limits.max_entries:
                raise _Deny(RouteReason.COMBO_TOO_LARGE)
            if entry.kind == "ref":
                sub_leaves, sub_height = self._walk_combo(entry.name, depth + 1, (*stack, name))
                leaves.extend(sub_leaves)
                height = max(height, sub_height + 1)
            else:
                leaves.append(self._leaf(entry))
        result = (tuple(dict.fromkeys(leaves)), height)
        self._memo[name] = result
        return result

    def _leaf(self, entry: _Entry) -> LeafIdentity:
        provider = _label(entry.provider)
        hosts = self._snap.provider_hosts.get(entry.provider)
        if not hosts:
            raise _Deny(RouteReason.PROVIDER_MISSING, provider)
        if any(h is HostClass.REMOTE for h in hosts):
            raise _Deny(RouteReason.LEAF_NON_LOCAL, provider)
        if any(h not in _LOCAL_CLASSES for h in hosts):
            raise _Deny(RouteReason.LEAF_UNKNOWN_LOCALITY, provider)
        if self._allowed is not None and entry.provider not in self._allowed:
            raise _Deny(RouteReason.PROVIDER_NOT_ALLOWLISTED, provider)
        # Report the least-local class among connections (all are local here).
        order = (HostClass.IN_CLUSTER, HostClass.PRIVATE, HostClass.LOOPBACK)
        worst = next(c for c in order if c in hosts)
        return LeafIdentity(provider=provider, model=_label(entry.model), host_class=worst)

    def digest(self, alias: str, leaves: tuple[LeafIdentity, ...]) -> str:
        combos = {
            name: [
                self._snap.combos[name].strategy,
                self._snap.combos[name].secure,
                [
                    e.name if e.kind == "ref" else f"{e.provider}/{e.model}"
                    for e in self._snap.combos[name].entries
                ],
            ]
            for name in sorted(self.visited)
        }
        canonical = json.dumps(
            {
                "alias": alias,
                "combos": combos,
                "leaves": sorted(
                    [leaf.provider, leaf.model, leaf.host_class.value] for leaf in leaves
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]


# -- guarded dispatch -----------------------------------------------------------


async def dispatch_if_local(
    resolver: LocalOnlyRouteResolver,
    alias: str,
    send: Callable[[RouteIdentity], Awaitable[T]],
) -> GuardedResult[T]:
    """Resolve first; call ``send`` only when the whole route is local-only.

    ``send`` is the only code that ever sees content, and it is not invoked on
    any deny, so no content can leave through a remote hop (T-13). The decision
    is made per call from a fresh registry read; ``send`` receives the identity
    so it can pin or compare :attr:`RouteIdentity.digest`.
    """
    decision = await resolver.resolve(alias)
    if not decision.allowed or decision.identity is None:
        return GuardedResult(decision=decision, sent=False)
    value = await send(decision.identity)
    return GuardedResult(decision=decision, sent=True, value=value)


__all__ = [
    "PATH_COMBOS",
    "PATH_MODELS",
    "PATH_PROVIDERS",
    "GuardedResult",
    "HostClass",
    "LeafIdentity",
    "LocalOnlyRouteResolver",
    "RegistryTransport",
    "ResolverLimits",
    "RouteDecision",
    "RouteDeniedError",
    "RouteIdentity",
    "RouteReason",
    "classify_host",
    "dispatch_if_local",
]
