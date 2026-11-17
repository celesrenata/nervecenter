"""Deterministic question parsing for retrieval (no model call)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be because been before being below between both
but by can could did do does doing down during each few for from further had has have having he her here hers
him his how i if in into is it its just me more most my no nor not now of off on once only or other our out
over own same she should so some such than that the their them then there these they this those through to
too under until up very was we were what when where which while who whom why will with would you your yours
tell show find list give please know look see get got say said anything something everything
many much long often coming soon number count total
""".split())

#: question words that make a request a lookup ("is there anything about X?")
_LOOKUP_RE = re.compile(
    r"^\s*(?:hey\W+|ok\W+|so\W+)?(?:can you |could you |please )?"
    r"(?:who|what|when|where|which|how many|find|show|list|search|look up|tell me|do i have|"
    r"did i|have i|is there|are there|was there|any |remind me)",
    re.IGNORECASE,
)

_DOMAIN_WORDS: dict[str, tuple[str, ...]] = {
    "calendar": ("calendar", "appointment", "appointments", "meeting", "meetings", "event",
                 "events", "schedule", "scheduled", "agenda"),
    "mail": ("email", "emails", "mail", "inbox", "thread"),
    "messages": ("text", "texts", "message", "messages", "imessage", "sms", "relay", "chat"),
    "voicemail": ("voicemail", "voicemails", "voice", "callback"),
    "contacts": ("contact", "contacts", "phone", "number", "address"),
    "waiting_on": ("waiting", "owes", "owe"),
    "followups": ("followup", "followups", "follow-up", "follow-ups"),
    "tasks": ("task", "tasks", "todo", "todos"),
    "goals": ("goal", "goals"),
    "incidents": ("incident", "incidents", "outage"),
    "evidence": ("evidence", "record", "records"),
    "bills": ("bill", "bills", "invoice", "invoices", "payment", "payments", "due", "owed"),
    "wellness": ("wellness", "mood", "sleep", "stress", "energy", "journal", "checkin", "check-in"),
    "logs": ("log", "logs", "error", "errors", "crash", "crashed", "pod", "pods", "deploy",
             "deployment", "happened"),
}

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_\-']*")
_QUOTED = re.compile(r"[\"“‘]([^\"”’]{2,80})[\"”’]")
_CAPITALISED = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{2,})\b")


@dataclass(frozen=True)
class Intent:
    question: str
    keywords: tuple[str, ...] = ()
    phrases: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    window: tuple[datetime, datetime] | None = None
    domain_hints: frozenset[str] = frozenset()
    lookup_shaped: bool = False
    wants_logs: bool = False
    #: tokens the user typed that look like a record id ("evidence-0000")
    id_like: tuple[str, ...] = field(default=())
    now: datetime | None = None

    @property
    def has_terms(self) -> bool:
        return bool(self.keywords or self.phrases or self.names)

    @property
    def search_terms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*self.names, *self.phrases, *self.keywords]))


def _stem(token: str) -> str:
    t = token.strip("'-_")
    if t.endswith("'s"):
        t = t[:-2]
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def _day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_window(text: str, now: datetime) -> tuple[datetime, datetime] | None:
    low = text.lower()
    today = _day(now)
    one = timedelta(days=1)
    if "day after tomorrow" in low:
        return today + 2 * one, today + 3 * one
    if "tomorrow" in low:
        return today + one, today + 2 * one
    if "yesterday" in low:
        return today - one, today
    if re.search(r"\btoday\b|\btonight\b", low):
        return today, today + one
    span = re.search(r"\b(next|coming|upcoming|following|last|past|previous)\s+(\d{1,3})\s+(day|week|month)s?\b", low)
    if span:
        length = int(span.group(2)) * {"day": 1, "week": 7, "month": 30}[span.group(3)]
        if span.group(1) in ("last", "past", "previous"):
            return today - length * one, today + one
        return today, today + length * one
    week_start = today - timedelta(days=today.weekday())
    if "next week" in low:
        return week_start + 7 * one, week_start + 14 * one
    if "last week" in low:
        return week_start - 7 * one, week_start
    if "this week" in low:
        return week_start, week_start + 7 * one
    first = today.replace(day=1)
    next_first = (first + 32 * one).replace(day=1)
    if "next month" in low:
        return next_first, (next_first + 32 * one).replace(day=1)
    if "last month" in low:
        return (first - one).replace(day=1), first
    if "this month" in low:
        return first, next_first
    for index, month in enumerate(_MONTHS, start=1):
        if re.search(rf"\b(?:in|during|for|of)\s+{month}\b", low):
            year = today.year if index >= today.month or "last" in low else today.year + 1
            if index > today.month and "last" in low:
                year = today.year - 1
            start = today.replace(year=year, month=index, day=1)
            end = (start + 32 * one).replace(day=1)
            return start, end
    return None


def parse_intent(question: str, now: datetime) -> Intent:
    text = (question or "").strip()
    low = text.lower()
    quoted = tuple(m.group(1).strip().lower() for m in _QUOTED.finditer(text))
    names = tuple(dict.fromkeys(
        n for n in (m.group(1).lower() for m in _CAPITALISED.finditer(text))
        if n not in STOPWORDS and n not in _MONTHS and n not in _WEEKDAYS
    ))
    id_like = tuple(t for t in _TOKEN.findall(low) if re.search(r"[a-z]-[0-9a-f]{4,}$", t))
    tokens = [_stem(t) for t in _TOKEN.findall(low)]
    domain_word_set = {w for words in _DOMAIN_WORDS.values() for w in words}
    keywords: list[str] = []
    for tok in tokens:
        if len(tok) < 3 or tok in STOPWORDS or tok in domain_word_set:
            continue
        if tok in names or tok in _MONTHS or tok in _WEEKDAYS:
            continue
        if tok in ("today", "tomorrow", "yesterday", "tonight", "week", "month", "day", "hour", "year", "next", "last",
                   "this", "future", "past", "upcoming", "recent", "recently", "latest"):
            continue
        keywords.append(tok)
    hints = frozenset(
        domain
        for domain, words in _DOMAIN_WORDS.items()
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words)
    )
    return Intent(
        question=text,
        keywords=tuple(dict.fromkeys(keywords)),
        phrases=tuple(p for p in quoted if p),
        names=names,
        window=parse_window(text, now),
        domain_hints=hints,
        lookup_shaped=bool(_LOOKUP_RE.search(text)),
        wants_logs="logs" in hints,
        id_like=id_like,
        now=now,
    )
