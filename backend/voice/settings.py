"""Per-account voice settings in the durable ``preferences`` table."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.voice.voices import DEFAULT_VOICE, is_valid_voice


class VoiceSettings(BaseModel):
    tts_voice: str = DEFAULT_VOICE
    spoken_replies: bool = False


def _key(sub: str) -> str:
    return f"voice:{sub}"


class VoiceSettingsStore:
    """``repo`` needs async get_preference/set_preference (WellnessRepository);
    without one an in-memory dict is used."""

    def __init__(self, repo: Any | None = None) -> None:
        self._repo = repo
        self._memory: dict[str, dict[str, Any]] = {}

    async def get(self, sub: str) -> VoiceSettings:
        raw = (
            await self._repo.get_preference(_key(sub))
            if self._repo is not None
            else self._memory.get(_key(sub))
        )
        if not isinstance(raw, dict):
            return VoiceSettings()
        voice = raw.get("tts_voice")
        return VoiceSettings(
            tts_voice=voice if is_valid_voice(voice) else DEFAULT_VOICE,
            spoken_replies=bool(raw.get("spoken_replies", False)),
        )

    async def set(self, sub: str, value: VoiceSettings) -> VoiceSettings:
        data = value.model_dump()
        if self._repo is not None:
            await self._repo.set_preference(_key(sub), data)
        else:
            self._memory[_key(sub)] = data
        return value
