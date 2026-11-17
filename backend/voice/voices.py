"""Curated Kokoro voices. An unknown voice makes OmniRoute answer 500, so the
server validates against this list before any upstream call."""
from __future__ import annotations

CURATED_VOICES: list[dict[str, object]] = [
    {"id": "af_sarah", "label": "Sarah", "lang": "en-US", "gender": "female", "verified": True},
    {"id": "am_adam", "label": "Adam", "lang": "en-US", "gender": "male", "verified": True},
    {"id": "bf_emma", "label": "Emma", "lang": "en-GB", "gender": "female", "verified": True},
    {"id": "af_nicole", "label": "Nicole", "lang": "en-US", "gender": "female", "verified": True},
    {"id": "am_michael", "label": "Michael", "lang": "en-US", "gender": "male", "verified": True},
    {"id": "bm_george", "label": "George", "lang": "en-GB", "gender": "male", "verified": True},
]
DEFAULT_VOICE = "af_sarah"


def is_valid_voice(voice: object) -> bool:
    return isinstance(voice, str) and any(v["id"] == voice for v in CURATED_VOICES)
