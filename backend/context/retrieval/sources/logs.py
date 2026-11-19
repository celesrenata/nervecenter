"""Optional log source: the read-only Elasticsearch log server, through
``NerveTools.logs_search``. Off unless ``NERVE_LOGS_ES_URL`` is set, and only
for questions that use log words. The app's own log buffer is never read."""
from __future__ import annotations

import os
from typing import Any

from backend.agents.tools.nerve import ES_URL_ENV, NerveToolError, NerveTools
from backend.context.retrieval.intent import Intent
from backend.context.retrieval.sources._util import make_all
from backend.context.retrieval.types import Record


async def logs(state: Any, intent: Intent, limit: int, *, es_transport: Any = None) -> list[Record] | None:
    if not intent.wants_logs or not os.environ.get(ES_URL_ENV, "").strip():
        return None
    terms = [t for t in intent.search_terms if t.isalnum()]
    if not terms:  # "any recent errors?": only log words, so search for them
        terms = [w for w in ("error", "crash", "fail") if w in intent.question.lower()]
    if not terms:
        return None
    app = type("App", (), {"state": state})()
    try:
        result = await NerveTools(app, es_transport=es_transport).call(
            None, "logs_search", {"query": " ".join(terms[:4]), "since_minutes": 1440, "limit": 30}
        )
    except NerveToolError as exc:
        raise RuntimeError(exc.reason) from None  # counted as skipped by the service
    return await make_all([
        ("logs", f"{r.get('t')}:{r.get('pod')}:{r.get('msg')}", f"{r.get('ns')}/{r.get('pod')}",
         r.get("msg"), r.get("t"))
        for r in result.get("items", [])
    ])
