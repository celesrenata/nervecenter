"""Voice routes (``/api/voice``): local ArcForge TTS/STT through OmniRoute.

Every route is gated by the default-deny person gate (none is public). The
service (speech client, settings store, mute registry) hangs off
``app.state.voice_service`` so tests can inject fakes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.connectors.redact import redact_message
from backend.voice.sessions import VoiceSessionRegistry
from backend.voice.settings import VoiceSettings, VoiceSettingsStore
from backend.voice.speech import (
    SpeechBadAudio,
    SpeechClient,
    SpeechError,
    SpeechPolicyError,
    SpeechUnavailable,
)
from backend.voice.voices import CURATED_VOICES, DEFAULT_VOICE, is_valid_voice

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/voice", tags=["voice"])

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 1000


@dataclass
class VoiceService:
    speech: Any
    settings: VoiceSettingsStore
    sessions: VoiceSessionRegistry


def _service(request: Request) -> VoiceService:
    svc = getattr(request.app.state, "voice_service", None)
    if svc is None:
        registry = getattr(request.app.state, "repository_registry", None)
        repo = None
        if registry is not None:
            from backend.persistence.repositories.wellness_repo import WellnessRepository

            repo = WellnessRepository(registry.session_factory)
        svc = VoiceService(SpeechClient(), VoiceSettingsStore(repo), VoiceSessionRegistry())
        request.app.state.voice_service = svc
    return svc


def _sub(request: Request) -> str:
    claims = getattr(request.state, "principal", None)
    if isinstance(claims, dict):
        return str(claims.get("sub") or claims.get("sid") or "")
    return ""


def _error(exc: SpeechError) -> HTTPException:
    status = {
        SpeechUnavailable: 503,
        SpeechBadAudio: 422,
        SpeechPolicyError: 500,
    }.get(type(exc), 500)
    log.warning("voice.error", code=exc.code, error=redact_message(exc))
    return HTTPException(status_code=status, detail={"code": exc.code})


class SettingsBody(BaseModel):
    tts_voice: str
    spoken_replies: bool = False


class MuteBody(BaseModel):
    muted: bool


class SpeakBody(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    voice: str | None = None
    format: Literal["mp3", "wav"] = "mp3"
    session_id: str = Field(default="default", min_length=1, max_length=80)


@router.get("/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    return {"data": (await _service(request).settings.get(_sub(request))).model_dump()}


@router.put("/settings")
async def put_settings(body: SettingsBody, request: Request) -> dict[str, Any]:
    if not is_valid_voice(body.tts_voice):
        raise HTTPException(status_code=422, detail={"code": "unknown_voice"})
    saved = await _service(request).settings.set(
        _sub(request),
        VoiceSettings(tts_voice=body.tts_voice, spoken_replies=body.spoken_replies),
    )
    return {"data": saved.model_dump()}


@router.get("/voices")
async def list_voices() -> dict[str, Any]:
    return {"data": CURATED_VOICES, "default": DEFAULT_VOICE}


@router.put("/sessions/{session_id}/mute")
async def put_mute(session_id: str, body: MuteBody, request: Request) -> dict[str, Any]:
    _service(request).sessions.set_muted(_sub(request), session_id, body.muted)
    return {"data": {"session_id": session_id, "muted": body.muted}}


@router.post("/speak")
async def speak(body: SpeakBody, request: Request) -> Response:
    svc = _service(request)
    sub = _sub(request)
    if not svc.sessions.should_synthesize(sub, body.session_id):
        raise HTTPException(status_code=409, detail={"code": "muted"})
    voice = body.voice
    if voice is None:
        voice = (await svc.settings.get(sub)).tts_voice
    elif not is_valid_voice(voice):
        raise HTTPException(status_code=422, detail={"code": "unknown_voice"})
    try:
        audio, ctype = await svc.speech.tts(body.text, voice, body.format)
    except SpeechError as exc:
        raise _error(exc) from None
    return Response(content=audio, media_type=ctype, headers={"Cache-Control": "no-store"})


@router.post("/transcribe")
async def transcribe(request: Request) -> dict[str, Any]:
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not (ctype.startswith("audio/") or ctype == "video/webm"):
        raise HTTPException(status_code=415, detail={"code": "unsupported_media_type"})
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail={"code": "audio_too_large"})
    audio = bytearray()
    async for chunk in request.stream():
        audio.extend(chunk)
        if len(audio) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail={"code": "audio_too_large"})
    if not audio:
        raise HTTPException(status_code=422, detail={"code": "bad_audio"})
    ext = ctype.split("/", 1)[1].split("+")[0] or "webm"
    try:
        result = await _service(request).speech.stt(bytes(audio), f"speech.{ext}", ctype)
    except SpeechError as exc:
        raise _error(exc) from None
    return {"text": result.text, "language": result.language, "duration": result.duration}
