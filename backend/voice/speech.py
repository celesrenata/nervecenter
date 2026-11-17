"""OmniRoute speech client, locked to the local ArcForge models.

TTS posts to ``/v1/audio/speech`` and STT to ``/v1/audio/transcriptions``.
OmniRoute can answer HTTP 200 with a JSON error body, so a TTS success needs
audio, not just a status. Failures are retried; after the last attempt
:class:`SpeechUnavailable` is raised and no other model is ever tried.
Bodies and keys are never logged.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from backend.inference import key_resolver

log = structlog.get_logger(__name__)

TTS_MODEL_PREFIX = "arcforge-tts/"
TTS_MODEL_BASE = "arcforge-tts/kokoro-v1.0/"
STT_ALLOWED_MODELS = frozenset({"whisper-large-v3", "arcforge-stt/whisper-large-v3"})
DEFAULT_STT_MODEL = "whisper-large-v3"
ATTEMPTS = 3
BACKOFF_SECONDS = (0.4, 1.2)
TTS_FORMATS = ("mp3", "wav")
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def redact_message(value: object) -> str:
    # Imported lazily: backend.connectors imports backend.api at package load.
    from backend.connectors.redact import redact_message as _redact

    return _redact(value)


class SpeechError(Exception):
    """Base class; ``code`` is the stable API error code."""

    code = "speech_error"


class SpeechPolicyError(SpeechError):
    code = "speech_policy"


class SpeechUnavailable(SpeechError):
    code = "speech_unavailable"


class SpeechBadAudio(SpeechError):
    code = "bad_audio"


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    duration: float | None


def _base_url() -> str:
    base = (os.environ.get("NERVE_OMNIROUTE_URL") or "").strip().rstrip("/")
    return base.removesuffix("/v1")


def check_tts_model(model: str) -> str:
    if not model.startswith(TTS_MODEL_PREFIX):
        raise SpeechPolicyError("TTS model must be a local ArcForge model")
    return model


def check_stt_model(model: str) -> str:
    if model not in STT_ALLOWED_MODELS:
        raise SpeechPolicyError("STT model must be a local ArcForge model")
    return model


def _is_audio(resp: httpx.Response) -> bool:
    if resp.status_code != 200 or not resp.content:
        return False
    ctype = resp.headers.get("content-type", "").lower()
    if ctype.startswith("audio/"):
        return True
    if ctype.startswith(("application/json", "text/")):
        return False
    return not resp.content.lstrip().startswith(b"{")


class SpeechClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        stt_model: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._client = client
        self._sleep = sleep
        self._stt_model = stt_model
        # Caps concurrent synthesis so sessions cannot flood the GPU.
        self._tts_slots = asyncio.BoundedSemaphore(2)

    def _url(self, route: str) -> str:
        base = (self._base_url if self._base_url is not None else _base_url()).rstrip("/")
        base = base.removesuffix("/v1")
        if not base:
            raise SpeechUnavailable("OmniRoute URL is not configured")
        return f"{base}{route}"

    def _headers(self) -> dict[str, str]:
        key = key_resolver.regular_key()  # read per call so rotation applies
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def _send(self, **kwargs: Any) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(**kwargs)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            return await client.post(**kwargs)

    async def _attempts(
        self, what: str, make: Callable[[], Awaitable[httpx.Response]],
        ok: Callable[[httpx.Response], bool],
    ) -> httpx.Response:
        for attempt in range(ATTEMPTS):
            reason = "bad_response"
            try:
                resp = await make()
                if resp.status_code in (400, 422):
                    raise SpeechBadAudio(f"{what} request rejected")
                if ok(resp):
                    return resp
                reason = f"http_{resp.status_code}"
            except SpeechError:
                raise
            except httpx.HTTPError as exc:
                reason = type(exc).__name__
                log.warning("voice.upstream_error", what=what, error=redact_message(exc))
            if attempt < ATTEMPTS - 1:
                log.info("voice.retry", what=what, attempt=attempt + 1, reason=reason)
                await self._sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
        raise SpeechUnavailable(f"{what} unavailable after {ATTEMPTS} attempts")

    async def tts(self, text: str, voice: str, fmt: str = "mp3") -> tuple[bytes, str]:
        if fmt not in TTS_FORMATS:
            raise SpeechPolicyError("unsupported audio format")
        model = check_tts_model(TTS_MODEL_BASE + voice)
        url = self._url("/v1/audio/speech")
        body = {"model": model, "input": text, "voice": voice, "response_format": fmt}

        async def make() -> httpx.Response:
            return await self._send(url=url, json=body, headers=self._headers())

        async with self._tts_slots:
            resp = await self._attempts("tts", make, _is_audio)
        default = "audio/mpeg" if fmt == "mp3" else "audio/wav"
        ctype = resp.headers.get("content-type", "")
        return resp.content, ctype if ctype.lower().startswith("audio/") else default

    async def stt(self, audio: bytes, filename: str, content_type: str) -> Transcript:
        model = check_stt_model(
            self._stt_model or os.environ.get("NERVE_VOICE_STT_MODEL") or DEFAULT_STT_MODEL
        )
        url = self._url("/v1/audio/transcriptions")

        def ok(resp: httpx.Response) -> bool:
            if resp.status_code != 200:
                return False
            try:
                data = resp.json()
            except ValueError:
                return False
            return isinstance(data, dict) and "error" not in data and "text" in data

        async def make() -> httpx.Response:
            return await self._send(
                url=url,
                data={"model": model},
                files={"file": (filename, audio, content_type)},
                headers=self._headers(),
            )

        resp = await self._attempts("stt", make, ok)
        data = resp.json()
        duration = data.get("duration")
        return Transcript(
            text=str(data.get("text") or "").strip(),
            language=data.get("language") if isinstance(data.get("language"), str) else None,
            duration=float(duration) if isinstance(duration, (int, float)) else None,
        )
