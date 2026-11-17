"""SpeechClient: ArcForge-only allowlist, JSON-error-body retry, no secret logging."""
from __future__ import annotations

import json

import httpx
import pytest

from backend.inference import key_resolver
from backend.voice.speech import (
    SpeechBadAudio,
    SpeechClient,
    SpeechPolicyError,
    SpeechUnavailable,
)

KEY = "sk-test-voice-key-0123456789abcdef"
AUDIO = b"ID3\x04fakeaudio"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(key_resolver.REGULAR_ENV, KEY)
    key_resolver.reset()


def _client(handler, sleeps=None):
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SpeechClient("http://omni.test", client=http, sleep=sleep)


def _audio(_req):
    return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})


async def test_tts_success_request_shape_and_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return _audio(req)

    audio, ctype = await _client(handler).tts("hello there", "am_adam")
    assert audio == AUDIO and ctype == "audio/mpeg"
    body = json.loads(seen[0].content)
    assert body["model"] == "arcforge-tts/kokoro-v1.0/am_adam"
    assert body["voice"] == "am_adam"
    assert body["response_format"] == "mp3"
    assert seen[0].url.path == "/v1/audio/speech"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"


async def test_json_error_body_with_200_is_retried() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={"error": {"message": "boom"}})
        return _audio(req)

    audio, _ = await _client(handler, sleeps).tts("hi", "af_sarah")
    assert audio == AUDIO and len(calls) == 2 and sleeps == [0.4]


async def test_500_three_times_is_unavailable() -> None:
    calls: list[int] = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500, text="nope")

    with pytest.raises(SpeechUnavailable):
        await _client(handler).tts("hi", "af_sarah")
    assert len(calls) == 3


async def test_transport_error_is_retried() -> None:
    calls: list[int] = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("refused")
        return _audio(req)

    audio, _ = await _client(handler).tts("hi", "af_sarah")
    assert audio == AUDIO and len(calls) == 3


async def test_stt_decode_failure_not_retried() -> None:
    calls: list[int] = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": "audio decode failed"})

    with pytest.raises(SpeechBadAudio):
        await _client(handler).stt(b"junk", "a.webm", "audio/webm")
    assert len(calls) == 1


async def test_stt_success_is_multipart_with_default_model() -> None:
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"text": " hi ", "language": "en", "duration": 1.5})

    result = await _client(handler).stt(b"abc", "a.webm", "audio/webm")
    assert (result.text, result.language, result.duration) == ("hi", "en", 1.5)
    assert seen[0].url.path == "/v1/audio/transcriptions"
    assert b"whisper-large-v3" in seen[0].content
    assert seen[0].headers["content-type"].startswith("multipart/form-data")


async def test_allowlist_rejects_foreign_models_without_requests(monkeypatch) -> None:
    calls: list[int] = []

    def handler(req):
        calls.append(1)
        return _audio(req)

    monkeypatch.setenv("NERVE_VOICE_STT_MODEL", "openai/whisper-1")
    with pytest.raises(SpeechPolicyError):
        await _client(handler).stt(b"abc", "a.webm", "audio/webm")
    monkeypatch.setenv("NERVE_VOICE_STT_MODEL", "whisper-1")
    with pytest.raises(SpeechPolicyError):
        await _client(handler).stt(b"abc", "a.webm", "audio/webm")
    from backend.voice import speech

    with pytest.raises(SpeechPolicyError):
        speech.check_tts_model("openai/tts-1")
    with pytest.raises(SpeechPolicyError):
        await _client(handler).tts("hi", "x", "ogg")
    assert calls == []


async def test_key_never_logged(capsys) -> None:
    def handler(req):
        return httpx.Response(500, text=f"echo {KEY}")

    with pytest.raises(SpeechUnavailable):
        await _client(handler).tts("hi", "af_sarah")
    out = capsys.readouterr()
    assert KEY not in out.out and KEY not in out.err


def test_bare_whisper_name_is_refused_because_omniroute_routes_it_to_the_cloud() -> None:
    from backend.voice.speech import DEFAULT_STT_MODEL, check_stt_model

    assert DEFAULT_STT_MODEL == "arcforge-stt/whisper-large-v3"
    assert check_stt_model(DEFAULT_STT_MODEL) == DEFAULT_STT_MODEL
    with pytest.raises(SpeechPolicyError):
        check_stt_model("whisper-large-v3")
