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


#: Separate opt-in for personal expectedness (NS-ADR-075); same syntax.
ENV_EXPECTEDNESS_SCOPES = "NERVE_MAIL_SECURITY_EXPECTEDNESS_SCOPES"

#: Live observer contract (NS-ADR-080): ``COMPACT`` (compact@1, the default)
#: or ``COMPACT_V2``. Anything else falls back to the default.
ENV_OBSERVER_CONTRACT = "NERVE_MAIL_SECURITY_OBSERVER_CONTRACT"
_LIVE_CONTRACTS = frozenset({"COMPACT", "COMPACT_V2"})


def observer_contract_from_env(env: Mapping[str, str] | None = None) -> str:
    raw = ((env if env is not None else os.environ).get(ENV_OBSERVER_CONTRACT) or "").strip()
    return raw if raw in _LIVE_CONTRACTS else "COMPACT"


def sender_domain(evidence: Any) -> str | None:
    """Registrable domain of the parsed From header, or ``None``."""
    import email.utils

    from backend.mail_security.inspections import _registrable

    for name, value in getattr(evidence, "headers", []) or []:
        if name.strip().lower() == "from":
            addr = email.utils.parseaddr(value)[1]
            domain = addr.rpartition("@")[2].strip().lower() if "@" in addr else ""
            return _registrable(domain) if domain and _HOST_RE.fullmatch(domain) else None
    return None


def scope_listed(scope: str, env_name: str, env: Mapping[str, str] | None = None) -> bool:
    scopes = observer_scopes(env, env_name=env_name)
    return scopes is None or scope in scopes


def observer_scopes(
    env: Mapping[str, str] | None = None, *, env_name: str = ENV_OBSERVER_SCOPES
) -> frozenset[str] | None:
    """``None`` = all accounts (``*``); a set of scopes; empty = none."""
    raw = ((env if env is not None else os.environ).get(env_name) or "").strip()
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


#: Rule codes that are hygiene, not threat, on ordinary bulk mail. compact@1
#: saw them on most marketing and they primed PHISHING_SUSPECTED (NS-ADR-080),
#: so compact@2 packets omit them. The deterministic policy still weighs them.
OBSERVER_CONTEXT_EXCLUDED_CODES = frozenset(
    {
        "REMOTE_IMAGE_RESOURCE",
        "REMOTE_TRACKING_PIXEL",
        "TINY_TRACKING_IMAGE_MARKUP",
        "HIDDEN_OR_OFFSCREEN_TEXT",
        "EXTERNAL_LINK_RESOURCE",
        "ENGINE_CLEAN_VERDICT_NOT_SAFETY",
        "SCAN_NOT_RUN",
        "DECODED_BYTES_EXCEEDED",
    }
)
#: Expected for email-service senders when DMARC passes (DKIM-aligned).
_EXCLUDED_WHEN_DMARC_PASS = frozenset({"RETURN_PATH_MISMATCH", "RETURN_PATH_DOMAIN_MISMATCH"})
_COUNTED_PROVENANCE = frozenset({"PROVIDER_REPORTED", "INDEPENDENT"})


def sender_auth_of(authentication: Any) -> str:
    """Code-derived DMARC summary for the observer: one of ``SENDER_AUTH_VALUES``.

    Accepts the assessment's ``authentication`` as a model or its JSON dict.
    Only a provider-reported or independent result counts; anything else is
    ``UNKNOWN``. Context for the model only, never a trust decision.
    """
    dmarc = authentication.get("dmarc") if isinstance(authentication, Mapping) else getattr(
        authentication, "dmarc", None
    )
    if dmarc is None:
        return "UNKNOWN"

    def field(name: str) -> str:
        value = dmarc.get(name) if isinstance(dmarc, Mapping) else getattr(dmarc, name, None)
        return str(getattr(value, "value", value) or "")

    if field("provenance") not in _COUNTED_PROVENANCE:
        return "UNKNOWN"
    return {"PASS": "DMARC_PASS", "FAIL": "DMARC_FAIL"}.get(field("result"), "UNKNOWN")


