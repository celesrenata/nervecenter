"""Live wiring for the SHADOW semantic observer (NS-ADR-073, G8 item 3).

Owner decision (2026-11-13, "do em"): real mail may be sent to the local
Granite reader. Preconditions proven live first (evidence
``semantic-observer.md``): through ``secure/reader`` OmniRoute injects nothing,
writes no memory (positive control: the same canary through ``local/reader``
WAS written) and stores no request/response bodies; the Granite service logs
no prompts.

What this adds (the observer itself, ``granite_observer``, is unchanged):

* The HTTP transports live in :mod:`backend.connectors.omniroute_observer`,
  because this package stays network-free (test-enforced).
* :func:`build_packet` — the minimal evidence packet from parsed mail: subject,
  a bounded plain-text excerpt, link hosts, attachment kinds and the
  deterministic finding codes.
* Opt-in per account: ``NERVE_MAIL_SECURITY_OBSERVER_SCOPES`` (``*`` or a
  ``conn:<uuid>`` list; unset/malformed = none), and only with mail security ON.

SHADOW only: the observation is stored as enum codes (no ``gist`` free text)
and never changes a disposition, the gate or attention.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from backend.mail_security.granite_observer import (
    MAX_ATTACHMENTS,
    MAX_BODY_CHARS,
    MAX_CODES,
    MAX_LINKS,
    MAX_SUBJECT_CHARS,
    EvidenceAttachment,
    EvidenceLink,
    EvidencePacket,
)

ENV_OBSERVER_SCOPES = "NERVE_MAIL_SECURITY_OBSERVER_SCOPES"
_SCOPE_RE = re.compile(r"^conn:[0-9a-f-]{36}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_KIND_RE = re.compile(r"^[A-Za-z0-9._/+-]{1,40}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_.-]{0,79}$")
_REF_SAFE = re.compile(r"[^A-Za-z0-9:_-]")


def observer_scopes(env: Mapping[str, str] | None = None) -> frozenset[str] | None:
    """``None`` = all accounts (``*``); a set of scopes; empty = none."""
    raw = ((env if env is not None else os.environ).get(ENV_OBSERVER_SCOPES) or "").strip()
    if raw == "*":
        return None
    items = [s.strip() for s in raw.split(",") if s.strip()]
    if not items or any(not _SCOPE_RE.fullmatch(s) for s in items):
        return frozenset()
    return frozenset(items)


def observer_applies(scope: str, env: Mapping[str, str] | None = None) -> bool:
    scopes = observer_scopes(env)
    return scopes is None or scope in scopes


def _opaque(value: str) -> str:
    return _REF_SAFE.sub("_", value)[:200] or "ref"


def _subject_of(headers: list[tuple[str, str]]) -> str:
    for name, value in headers:
        if name.strip().lower() == "subject":
            return value
    return ""


def build_packet(source_key: str, evidence: Any, finding_codes: list[str]) -> EvidencePacket:
    """Minimal, bounded evidence for the observer (truncated, never raw MIME)."""
    from backend.mail_security.campaigns import text_of
    from backend.mail_security.inspections import _url_host

    subject = " ".join(_subject_of(list(getattr(evidence, "headers", []) or [])).split())
    body = " ".join(text_of(evidence).split())
    links: list[EvidenceLink] = []
    seen_hosts: set[str] = set()
    for i, link in enumerate(getattr(evidence, "links", []) or []):
        host = _url_host(getattr(link, "target", "") or "")
        if not host or host in seen_hosts or not _HOST_RE.fullmatch(host):
            continue
        seen_hosts.add(host)
        links.append(EvidenceLink(ref=f"link_{i}", host=host))
        if len(links) >= MAX_LINKS:
            break
    attachments: list[EvidenceAttachment] = []
    for i, att in enumerate(getattr(evidence, "attachments", []) or []):
        # The sniffed type (what the bytes are), else the sender's claim. Never
        # the filename, which is free text.
        claimed = getattr(att, "claimed_content_type", "") or ""
        kind = str(getattr(att, "sniffed_type", None) or claimed)
        kind = kind.split(";")[0].strip().lower() or "unknown"
        attachments.append(
            EvidenceAttachment(ref=f"att_{i}", kind=kind if _KIND_RE.fullmatch(kind) else "unknown")
        )
        if len(attachments) >= MAX_ATTACHMENTS:
            break
    codes = list(dict.fromkeys(c for c in finding_codes if _CODE_RE.fullmatch(c)))[:MAX_CODES]
    return EvidencePacket(
        source_ref=_opaque(source_key),
        subject=subject[:MAX_SUBJECT_CHARS],
        body_excerpt=body[:MAX_BODY_CHARS],
        links=tuple(links),
        attachments=tuple(attachments),
        deterministic_codes=tuple(codes),
    )


def observation_codes(observation: Any) -> dict[str, Any]:
    """Enum/code-only summary of a SemanticObservation (no ``gist`` text)."""
    ident = observation.claimed_identity
    return {
        "message_category": observation.message_category.value,
        "claimed_role": ident.claimed_role.value,
        "identity_consistency": ident.identity_consistency.value,
        "requests": [
            {
                "request_type": r.request_type.value,
                "claimed_urgency": r.claimed_urgency.value,
                "high_impact": bool(r.high_impact),
                "requires_independent_verification": bool(r.requires_independent_verification),
            }
            for r in observation.requests
        ],
        "risk_indicators": sorted({str(r.indicator_code) for r in observation.risk_indicators}),
    }


class MessageScopedRegistry:
    """Temporary registry snapshot for ONE message's observation (NS-ADR-074).

    Owner: "a temporary cache is fine, as long as it is purged when no longer
    needed". The route is checked twice per message (alias selection, then the
    observer's own guard); this lets both checks read the same snapshot (three
    GETs instead of six, and one consistent view) and retries a failed read
    once. It is created per message and :meth:`purge`-d in ``finally`` by the
    runtime, so nothing survives between messages: an OmniRoute edit still
    takes effect on the very next message.
    """

    def __init__(self, inner: Any, *, retries: int = 1) -> None:
        self._inner = inner
        self._retries = max(0, int(retries))
        self._docs: dict[str, Any] = {}

    async def get_json(self, path: str) -> Any:
        if path in self._docs:
            return self._docs[path]
        last: Exception | None = None
        for _ in range(self._retries + 1):
            try:
                doc = await self._inner.get_json(path)
            except Exception as exc:  # noqa: BLE001 - the resolver maps this to a typed deny
                last = exc
                continue
            self._docs[path] = doc
            return doc
        raise ValueError("registry unavailable") from last

    def purge(self) -> None:
        self._docs.clear()

    @property
    def cached_paths(self) -> frozenset[str]:
        return frozenset(self._docs)


class ObserverRuntime:
    """Route-select, observe and record one message (SHADOW, best-effort).

    Network-free: the registry and chat transports are injected (they live in
    :mod:`backend.connectors.omniroute_observer`). The NerveCenter ``Reader``
    combo is re-read and route-guarded on every call; any deny or observer
    error is recorded as a typed status and nothing else happens.
    """

    def __init__(
        self,
        *,
        combos: Any,
        transport: Any,
        repository: Any,
        resolver: Any = None,
        registry: Any = None,
        resolver_factory: Any = None,
    ) -> None:
        """Either a fixed ``resolver`` (no snapshot), or ``registry`` +
        ``resolver_factory(registry) -> resolver`` for a per-message snapshot."""
        if resolver is None and (registry is None or resolver_factory is None):
            raise ValueError("need a resolver, or a registry and a resolver_factory")
        self._combos = combos
        self._resolver = resolver
        self._registry = registry
        self._resolver_factory = resolver_factory
        self._transport = transport
        self._repo = repository
        #: Paths cached by the most recent message's snapshot at purge time
        #: (always empty afterwards; exposed so tests can prove the purge).
        self.last_snapshot: MessageScopedRegistry | None = None

    async def observe(self, account_scope: str, **kw: Any) -> str:
        if self._resolver is not None:
            return await self._observe(account_scope, resolver=self._resolver, **kw)
        snapshot = MessageScopedRegistry(self._registry)
        self.last_snapshot = snapshot
        try:
            return await self._observe(
                account_scope, resolver=self._resolver_factory(snapshot), **kw
            )
        finally:
            snapshot.purge()

    async def _observe(
        self,
        account_scope: str,
        *,
        resolver: Any,
        source_key: str,
        revision: int,
        evidence: Any,
        finding_codes: list[str],
    ) -> str:
        from backend.mail_security.granite_observer import (
            GraniteObserver,
            GraniteObserverError,
            ObserverConfig,
        )
        from backend.mail_security.observer_route import resolve_observer_route

        if not observer_applies(account_scope):
            return "NOT_OPTED_IN"
        if await self._repo.exists(account_scope, source_key, revision):
            return "ALREADY_OBSERVED"
        try:
            packet = build_packet(source_key, evidence, finding_codes)
        except Exception:  # noqa: BLE001 - bounded packet rejected the input
            await self._repo.record(account_scope, source_key=source_key, revision=revision,
                                    status="INPUT_INVALID")
            return "INPUT_INVALID"
        selection = await resolve_observer_route(self._combos, resolver)
        if not selection.allowed:
            reason = selection.route.reason.value if selection.route else selection.reason.value
            status = f"ROUTE_DENIED_{reason}"[:48]
            await self._repo.record(account_scope, source_key=source_key, revision=revision,
                                    status=status)
            return status
        observer = GraniteObserver(
            resolver, self._transport, ObserverConfig(model_alias=selection.alias)
        )
        try:
            result = await observer.observe(packet)
        except GraniteObserverError as exc:
            await self._repo.record(
                account_scope, source_key=source_key, revision=revision,
                status=exc.code.value, route_digest=exc.route_digest or None,
                attempts=exc.attempts, latency_ms=exc.latency_ms,
            )
            return exc.code.value
        await self._repo.record(
            account_scope, source_key=source_key, revision=revision, status="COMPLETED",
            prompt_version=result.prompt_version, route_digest=result.route_digest,
            attempts=result.attempts, latency_ms=result.latency_ms,
            observation=observation_codes(result.observation),
        )
        return "COMPLETED"


_observer_runtime: Any = None


def set_observer_runtime(runtime: Any) -> None:
    global _observer_runtime
    _observer_runtime = runtime


def get_observer_runtime() -> Any:
    return _observer_runtime


__all__ = [
    "ENV_OBSERVER_SCOPES",
    "ObserverRuntime",
    "build_packet",
    "get_observer_runtime",
    "observation_codes",
    "observer_applies",
    "observer_scopes",
    "set_observer_runtime",
]
