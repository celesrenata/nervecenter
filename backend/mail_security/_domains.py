"""Shared offline domain helpers for NerveShield mail-security (Phase 1).

Small, pure, **network-free** canonicalization helpers shared by the Task 1.4
authentication-evidence detector (:mod:`backend.mail_security.auth_evidence`)
and the Task 1.5 offline inspections (:mod:`backend.mail_security.inspections`)
so the confusable / IDNA and address-domain logic lives in one place instead of
being re-implemented per module (review finding 1 — reuse, don't fork).

Behavior is intentionally identical to the original private helpers in
``auth_evidence.py`` (Task 1.4): extracting these does not change what that
module emits, so its tests pass unchanged. Nothing here performs DNS, URL
resolution or any I/O (R-004.3, R-005.2, T-03, T-05).
"""

from __future__ import annotations

import unicodedata


def domain_of(address: str | None) -> str | None:
    """Return the lowercased domain of an email address, offline and bounded.

    Handles an optional display name and angle-addr (``Name <a@b>``) and bare
    ``a@b``. Returns ``None`` when no ``@domain`` is present. Performs no DNS.
    """
    if not isinstance(address, str):
        return None
    text = address.strip()
    if "<" in text and ">" in text:
        start = text.rfind("<")
        end = text.find(">", start)
        if end > start:
            text = text[start + 1 : end].strip()
    if "@" not in text:
        return None
    domain = text.rsplit("@", 1)[-1].strip().strip("<>").rstrip(".")
    # Strip a trailing route/port or whitespace fragment defensively.
    domain = domain.split()[0] if domain.split() else ""
    return domain.lower() or None


def is_confusable_domain(domain: str) -> bool:
    """Offline heuristic: does the domain look like a homoglyph/IDNA lookalike?

    Minimal and network-free (T-03). Flags a domain when either:

    - it is already in punycode/IDNA ``xn--`` form (an encoded non-ASCII label
      that could be a lookalike of an ASCII brand), or
    - it contains any non-ASCII character — i.e. a Unicode confusable such as
      Cyrillic ``а`` (U+0430) mixed into an otherwise ASCII-looking name, or
    - a compatibility NFKC fold changes it (it carried compatibility /
      confusable characters).
    """
    if not domain:
        return False
    lowered = domain.lower()
    # Punycode / IDNA encoded labels.
    if "xn--" in lowered:
        return True
    # Any non-ASCII code point is a confusable candidate.
    for char in domain:
        if ord(char) > 0x7F:
            return True
        # Mixed-script detection for already-decoded homoglyphs: a letter whose
        # Unicode block is not Latin is suspicious.
        if char.isalpha() and not char.isascii():
            return True
    # Defensive: NFKC-fold and compare; if folding changes the string it had
    # compatibility/confusable characters.
    if unicodedata.normalize("NFKC", domain) != domain:
        return True
    return False


__all__ = ["domain_of", "is_confusable_domain"]
