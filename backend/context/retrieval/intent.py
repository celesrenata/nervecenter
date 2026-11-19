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
                 "events", "schedule", "scheduled", "agenda", "free", "busy"),
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
    "bills": ("bill", "bills", "invoice", "invoices", "payment", "payments", "due", "owed", "pay", "paid"),
    "wellness": ("wellness", "mood", "sleep", "stress", "energy", "journal", "checkin", "check-in"),
    "finance": ("spent", "spend", "spending", "expense", "expenses", "finance", "finances"),
    "obligations": ("obligation", "obligations", "commitment", "commitments", "promised", "promise",
                    "promises"),
    "knowledge": ("note", "notes", "decision", "decisions", "document", "documents", "knowledge",
                  "claim", "claims"),
    "health": ("health", "sleep", "slept", "steps", "heartrate", "workout", "workouts", "mood", "energy"),
    "routines": ("routine", "routines", "habit", "habits", "checkin", "check-in"),
    "briefing": ("briefing", "digest", "summary", "summarize", "summarise", "overview"),
    "security": ("security", "nervshield", "nerveshield", "quarantine", "quarantined", "held",
                 "phishing", "malware", "threat", "threats"),
    "systems": ("system", "systems", "server", "servers", "kubernetes", "k8s", "cluster", "infra",
                "infrastructure"),
    "logs": ("log", "logs", "error", "errors", "crash", "crashed", "pod", "pods", "deploy",
             "deployment", "happened"),
}

#: multi-word phrases that name one or more domains
_DOMAIN_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("what do i owe", ("finance", "obligations", "bills")),
    ("what i owe", ("finance", "obligations", "bills")),
    ("i owe", ("finance", "obligations", "bills")),
    ("is anything wrong", ("incidents", "security", "systems")),
    ("anything wrong", ("incidents", "security", "systems")),
    ("what needs my attention", ("briefing",)),
    ("need my attention", ("briefing",)),
    ("need to know", ("briefing",)),
    ("morning routine", ("routines",)),
    ("evening routine", ("routines",)),
    ("how did i sleep", ("health",)),
    ("waiting on me", ("obligations",)),
)
#: hints served by the ``status`` adapter (security and system health have no domain of their own)
_STATUS_HINTS = frozenset({"security", "systems"})

#: question kinds
LOOKUP, AGGREGATE, DOMAIN_LIST, OVERVIEW, CONVERSATIONAL, FOLLOWUP = (
    "lookup", "aggregate", "domain_list", "overview", "conversational", "followup",
)

_NEXT_NOT_WINDOW = r"\bnext\b(?!\s+(?:week|month|year|\d|monday|tuesday|wednesday|thursday|friday|saturday|sunday))"
_NUMBER_WORDS = {"three": 3, "five": 5, "ten": 10}
#: ordered: the first matching row wins. (pattern, kind, field)
_OPERATOR_TABLE: tuple[tuple[str, str, str | None], ...] = (
    (r"\btop\s+(\d{1,3}|three|five|ten)\b", "top_n", "amount"),
    (r"\bhighest[\s-]+priority\b|\bmost\s+important\b", "max", "priority"),
    (r"\bmost\s+expensive\b|\bbiggest\b|\blargest\b|\bhighest\b|\bgreatest\b|\bpriciest\b", "max", "amount"),
    (r"\bleast\s+expensive\b|\bcheapest\b|\bsmallest\b|\blowest\b", "min", "amount"),
    (r"\boverdue\b|\bpast\s+due\b|\bbehind\b", "overdue", "due"),
    (r"\bdue\s+(?:this\s+week|soon)\b|\bcoming\s+due\b", "due_soon", "due"),
    (r"\baverage\b|\bavg\b|\bon\s+average\b", "avg", "amount"),
    (r"\bhow\s+many\b|\bnumber\s+of\b|\bcount\b", "count", None),
    (r"\bhow\s+much\b|\btotal\b", "sum", "amount"),
    (r"\bearliest\b|\boldest\b", "earliest", "ts"),
    (r"\blatest\b|\bmost\s+recent\b|\bnewest\b", "latest", "ts"),
    (_NEXT_NOT_WINDOW, "next", "ts"),
)
_OPERATOR_RES = tuple((re.compile(p, re.IGNORECASE), k, f) for p, k, f in _OPERATOR_TABLE)
#: stemmed operator words never count as match terms
_OPERATOR_WORDS = frozenset("""
biggest largest expensive highest cheapest smallest lowest priciest greatest average avg overdue
earliest oldest latest newest soon top cheap pricey behind
""".split())

