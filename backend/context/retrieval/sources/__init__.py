"""Source adapters. ``ADAPTERS`` is the only registry; a domain not listed in
``backend.context.retrieval.policy.DOMAINS`` cannot be read."""
from backend.context.retrieval.sources import communications as _c
from backend.context.retrieval.sources import logs as _l
from backend.context.retrieval.sources import planning as _p

ADAPTERS = {
    "calendar": _c.calendar,
    "mail": _c.mail,
    "messages": _c.messages,
    "voicemail": _c.voicemail,
    "contacts": _c.contacts,
    "waiting_on": _c.waiting_on,
    "followups": _p.followups,
    "tasks": _p.tasks,
    "goals": _p.goals,
    "incidents": _p.incidents,
    "evidence": _p.evidence,
    "bills": _p.bills,
    "wellness": _p.wellness,
    "logs": _l.logs,
}
