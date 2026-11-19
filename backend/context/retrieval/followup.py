"""Resolve a follow-up question ("when is it due?", "and the one after that") against recent turns.

Deterministic, no model call. Only record ids and the previous *user* question are taken from the
history. Stored assistant text is never used as evidence: the carried ids are re-read through the
adapters by ``retrieve`` and pass the same policy, scrub and exclusion steps as any other record.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backend.context.retrieval.intent import FOLLOWUP, Intent

_ID_RE = re.compile(r"^[a-z][a-z_]*-[0-9a-f]{10}$")
_AFTER_RE = re.compile(r"\bthe\s+one\s+after(?:\s+that)?\b|\bnext\s+one\b|\bthe\s+following\s+one\b")
MAX_TURNS = 4
MAX_CARRIED = 12


@dataclass(frozen=True)
class ResolvedQuestion:
    #: the earlier user question to re-run through the adapters
    question: str
    #: citation-safe record ids the previous answer cited, in order
    carried_ids: tuple[str, ...]
    #: 0 = the carried records themselves, 1 = the one after them in the previous ranked selection
    shift: int
    #: turns before the previous user question (for a follow-up of a follow-up)
    earlier: tuple[dict[str, Any], ...] = ()


def _ids(turn: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for key, field in (("citations", "record_id"), ("records", "id")):
        for item in turn.get(key) or []:
            value = item.get(field) if isinstance(item, Mapping) else item
            if isinstance(value, str) and _ID_RE.match(value) and value not in found:
                found.append(value)
    return found[:MAX_CARRIED]


def resolve_followup(
    question: str, turns: Sequence[Mapping[str, Any]] | None, intent: Intent | None = None,
) -> ResolvedQuestion | None:
    """The previous subject for ``question``, or ``None`` when it is standalone (no history, not a
    follow-up, no earlier user question, or nothing was cited). An explicit new domain or name makes
    ``parse_intent`` classify the question as a lookup, so it overrides the carried subject."""
    if not turns or (intent is not None and intent.kind != FOLLOWUP):
        return None
    history = [dict(t) for t in turns[-MAX_TURNS:] if isinstance(t, Mapping)]
    for idx in range(len(history) - 1, -1, -1):
        turn = history[idx]
        if turn.get("role") != "assistant":
            continue
        carried = _ids(turn)
        if not carried:
            continue
        for j in range(idx - 1, -1, -1):
            prior = history[j]
            text = str(prior.get("content") or "").strip()
            if prior.get("role") == "user" and text:
                shift = 1 if _AFTER_RE.search(question.lower()) else 0
                return ResolvedQuestion(text, tuple(carried), shift, tuple(history[:j]))
        return None
    return None
