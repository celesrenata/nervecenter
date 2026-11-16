"""OpenAI API engine with project context (NS-ADR-104).

Server side only: NerveCenter calls the OpenAI Chat Completions API with the
key from ``NERVE_OPENAI_KEY`` (env / Secret, never logged or returned) and
injects the selected project's context into the system message. Without a key
it answers honestly with :class:`OpenAINotConfigured`; nothing is faked.

ChatGPT Projects on the $20 plan are not readable through the API, so the
NerveCenter project store is the shared source of truth.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

KEY_ENV = "NERVE_OPENAI_KEY"
MODEL_ENV = "NERVE_OPENAI_MODEL"
BASE_URL_ENV = "NERVE_OPENAI_BASE_URL"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
BASE_SYSTEM = "You are an assistant working for the owner of NerveCenter. Be direct and concise."


class OpenAINotConfigured(RuntimeError):
    """No API key is set."""


class OpenAIError(RuntimeError):
    """The API call failed. Carries a status code and never request headers."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OpenAIEngine:
    def __init__(
        self,
        context_provider: Any = None,
        *,
        env: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
        limiter: Any = None,
    ) -> None:
        e = env if env is not None else os.environ
        self._key = (e.get(KEY_ENV) or "").strip() or None
        self.model = (e.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL
        self._base_url = ((e.get(BASE_URL_ENV) or "").strip() or DEFAULT_BASE_URL).rstrip("/")
        self._context = context_provider  # object with async context_text(slug)
        self._transport = transport
        self._timeout = timeout
        # NS-ADR-107: every call goes through the one app-wide OpenAILimiter.
        self._limiter = limiter

    @property
    def configured(self) -> bool:
        return self._key is not None

    def status(self) -> dict[str, Any]:
        """Sanitized projection: never contains the key."""
        return {"provider": "openai", "model_id": self.model, "key_configured": self.configured}

    async def build_messages(self, prompt: str, project_slug: str | None) -> list[dict[str, str]]:
        system = BASE_SYSTEM
        if project_slug:
            if self._context is None:
                raise OpenAIError("project context is not available")
            system += (
                "\n\nProject context (from NerveCenter):\n"
                + await self._context.context_text(project_slug)
            )
        return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    async def generate(
        self,
        prompt: str,
        project_slug: str | None = None,
        *,
        caller: str = "openai",
        reason: str = "owner_requested",
    ) -> dict[str, Any]:
        """One chat completion. ``reason`` is the NS-ADR-107 escalation code."""
        if not self._key:
            raise OpenAINotConfigured(f"OpenAI is not configured (set {KEY_ENV})")
        messages = await self.build_messages(prompt, project_slug)
        if self._limiter is None:
            return await self._once(messages, project_slug)
        return await self._limiter.run(
            lambda: self._once(messages, project_slug), caller=caller, reason=reason
        )

    async def _once(
        self, messages: list[dict[str, str]], project_slug: str | None
    ) -> dict[str, Any]:
        from backend.inference.openai_limiter import OpenAIThrottled

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={"model": self.model, "messages": messages},
                )
        except httpx.HTTPError as exc:
            raise OpenAIError(f"cannot reach OpenAI ({exc.__class__.__name__})") from None
        if resp.status_code == 429:
            raise OpenAIThrottled(_retry_after(resp.headers.get("retry-after")))
        if resp.status_code >= 400:
            log.warning("openai.call_failed", status=resp.status_code)
            raise OpenAIError(f"OpenAI answered HTTP {resp.status_code}", resp.status_code)
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise OpenAIError("OpenAI returned an unexpected response") from None
        usage = data.get("usage") or {}
        return {
            "content": content,
            "model": data.get("model", self.model),
            "project": project_slug,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        }


def _retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP date)."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    from datetime import UTC, datetime

    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def get_engine(app: Any) -> OpenAIEngine:
    """App-wide engine: project context from the store, the shared limiter."""
    existing = getattr(app.state, "openai_engine", None)
    if existing is not None:
        return existing
    from backend.inference.openai_limiter import get_limiter
    from backend.projects import get_service

    engine = OpenAIEngine(get_service(app), limiter=get_limiter(app))
    app.state.openai_engine = engine
    return engine
