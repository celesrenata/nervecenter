"""Local-first model policy (NS-ADR-107).

Owner rule: "we need to take our time when hitting openai, and we need to only
hit openai when it matters, we have 3 models locally to use first with no api
limits".

* Every model call goes to a local OmniRoute target first. The three local
  backends (OmniRoute catalog, 2026-11-16): ``local/esnixi`` (vllm
  qwen3.8-27b-nvfp4), ``local/stabulous`` (llama-cpp deepseek-v41-1017) and
  ``local/gremlin-1`` (vllm granite-4.2-8b). The pooled combos ``local/*`` and
  ``secure/*`` are capped at tier 1 (``maximumTier: 1``), which is local only.
* A configured model that is not local (``auto/*``, ``cloud/*``, ``hybrid/*``,
  ``openai/*`` ...) is replaced by :func:`local_default` and the swap is logged.
* The OpenAI API is used only when :func:`decide` returns an escalation with one
  of the :class:`Reason` codes. Background callers escalate only for
  ``owner_requested`` or ``high_stakes``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

log = structlog.get_logger(__name__)

LOCAL_MODEL_ENV = "NERVE_LOCAL_MODEL"
LOCAL_MAX_FAILURES_ENV = "NERVE_LOCAL_MAX_FAILURES"
DEFAULT_LOCAL_MODEL = "local/any"
DEFAULT_LOCAL_MAX_FAILURES = 2

#: OmniRoute model or combo names that only resolve to the local backends.
LOCAL_PREFIXES = (
    "local/",
    "secure/",
    "pool/tier1/",
    "pool/tier1b/",
    "vllm/",
    "llama-cpp/",
    "ollama-local/",
)
#: Capabilities only the OpenAI API path has.
OPENAI_ONLY_CAPABILITIES = frozenset({"openai_mcp_tunnel"})


class Reason(StrEnum):
    OWNER_REQUESTED = "owner_requested"  # (a)
    HIGH_STAKES = "high_stakes"  # (b)
    LOCAL_FAILED = "local_failed"  # (c)
    CAPABILITY_REQUIRED = "capability_required"  # (d)


@dataclass(frozen=True)
class Decision:
    target: str  # "local" | "openai"
    model: str  # OmniRoute model for local; "" for openai (engine picks)
    reason: Reason | None = None

    @property
    def escalated(self) -> bool:
        return self.target == "openai"


def is_local(model: str | None) -> bool:
    return bool(model) and str(model).strip().lower().startswith(LOCAL_PREFIXES)


def local_default(env: dict[str, str] | None = None) -> str:
    raw = ((env if env is not None else os.environ).get(LOCAL_MODEL_ENV) or "").strip()
    return raw if is_local(raw) else DEFAULT_LOCAL_MODEL


def max_local_failures(env: dict[str, str] | None = None) -> int:
    raw = ((env if env is not None else os.environ).get(LOCAL_MAX_FAILURES_ENV) or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_LOCAL_MAX_FAILURES
    except ValueError:
        return DEFAULT_LOCAL_MAX_FAILURES


def local_model(requested: str | None, *, caller: str, env: dict[str, str] | None = None) -> str:
    """The local model to use for ``requested`` (itself when already local)."""
    if is_local(requested):
        return str(requested).strip()
    chosen = local_default(env)
    if requested:
        log.info(
            "inference.local_first.forced_local",
            caller=caller,
            requested=str(requested)[:64],
            model=chosen,
        )
    return chosen


def decide(
    requested: str | None,
    *,
    caller: str,
    background: bool,
    owner_requested: bool = False,
    high_stakes: bool = False,
    local_failures: int = 0,
    capability: str | None = None,
    env: dict[str, str] | None = None,
) -> Decision:
    """Pick local or OpenAI for one call and say why. Never sees content.

    * owner_requested (a): OpenAI.
    * background (agents, researcher): otherwise OpenAI only for a high-stakes
      task (b) once local failed ``max_local_failures`` times or an OpenAI-only
      capability is needed; reason ``high_stakes``.
    * foreground: an OpenAI-only capability (d), or local failed N times (c).
      A high-stakes foreground task also stays local until then.
    """
    needs_capability = capability in OPENAI_ONLY_CAPABILITIES
    failed = local_failures >= max_local_failures(env)
    reason: Reason | None = None
    if owner_requested:
        reason = Reason.OWNER_REQUESTED
    elif background:
        if high_stakes and (failed or needs_capability):
            reason = Reason.HIGH_STAKES
    elif needs_capability:
        reason = Reason.CAPABILITY_REQUIRED
    elif failed:
        reason = Reason.LOCAL_FAILED
    if reason is not None:
        log.info(
            "inference.local_first.escalate",
            caller=caller,
            reason=str(reason),
            background=background,
        )
        return Decision("openai", "", reason)
    return Decision("local", local_model(requested, caller=caller, env=env))


class LocalFirstTransport:
    """Wraps an OmniRoute transport so every ``generate`` uses a local model."""

    def __init__(self, inner: Any, caller: str) -> None:
        self._inner = inner
        self._caller = caller

    def generate(self, model: str, prompt: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._inner.generate(
            local_model(model, caller=self._caller), prompt, *args, **kwargs
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def background_call(
    requested: str | None,
    grant: str | None,
    *,
    caller: str,
    local: Any,
    openai: Any,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one background call (agent runner) under the policy.

    ``local(model)`` and ``openai(reason)`` are async callables. Without a grant
    there is exactly one local attempt and no escalation. ``owner_requested``
    goes to OpenAI; ``high_stakes`` tries local ``max_local_failures`` times
    and then escalates.
    """
    high = grant == "high_stakes"
    first = decide(
        requested,
        caller=caller,
        background=True,
        owner_requested=grant == "owner_requested",
        high_stakes=high,
        env=env,
    )
    if first.escalated:
        return await openai(first.reason)
    failures = 0
    while True:
        try:
            return await local(first.model)
        except Exception:
            failures += 1
            if not high:
                raise
            nxt = decide(
                requested,
                caller=caller,
                background=True,
                high_stakes=True,
                local_failures=failures,
                env=env,
            )
            if nxt.escalated:
                return await openai(nxt.reason)
