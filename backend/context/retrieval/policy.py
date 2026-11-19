"""What Ask may read, and how text is cleaned before a model sees it.

The allowlist below is the only way data reaches Ask. Every excluded store has
no adapter (a test checks this), and every snippet is secret-scrubbed and
clipped before it leaves this package.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from backend.connectors.redact import redact_message
from backend.observability.log_buffer import _scrub

SNIPPET_CHARS = 300
TITLE_CHARS = 140

#: domain -> (id prefix, context section kind, screen the record links to)
DOMAINS: dict[str, tuple[str, str, str]] = {
    "calendar": ("cal", "calendar", "/calendar"),
    "mail": ("mail", "mail", "/email"),
    "messages": ("msg", "messages", "/messages"),
    "voicemail": ("vm", "voicemail", "/voicemail"),
    "contacts": ("con", "contacts", "/contacts"),
    "waiting_on": ("wait", "personal", "/waiting-on"),
    "followups": ("fu", "personal", "/waiting-on"),
    "tasks": ("task", "tasks", "/tasks"),
    "goals": ("goal", "goals", "/goals"),
    "incidents": ("inc", "evidence", "/incidents"),
    "evidence": ("evd", "evidence", "/timeline"),
    "bills": ("bill", "bills", "/bills"),
    "wellness": ("well", "personal", "/wellness"),
    "logs": ("log", "personal", "/logs"),
    "finance": ("fin", "bills", "/finance"),
    "obligations": ("obl", "personal", "/waiting-on"),
    "knowledge": ("know", "notes", "/projects"),
    "health": ("hlth", "personal", "/health"),
    "routines": ("rtn", "personal", "/routines"),
    "briefing": ("brf", "personal", "/briefing"),
    "status": ("stat", "evidence", "/systems"),
}

#: Stores Ask never reads (owner decision). No adapter may be registered for these.
EXCLUDED_STORES: tuple[str, ...] = (
    # credentials
    "secrets", "connections", "omniroute_key_audit", "omniroute_keys",
    "personal_api_tokens", "inference_providers",
    # auth and sessions
    "auth_sessions", "auth_refresh_sessions", "auth_devices", "pairing_challenges",
    "approvals", "nc_principals", "nc_policies", "escalation_grants",
    # security internals (the Email screen's own view is the only mail source)
    "mail_security", "mail_evidence", "mail_intel", "mail_thread_ledger",
    "mail_campaign", "mail_semantic_observations", "mail_expectedness",
    # plumbing
    "ingest_cursors", "import_jobs", "relay_seen_messages", "nc_connector_instances",
    "infra_connectors", "agent_runtime", "app_logs", "object_store", "raw_mime",
    # mental-health forms: answers stay out (owner answer A)
    "mh_forms", "mental_health",
    # finance ledger lines stay out; summaries only (the finance adapter reads accounts and monthly spend, never ledger lines)
    "finance_ledger",
)


def prefix_for(domain: str) -> str:
    return DOMAINS[domain][0]


def kind_for(domain: str) -> str:
    return DOMAINS[domain][1]


def link_for(domain: str) -> str:
    return DOMAINS[domain][2]


def record_id(domain: str, source_ref: str) -> str:
    """Stable, citation-safe id for a record: same source -> same id."""
    digest = hashlib.sha1(f"{domain}:{source_ref}".encode()).hexdigest()[:10]  # noqa: S324 - id, not security
    return f"{prefix_for(domain)}-{digest}"


def scrub(text: object, limit: int = SNIPPET_CHARS) -> str:
    """Single-line, secret-scrubbed, clipped text."""
    raw = " ".join(str(text if text is not None else "").split())
    cleaned = _scrub(redact_message(raw, limit=max(limit * 3, 600)))
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def when_iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


_CITE_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]*$")


def citation_safe(value: str) -> bool:
    return bool(_CITE_OK.match(value))
