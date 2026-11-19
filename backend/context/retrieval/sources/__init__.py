"""Source adapters. ``ADAPTERS`` is the only registry; a domain not listed in
``backend.context.retrieval.policy.DOMAINS`` cannot be read."""
from backend.context.retrieval.sources import communications as _c
from backend.context.retrieval.sources import briefing as _br
from backend.context.retrieval.sources import finance as _f
from backend.context.retrieval.sources import health as _h
from backend.context.retrieval.sources import knowledge as _k
from backend.context.retrieval.sources import logs as _l
from backend.context.retrieval.sources import obligations as _o
from backend.context.retrieval.sources import planning as _p
from backend.context.retrieval.sources import routines as _r
from backend.context.retrieval.sources import status as _s

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
    "finance": _f.finance,
    "obligations": _o.obligations,
    "knowledge": _k.knowledge,
    "health": _h.health,
    "routines": _r.routines,
    "briefing": _br.briefing,
    "status": _s.status,
}
