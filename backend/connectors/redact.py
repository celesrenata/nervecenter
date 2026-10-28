"""Shared credential-redaction helper for error text that lands in durable
state, logs, or HTTP responses.

The same redactor is used by:

- :mod:`backend.api.health` (per-check ``str(exc)`` and per-connection
  ``last_error_message``),
- :mod:`backend.calendar.sync` (per-account ``last_error_message`` and
  durable/log error lines),
- :mod:`backend.ingest.supervisor` (worker-build / reconcile error lines and
  the per-worker ``last_error_message`` surfaced through
  :func:`backend.api.health._ingest_check`),
- :mod:`backend.connectors.registry` (probe / import error messages stored on
  the connection row),
- :mod:`backend.ingest.mail_worker` (transport errors; kept as a thin
  back-compat alias to preserve the existing
  :func:`backend.ingest.mail_worker.redact_transport_error` symbol).

Placement rationale: this module is a stdlib-only leaf (``re`` +
``typing``) with no project imports, so it is safe to import from any of the
above planes without risking a cycle — including from the health endpoint,
which only imports :mod:`backend.persistence.connection` today.

The redaction rules mirror the ones the mail worker has always used for its
own durable error text (see the ``_URL_CRED_RE`` / ``_TOKEN_RE`` patterns
below): strip the password half of any ``scheme://user:pass@host`` URL while
keeping the user account name, and mask token-shaped substrings (JWTs,
Google ``ya29.`` tokens, AWS ``AKIA`` access keys, Slack ``xox*`` tokens,
long base64url blobs) so neither half of a credential pair can leak through
an exception message, a durable row, or a log line.

The result is truncated to a caller-supplied ``limit`` (default 256 chars)
so the bounded ``last_error_message`` columns stay within their declared
size.

Hard contract: the input is never assumed to be non-empty and never raises —
``None`` maps to ``""``.
"""

from __future__ import annotations

import re
from typing import Any

#: URL userinfo with embedded credentials: ``scheme://user:pass@host``. The
#: password (between the first ``:`` and the ``@``) is stripped; the user
#: half is kept — it is the account name, not a secret. The optional scheme
#: is preserved verbatim.
_URL_CRED_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)?"
    r"(?P<user>[^:/@\s]+):(?P<password>[^@/\s\"']+)@"
)
#: Token-shaped substrings: JWTs (``eyJ…``), Google ``ya29.`` tokens, AWS
#: ``AKIA`` access keys, Slack ``xox*`` bot/user tokens, and any long
#: base64url blob (Fernet payloads, long bearer/access tokens). An optional
#: labelled prefix (``access_token=`` …) is consumed with the value so an
#: error body does not echo the credential.
_TOKEN_RE = re.compile(
    r"(?i)(?:bearer\s+|"
    r"(?:access_token|refresh_token|client_secret|client_id|"
    r"device_token|session_token|api[_ ]?key|secret[_ ]?key|password|token)"
    r"\s*[:=]\s*)?"
    r"(eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}|"
    r"ya29\.[A-Za-z0-9_-]{10,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"xox[baprs]-[A-Za-z0-9\-]{10,}|"
    r"[A-Za-z0-9_-]{40,})"
)

#: Default truncation bound for durable ``last_error_message`` columns and
#: log lines. The mail worker, health, calendar, and supervisor planes all
#: use this default; callers that need a different bound (e.g. a 500-char
#: durable column) pass it explicitly.
DEFAULT_REDACT_LIMIT = 256


def _strip_url_userinfo(message: str) -> str:
    """Remove the password from any ``scheme://user:pass@host`` URL."""
    return _URL_CRED_RE.sub(
        lambda m: (m.group("scheme") or "") + m.group("user") + "@",
        message,
    )


def _mask_token_shapes(message: str) -> str:
    """Mask token-shaped substrings (JWT / ya29 / AKIA / xox* / long blobs)."""
    return _TOKEN_RE.sub("REDACTED", message)


def redact_message(message: Any, *, limit: int = DEFAULT_REDACT_LIMIT) -> str:
    """Stable, redacted form of a transport / exception message.

    The returned text carries the message's *shape* with every
    credential-bearing fragment removed: URL ``user:pass@`` userinfo is
    stripped to ``user@`` and token-shaped substrings are replaced by
    ``REDACTED`` markers. The result is truncated to ``limit`` characters
    so the durable column and log line stay bounded.

    ``None`` maps to ``""``; the helper never raises.
    """
    if message is None:
        return ""
    text = _strip_url_userinfo(str(message))
    text = _mask_token_shapes(text)
    return text[:limit]


def redact_transport_error(message: Any, *, limit: int = DEFAULT_REDACT_LIMIT) -> str:
    """Back-compat alias for :func:`redact_message` (kept for the mail-worker
    symbol ``redact_transport_error`` so the durable / log call sites in
    :mod:`backend.ingest.mail_worker` can keep importing the familiar name).
    """
    return redact_message(message, limit=limit)
