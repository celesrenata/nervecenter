"""/api/voice routes with a fake SpeechClient and an injected clock."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.endpoints import voice
from backend.voice.sessions import VoiceSessionRegistry
from backend.voice.settings import VoiceSettingsStore
from backend.voice.speech import SpeechUnavailable, Transcript


class FakeSpeech:
    def __init__(self) -> None:
        self.tts_calls: list[tuple[str, str, str]] = []
        self.fail = False

    async def tts(self, text, voice_id, fmt="mp3"):
        self.tts_calls.append((text, voice_id, fmt))
        if self.fail:
            raise SpeechUnavailable("down")
        return b"AUDIO", "audio/mpeg"

    async def stt(self, audio, filename, content_type):
        return Transcript(text="hello", language="en", duration=1.0)


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env():
    clock = Clock()
    speech = FakeSpeech()
    app = FastAPI()

    @app.middleware("http")
    async def _principal(request, call_next):
        request.state.principal = {"sub": request.headers.get("x-sub", "alice")}
        return await call_next(request)

    app.include_router(voice.router)
    app.state.voice_service = voice.VoiceService(
        speech, VoiceSettingsStore(), VoiceSessionRegistry(clock)
    )
    return SimpleNamespace(client=TestClient(app), speech=speech, clock=clock)


def test_settings_round_trip_isolated_per_sub(env) -> None:
    c = env.client
    assert c.get("/api/voice/settings").json()["data"] == {
        "tts_voice": "af_sarah", "spoken_replies": False,
    }
    r = c.put("/api/voice/settings", json={"tts_voice": "am_adam", "spoken_replies": True})
    assert r.status_code == 200
    assert c.get("/api/voice/settings").json()["data"]["tts_voice"] == "am_adam"
    other = c.get("/api/voice/settings", headers={"x-sub": "bob"}).json()["data"]
    assert other == {"tts_voice": "af_sarah", "spoken_replies": False}


def test_unknown_voice_is_422(env) -> None:
    r = env.client.put("/api/voice/settings", json={"tts_voice": "zz_nope"})
    assert r.status_code == 422
    assert env.client.post("/api/voice/speak", json={"text": "hi", "voice": "zz"}).status_code == 422
    assert env.speech.tts_calls == []


def test_voices_list(env) -> None:
    body = env.client.get("/api/voice/voices").json()
    ids = {v["id"] for v in body["data"]}
    assert {"af_sarah", "am_adam", "bf_emma"} <= ids and body["default"] == "af_sarah"


def test_speak_uses_saved_voice(env) -> None:
    env.client.put("/api/voice/settings", json={"tts_voice": "bf_emma"})
    r = env.client.post("/api/voice/speak", json={"text": "hi"})
    assert r.status_code == 200 and r.content == b"AUDIO"
    assert r.headers["content-type"] == "audio/mpeg"
    assert env.speech.tts_calls == [("hi", "bf_emma", "mp3")]


def test_speak_unavailable_is_503_and_text_limit(env) -> None:
    env.speech.fail = True
    r = env.client.post("/api/voice/speak", json={"text": "hi"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "speech_unavailable"
    assert env.client.post("/api/voice/speak", json={"text": "x" * 1001}).status_code == 422


def test_mute_gate_with_grace(env) -> None:
    c, clock = env.client, env.clock
    body = {"text": "hi", "session_id": "s1"}
    c.put("/api/voice/sessions/s1/mute", json={"muted": True})
    clock.now += 10
    assert c.post("/api/voice/speak", json=body).status_code == 200
    clock.now += 21  # 31 s since mute
    before = len(env.speech.tts_calls)
    r = c.post("/api/voice/speak", json=body)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "muted"
    assert len(env.speech.tts_calls) == before
    c.put("/api/voice/sessions/s1/mute", json={"muted": False})
    assert c.post("/api/voice/speak", json=body).status_code == 200


def test_transcribe_validation_and_success(env) -> None:
    c = env.client
    assert c.post("/api/voice/transcribe", content=b"x",
                  headers={"content-type": "text/plain"}).status_code == 415
    big = b"0" * (10 * 1024 * 1024 + 1)
    assert c.post("/api/voice/transcribe", content=big,
                  headers={"content-type": "audio/webm"}).status_code == 413
    r = c.post("/api/voice/transcribe", content=b"abc",
               headers={"content-type": "audio/webm;codecs=opus"})
    assert r.status_code == 200
    assert r.json() == {"text": "hello", "language": "en", "duration": 1.0}
