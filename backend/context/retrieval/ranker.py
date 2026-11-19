"""Deterministic ranking: term overlap, name/phrase boosts, window and recency."""
from __future__ import annotations

from datetime import UTC, datetime

from backend.context.retrieval.intent import AGGREGATE, DOMAIN_LIST, OVERVIEW, Intent, _stem
from backend.context.retrieval.types import Record

MAX_RECORDS = 12
CHAR_BUDGET = 3600


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _words(text: str) -> set[str]:
    import re

    return {_stem(t) for t in re.findall(r"[a-z0-9][a-z0-9_\-']*", text.lower())}


def score_record(record: Record, intent: Intent, now: datetime) -> float:
    """0 means "does not match"; a positive score is relevance."""
    hay = f"{record.title} {record.snippet}".lower()
    words = _words(hay)
    score = 0.0
    hits = 0
    for kw in intent.keywords:
        if kw in words or (len(kw) > 4 and kw in hay):
            score += 1.0
            hits += 1
    for name in intent.names:
        if name in words:
            score += 3.0
            hits += 1
    for phrase in intent.phrases:
        if phrase in hay:
            score += 3.0
            hits += 1
    in_window = False
    ts = _aware(record.ts)
    if intent.window and ts is not None:
        start, end = _aware(intent.window[0]), _aware(intent.window[1])
        in_window = start <= ts < end  # type: ignore[operator]
        if in_window:
            score += 2.0
    # Aggregate, domain-list and overview records are selected by domain (or by
    # aggregate.py), not by keyword hit, so they bypass the keyword gate.
    selected_by_domain = intent.kind in (AGGREGATE, DOMAIN_LIST, OVERVIEW)
    if intent.has_terms and not selected_by_domain:
        if hits == 0:
            # An in-window calendar event in a calendar-domain windowed question stays a weak
            # candidate: the window and domain select it, not a filler word or an attendee-only name.
            if not (in_window and record.domain == "calendar" and "calendar" in intent.domain_hints):
                return 0.0
            score += 0.25
    elif selected_by_domain and hits > 0:
        pass  # matched terms still count
    elif intent.window is not None:
        if not in_window and not (selected_by_domain and record.domain in intent.domain_hints):
            return 0.0
        if not in_window:
            score = 0.5
    elif record.domain in intent.domain_hints:
        score = 0.5  # "my voicemails": list the recent ones
    else:
        return 0.0
    if record.domain in intent.domain_hints:
        score += 1.0
    if ts is not None:
        age_days = abs((_aware(now) - ts).total_seconds()) / 86400  # type: ignore[operator]
        score += max(0.0, 1.0 - min(age_days, 365.0) / 365.0) * 0.5
    return round(score, 4)


def rank(records: list[Record], intent: Intent, now: datetime) -> list[Record]:
    scored = []
    for rec in records:
        s = score_record(rec, intent, now)
        if s > 0:
            scored.append(rec.with_score(s))
    scored.sort(key=lambda r: (-r.score, r.domain, r.id))
    out: list[Record] = []
    used = 0
    for rec in scored:
        size = len(rec.title) + len(rec.snippet) + 60
        if out and (len(out) >= MAX_RECORDS or used + size > CHAR_BUDGET):
            break
        out.append(rec)
        used += size
    return out