def build_packet(
    source_key: str,
    evidence: Any,
    finding_codes: list[str],
    *,
    sender_auth: str = "UNKNOWN",
    filter_codes: bool = False,
    extra_freemail_domains: frozenset[str] = frozenset(),
) -> EvidencePacket:
    """Minimal, bounded evidence for the observer (truncated, never raw MIME).

    ``filter_codes`` (compact@2) drops hygiene codes; the sender domain, the
    DMARC summary and per-link alignment are code-derived context.
    """
    from backend.mail_security.campaigns import text_of
    from backend.mail_security.inspections import _registrable, _url_host

    subject = " ".join(_subject_of(list(getattr(evidence, "headers", []) or [])).split())
    body = " ".join(text_of(evidence).split())
    from backend.mail_security.auth_evidence import FREEMAIL_DOMAINS

    domain = sender_domain(evidence) or ""
    freemail = bool(domain) and domain in (FREEMAIL_DOMAINS | extra_freemail_domains)
    links: list[EvidenceLink] = []
    seen_hosts: set[str] = set()
    for i, link in enumerate(getattr(evidence, "links", []) or []):
        host = _url_host(getattr(link, "target", "") or "")
        if not host or host in seen_hosts or not _HOST_RE.fullmatch(host):
            continue
        seen_hosts.add(host)
        aligned = (_registrable(host.lower()) == domain) if domain else None
        links.append(EvidenceLink(ref=f"link_{i}", host=host, sender_aligned=aligned))
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
    excluded: frozenset[str] = frozenset()
    if filter_codes:
        excluded = OBSERVER_CONTEXT_EXCLUDED_CODES | (
            _EXCLUDED_WHEN_DMARC_PASS if sender_auth == "DMARC_PASS" else frozenset()
        )
    codes = list(
        dict.fromkeys(c for c in finding_codes if _CODE_RE.fullmatch(c) and c not in excluded)
    )[:MAX_CODES]
    return EvidencePacket(
        source_ref=_opaque(source_key),
        subject=subject[:MAX_SUBJECT_CHARS],
        body_excerpt=body[:MAX_BODY_CHARS],
        links=tuple(links),
        attachments=tuple(attachments),
        deterministic_codes=tuple(codes),
        sender_domain=domain,
        sender_auth=sender_auth,
        sender_freemail=freemail,
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
        expectedness_repository: Any = None,
        expectedness_secret: bytes | None = None,
        contract: str = "COMPACT",
        role: str = "Reader",
        extra_freemail_domains: frozenset[str] = frozenset(),
    ) -> None:
        """Either a fixed ``resolver`` (no snapshot), or ``registry`` +
        ``resolver_factory(registry) -> resolver`` for a per-message snapshot.

        ``contract`` names an ``OutputContract`` (compact@1 by default;
        compact@2 is NS-ADR-080); ``role`` is the NerveCenter combo role whose alias is
        route-guarded (``Reader`` live; another role only for evaluation)."""
        from backend.mail_security.granite_observer import OutputContract

        if resolver is None and (registry is None or resolver_factory is None):
            raise ValueError("need a resolver, or a registry and a resolver_factory")
        self._contract = OutputContract(contract)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,39}", role):
            raise ValueError("invalid role")
        self._role = role
        #: Eval only: reserved test domains (``*.example``) to treat as free mail.
        if any(not d.endswith(".example") for d in extra_freemail_domains):
            raise ValueError("extra free-mail domains must be reserved .example names")
        self._extra_freemail = frozenset(extra_freemail_domains)
        self._combos = combos
        self._resolver = resolver
        self._registry = registry
        self._resolver_factory = resolver_factory
        self._transport = transport
        self._repo = repository
        self._expect_repo = expectedness_repository
        self._expect_secret = expectedness_secret
        #: Paths cached by the most recent message's snapshot at purge time
        #: (always empty afterwards; exposed so tests can prove the purge).
        self.last_snapshot: MessageScopedRegistry | None = None

    async def observe(self, account_scope: str, **kw: Any) -> str:
        return await self._with_resolver(self._observe, account_scope, **kw)

    async def dry_run(
        self,
        account_scope: str,
        *,
        source_key: str,
        evidence: Any,
        finding_codes: list[str],
        sender_auth: str = "UNKNOWN",
    ) -> tuple[str, dict[str, Any] | None, int | None]:
        """Observe without storing anything (evaluation only).

        Same opt-in, route guard, contract and validation as :meth:`observe`;
        returns ``(status, codes or None, latency_ms)``. No row, no
        expectedness count, no gist.
        """
        status, codes, latency, _extra = await self._with_resolver(
            self._run, account_scope, source_key=source_key, evidence=evidence,
            finding_codes=finding_codes, sender_auth=sender_auth,
        )
        return status, codes, latency

    async def _with_resolver(self, fn: Any, account_scope: str, **kw: Any) -> Any:
        if self._resolver is not None:
            return await fn(account_scope, resolver=self._resolver, **kw)
        snapshot = MessageScopedRegistry(self._registry)
        self.last_snapshot = snapshot
        try:
            return await fn(account_scope, resolver=self._resolver_factory(snapshot), **kw)
        finally:
            snapshot.purge()

    async def _run(
        self,
        account_scope: str,
        *,
        resolver: Any,
        source_key: str,
        evidence: Any,
        finding_codes: list[str],
        sender_auth: str,
    ) -> tuple[str, dict[str, Any] | None, int | None, Any]:
        """Route-select and observe; returns ``(status, codes, latency, extra)``.

        ``extra`` is the result (COMPLETED) or the typed error, for recording.
        """
        from backend.mail_security.granite_observer import (
            GraniteObserver,
            GraniteObserverError,
            ObserverConfig,
            OutputContract,
        )
        from backend.mail_security.observer_route import resolve_observer_route

        if not observer_applies(account_scope):
            return "NOT_OPTED_IN", None, None, None
        try:
            packet = build_packet(
                source_key, evidence, finding_codes, sender_auth=sender_auth,
                filter_codes=self._contract is OutputContract.COMPACT_V2,
                extra_freemail_domains=self._extra_freemail,
            )
        except Exception:  # noqa: BLE001 - bounded packet rejected the input
            return "INPUT_INVALID", None, None, None
        selection = await resolve_observer_route(self._combos, resolver, role=self._role)
        if not selection.allowed:
            reason = selection.route.reason.value if selection.route else selection.reason.value
            return f"ROUTE_DENIED_{reason}"[:48], None, None, None
        observer = GraniteObserver(
            resolver, self._transport,
            ObserverConfig(model_alias=selection.alias, contract=self._contract),
        )
        try:
            result = await observer.observe(packet)
        except GraniteObserverError as exc:
            return exc.code.value, None, exc.latency_ms, exc
        return "COMPLETED", observation_codes(result.observation), result.latency_ms, result

    async def _observe(
        self,
        account_scope: str,
        *,
        resolver: Any,
        source_key: str,
        revision: int,
        evidence: Any,
        finding_codes: list[str],
        sender_auth: str = "UNKNOWN",
    ) -> str:
        if not observer_applies(account_scope):
            return "NOT_OPTED_IN"
        if await self._repo.exists(account_scope, source_key, revision):
            return "ALREADY_OBSERVED"
        status, codes, _latency, extra = await self._run(
            account_scope, resolver=resolver, source_key=source_key,
            evidence=evidence, finding_codes=finding_codes, sender_auth=sender_auth,
        )
        if status != "COMPLETED":
            if extra is None:
                await self._repo.record(account_scope, source_key=source_key,
                                        revision=revision, status=status)
            else:
                await self._repo.record(
                    account_scope, source_key=source_key, revision=revision,
                    status=status, route_digest=extra.route_digest or None,
                    attempts=extra.attempts, latency_ms=extra.latency_ms,
                )
            return status
        result = extra
        assert codes is not None  # noqa: S101 - COMPLETED always carries codes
        expectedness = await self._expectedness(account_scope, evidence, codes)
        if expectedness is not None:
            codes["expectedness"], codes["expectedness_sender_key"] = expectedness
        await self._repo.record(
            account_scope, source_key=source_key, revision=revision, status="COMPLETED",
            prompt_version=result.prompt_version, route_digest=result.route_digest,
            attempts=result.attempts, latency_ms=result.latency_ms,
            observation=codes,
        )
        return "COMPLETED"

    async def _expectedness(
        self, account_scope: str, evidence: Any, codes: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str] | None:
        """NS-ADR-075: score this message's asks against the sender's history
        BEFORE counting them, then count them. ``None`` when not opted in or
        the sender has no usable domain. Never raises into the observer."""
        if self._expect_repo is None or self._expect_secret is None:
            return None
        if not scope_listed(account_scope, ENV_EXPECTEDNESS_SCOPES):
            return None
        from backend.mail_security.expectedness import score, sender_key

        domain = sender_domain(evidence)
        types = sorted({r["request_type"] for r in codes.get("requests") or []})
        if not domain or not types:
            return None
        from datetime import UTC, datetime

        key = sender_key(self._expect_secret, account_scope, domain)
        counts = await self._expect_repo.counts(account_scope, key)
        live = getattr(self._expect_repo, "live_feedback_weights", None)
        feedback = await live(account_scope, key, now=datetime.now(UTC)) if live else {}
        scores = [score(t, counts, feedback).as_codes() for t in types]
        await self._expect_repo.add(account_scope, key, types)
        return scores, key


_observer_runtime: Any = None


def set_observer_runtime(runtime: Any) -> None:
    global _observer_runtime
    _observer_runtime = runtime


def get_observer_runtime() -> Any:
    return _observer_runtime


__all__ = [
    "ENV_EXPECTEDNESS_SCOPES",
    "ENV_OBSERVER_SCOPES",
    "OBSERVER_CONTEXT_EXCLUDED_CODES",
    "ENV_OBSERVER_CONTRACT",
    "observer_contract_from_env",
    "sender_auth_of",
    "MessageScopedRegistry",
    "ObserverRuntime",
    "scope_listed",
    "sender_domain",
    "build_packet",
    "get_observer_runtime",
    "observation_codes",
    "observer_applies",
    "observer_scopes",
    "set_observer_runtime",
]
