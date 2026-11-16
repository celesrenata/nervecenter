"""One shared client for model calls that carry personal data (NS-ADR-108).

Owner rule: "there is a secure header we can flag it with, secure means it
doesn't hit conversation history". This reuses the live-proven NS-ADR-073
pattern (``backend/connectors/omniroute_observer.py``):

* the model is an OmniRoute ``secure/*`` combo (OmniRoute's per-combo isolation
  flag: no memory, skills, styles or injected context, whole tree);
* every request sends ``x-omniroute-no-memory: true`` and
  ``x-omniroute-compression: off``;
* before every call the route guard (``LocalOnlyRouteResolver`` with
  ``require_secure_combo=True``) re-reads the registry and checks the combo is
  flagged secure and every leaf, fallbacks included, is local. Any deny raises
  :class:`SecureRouteDenied` with a reason code and nothing is sent.

Default: every call through :class:`SecureTransport` is sensitive. Only a call
that passes ``sensitive=False`` explicitly takes the plain local-first path
(NS-ADR-107). In fixture mode (tests, offline dev) nothing leaves the process,
so the guard is skipped.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from backend.inference.local_first import local_model

log = structlog.get_logger(__name__)

SECURE_HEADERS: dict[str, str] = {
    "x-omniroute-no-memory": "true",
    "x-omniroute-compression": "off",
}
SECURE_MODEL_ENV = "NERVE_SECURE_MODEL"
#: secure/planner -> pool/tier1/planner (llama-cpp deepseek-v41), tier 1 only.
DEFAULT_SECURE_MODEL = "secure/planner"

Guard = Callable[[str], Awaitable[Any]]  # alias -> RouteDecision


class SecureRouteDenied(RuntimeError):
    """The route guard denied the secure combo. ``reason`` is a log-safe code."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"secure route denied: {reason}")
        self.reason = reason


def is_secure(model: str | None) -> bool:
    return bool(model) and str(model).strip().startswith("secure/")


def secure_model(requested: str | None, env: dict[str, str] | None = None) -> str:
    """``requested`` when it is a secure combo, else the configured secure default."""
    if is_secure(requested):
        return str(requested).strip()
    raw = ((env if env is not None else os.environ).get(SECURE_MODEL_ENV) or "").strip()
    return raw if is_secure(raw) else DEFAULT_SECURE_MODEL


def build_guard(base_url: str | None, api_key: str | None) -> Guard:
    """The NS-ADR-073 route guard bound to the OmniRoute registry."""
    from backend.inference.omniroute_registry import HttpRegistryTransport
    from backend.mail_security.route_guard import LocalOnlyRouteResolver

    resolver = LocalOnlyRouteResolver(
        HttpRegistryTransport(base_url or "", api_key or ""),
        omniroute_base_url=base_url or "",
        require_secure_combo=True,
    )
    return resolver.resolve


def _run_sync(coro: Awaitable[Any]) -> Any:
    """Run ``coro`` to completion from sync code, inside or outside a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = asyncio.run(coro)  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


class SecureTransport:
    """Wraps an OmniRoute transport: secure combo + no-memory headers + route guard."""

    def __init__(self, inner: Any, caller: str, guard: Guard | None = None) -> None:
        self._inner = inner
        self._caller = caller
        self._guard = (
            guard
            if guard is not None
            else build_guard(getattr(inner, "base_url", None), getattr(inner, "api_key", None))
        )

    def check(self, model: str) -> None:
        """Run the route guard; raises :class:`SecureRouteDenied` on any deny."""
        try:
            decision = _run_sync(self._guard(model))
        except Exception:  # noqa: BLE001 - fail closed, never surface exception text
            log.warning(
                "inference.secure.denied", caller=self._caller, model=model, reason="GUARD_ERROR"
            )
            raise SecureRouteDenied("GUARD_ERROR") from None
        if not getattr(decision, "allowed", False):
            reason = str(getattr(getattr(decision, "reason", None), "value", "DENIED"))
            log.warning("inference.secure.denied", caller=self._caller, model=model, reason=reason)
            raise SecureRouteDenied(reason)

    def generate(
        self,
        model: str,
        prompt: str,
        parameters: dict[str, Any] | None = None,
        timeout: int | None = None,
        *,
        sensitive: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not sensitive:
            chosen = local_model(model, caller=self._caller)
            return self._inner.generate(chosen, prompt, parameters, timeout, **kwargs)
        chosen = secure_model(model)
        if not getattr(self._inner, "fixture_mode", False):
            self.check(chosen)
        headers = {**(kwargs.pop("headers", None) or {}), **SECURE_HEADERS}
        return self._inner.generate(chosen, prompt, parameters, timeout, headers=headers, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def secure_transport(caller: str, inner: Any = None) -> SecureTransport:
    """The one factory every personal-data call site uses."""
    if inner is None:
        from backend.inference.omniroute import OmniRouteTransport

        inner = OmniRouteTransport.from_env()
    return SecureTransport(inner, caller)