_CONVERSATIONAL_RE = re.compile(
    r"^(?:hello|hi|hey|howdy|good (?:morning|afternoon|evening)|thanks|thank you|thanks a lot|"
    r"thank you very much|help|help me|what can you do|what do you do|who are you|what are you|"
    r"how can you help(?: me)?|what can i ask(?: you)?|how do you work)(?: there| nervcenter)?$"
)
_FOLLOWUP_STRONG_RE = re.compile(
    r"\b(?:the one after(?: that)?|that one|this one|what about|and then|how about|"
    r"when is it|when(?:'s| is) that|how much is it|who is it)\b"
)
_FOLLOWUP_WEAK_RE = re.compile(r"\b(?:it|them|that|those|they|its)\b")
_FOLLOWUP_FILLER = frozenset({"one", "ones"})
#: leftover words that carry no match meaning once a domain is known ("this" stems to "thi")
_DOMAIN_FILLER = frozenset({"thi", "right", "wrong", "need", "people", "down"})
_CALENDAR_VERBS = frozenset({"see", "meet", "visit"})
_CALENDAR_VERB_RE = re.compile(r"\b(?:see|meet|visit)\b")
#: "planned" stays a match keyword (ranks term hits first); it only adds the calendar hint
_PLANNED_RE = re.compile(r"\bplanned\b|\bon my schedule\b")
_STATUS_RE = (
    (re.compile(r"\bunpaid\b|\bowe\b|\bowed\b|\boutstanding\b|\boverdue\b|\bpast due\b|\bdue\b"), "unpaid"),
    (re.compile(r"\bpaid\b"), "paid"),
)
_PAYEE_RE = re.compile(r"\b(?:to|for|at)\s+(?:the\s+)?([A-Z][A-Za-z0-9&'\-]{2,}(?:\s+[A-Z][A-Za-z0-9&'\-]+)*)")

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_\-']*")
_QUOTED = re.compile(r"[\"“‘]([^\"”’]{2,80})[\"”’]")
_CAPITALISED = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{2,})\b")


@dataclass(frozen=True)
class Operator:
    """A selection or computation the question asks for ("biggest", "total", "overdue")."""

    kind: str  # max, min, top_n, sum, count, avg, earliest, latest, next, overdue, due_soon, list
    n: int | None = None
    field: str | None = None  # amount, due, ts, priority
    #: 0 = the best match; 1 = "the one after that" (single-selection operators only)
    offset: int = 0


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
    operator: Operator | None = None
    #: status, payee, name
    filters: dict[str, str] = field(default_factory=dict, hash=False)
    kind: str = LOOKUP

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
    if len(t) > 4 and t.endswith("xes"):  # taxes -> tax, boxes -> box
        return t[:-2]
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


def parse_operator(text: str) -> Operator | None:
    for pattern, kind, fld in _OPERATOR_RES:
        m = pattern.search(text)
        if not m:
            continue
        n: int | None = None
        if kind == "top_n":
            raw = m.group(1).lower()
            n = _NUMBER_WORDS.get(raw) or int(raw)
        return Operator(kind=kind, n=n, field=fld)
    return None


def _filters(text: str, low: str, names: tuple[str, ...], operator: Operator | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if operator is not None and operator.kind in ("overdue", "due_soon"):
        out["status"] = "unpaid"
    else:
        for pattern, status in _STATUS_RE:
            if pattern.search(low):
                out["status"] = status
                break
    payee = _PAYEE_RE.search(text)
    if payee:
        out["payee"] = payee.group(1).strip().lower()
    if names:
        out["name"] = names[0]
    return out


def _classify(
    low: str, tokens: list[str], keywords: list[str], names: tuple[str, ...], quoted: tuple[str, ...],
    hints: frozenset[str], operator: Operator | None, has_window: bool,
) -> str:
    plain = re.sub(r"[^a-z0-9' ]+", " ", low)
    plain = " ".join(plain.split())
    if not hints and not names and _CONVERSATIONAL_RE.match(plain):
        return CONVERSATIONAL
    if operator is not None:
        return AGGREGATE
    leftover = [k for k in keywords if k not in _FOLLOWUP_FILLER]
    if len(tokens) <= 8 and not names and not quoted and not leftover:
        if _FOLLOWUP_STRONG_RE.search(low) or (not hints and _FOLLOWUP_WEAK_RE.search(low)):
            return FOLLOWUP
    if not keywords and not names and not quoted and hints:
        if "briefing" in hints:
            return OVERVIEW
        if not has_window:
            return DOMAIN_LIST
    return LOOKUP


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
        if tok in _OPERATOR_WORDS:
            continue
        if tok in ("today", "tomorrow", "yesterday", "tonight", "week", "month", "day", "hour", "year", "next", "last",
                   "this", "future", "past", "upcoming", "recent", "recently", "latest", "afternoon"):
            continue
        keywords.append(tok)
    hints = frozenset(
        domain
        for domain, words in _DOMAIN_WORDS.items()
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words)
    )
    hints = hints | frozenset(d for phrase, doms in _DOMAIN_PHRASES if phrase in low for d in doms)
    if hints & _STATUS_HINTS:
        hints = hints | {"status"}
    operator = parse_operator(text)
    window = parse_window(text, now)
    # "when do I next see/meet Pam?" and "anything planned next month?" name a calendar lookup
    # without a calendar word; only treat them so with a next/earliest/latest operator or a window.
    if "calendar" not in hints:
        verb = _CALENDAR_VERB_RE.search(low) if (window is not None or (
            operator is not None and operator.kind in ("next", "earliest", "latest"))) else None
        if verb is None and window is not None and _PLANNED_RE.search(low):
            verb = True
        if verb:
            hints = hints | {"calendar"}
            keywords = [k for k in keywords if k not in _CALENDAR_VERBS]
    if hints and keywords and all(k in _DOMAIN_FILLER for k in keywords):
        keywords = []
    kind = _classify(low, tokens, keywords, names, tuple(p for p in quoted if p), hints, operator,
                     window is not None)
    if kind == CONVERSATIONAL:
        keywords = []
    return Intent(
        question=text,
        keywords=tuple(dict.fromkeys(keywords)),
        phrases=tuple(p for p in quoted if p),
        names=names,
        window=window,
        domain_hints=hints,
        lookup_shaped=bool(_LOOKUP_RE.search(text)),
        wants_logs="logs" in hints,
        id_like=id_like,
        now=now,
        operator=operator,
        filters=_filters(text, low, names, operator),
        kind=kind,
    )
