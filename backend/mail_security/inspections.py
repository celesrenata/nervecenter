"""Pure offline sender/domain/URL/HTML inspections for NerveShield (Task 1.5).

This module turns the Task 1.3 parser's transient :class:`ParsedEvidence`
(plus an offline view of the sender addresses) into typed, deterministic
:class:`~backend.mail_security.models.SecurityFinding` objects. Every check is
**lexical, structural or Unicode-level**: it performs no network I/O, no DNS,
no URL resolution, no remote-image fetch and never expands a shortened link
(R-005.2, T-05). Analysis happens *instead of* — never before — any network
resolution, which Phase 1 does not perform at all.

Design anchors (`.kiro/specs/nerve-shield/design.md` §5.2 "Offline inspections";
requirements R-005, R-006; threats T-03/T-05/T-10/T-11):

- **Address/domain.** Unicode/IDNA confusable detection (``xn--`` punycode and
  non-ASCII / mixed-script code points), all-ASCII typosquat/lookalike
  detection (digit-for-letter substitution, brand + hyphenated suffix, ``co``
  vs ``com`` truncation, near-miss edit distance to a small known-brand list),
  and Reply-To / Return-Path vs From mismatch. No DNS.
- **URL.** Percent-encoding / punycode normalization, registrable-domain
  (eTLD+1) parsing with a *minimal built-in* public-suffix table (no new
  third-party dependency — see the coverage note), visible-text vs actual-href
  mismatch, subdomain deception, confusables and lexical shortened-link
  recognition against an offline host list. Links are **never clicked**.
- **HTML.** Remote-resource references (external ``<img>`` and external link
  targets) and basic tracking-pixel heuristics, plus hidden / zero-size /
  offscreen text. Active content is **never rendered**.
- **Phrases.** Social-engineering / account-takeover / payment / secret-
  harvesting / AI-prompt-injection lexical indicators. No single regex is
  treated as proof; each is an inert *finding* with a confidence expressed as a
  typed ``status`` (OBSERVED/SUSPECTED), never a fabricated probability. A
  prompt-injection phrase is captured as inert data (I-01/R-022.4) — it is a
  finding, never an instruction.

Every finding carries a stable ``rule_id``, ``category``, ``severity``,
``status`` (the confidence *type*), ``detector_version`` and a safe upper-snake
``explanation_code``. No finding field carries raw body text, raw URLs or
addresses: attacker-controlled strings are reduced to bounded safe codes and
opaque refs (design §9, I-05, R-022.4).

**Orthogonality (design §16).** These are SECURITY findings only. This module
imports nothing from ``backend.communications.detection.relevance`` and encodes
no VIP / importance / priority semantics. It emits evidence; the Task 1.6
policy decides disposition.

No code here performs any I/O other than reading the in-memory evidence it is
handed; there is no import of ``socket``, ``urllib``, ``requests``, ``http`` or
``dns`` and every public function is a pure function of its inputs.
"""

from __future__ import annotations

import re

from backend.mail_security._domains import domain_of as _domain_of
from backend.mail_security._domains import is_confusable_domain as _is_confusable_domain
from backend.mail_security.auth_evidence import SenderAddresses
from backend.mail_security.models import (
    Code,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    SecurityFinding,
)
from backend.mail_security.parser import ParsedEvidence

# -- detector identity -------------------------------------------------------

#: Stable version recorded on every emitted finding so deterministic evidence
#: is reproducible / auditable (R-002, R-005.5).
INSPECTIONS_VERSION = "offline-inspections@1"

# -- stable rule ids (grouping keys for dashboards / policy) -----------------

_RULE_DISPLAY_TARGET_MISMATCH = "URL_DISPLAY_TARGET_MISMATCH"
_RULE_CONFUSABLE_IDNA = "DOMAIN_CONFUSABLE_IDNA"
_RULE_TYPOSQUAT_ASCII = "DOMAIN_TYPOSQUAT_ASCII"
_RULE_SUBDOMAIN_DECEPTION = "URL_SUBDOMAIN_DECEPTION"
_RULE_SHORTENED_LINK = "URL_SHORTENED_LINK"
_RULE_REMOTE_IMAGE_PIXEL = "REMOTE_IMAGE_TRACKING_PIXEL"
_RULE_REMOTE_IMAGE = "REMOTE_IMAGE_RESOURCE"
_RULE_LINK_REMOTE_RESOURCE = "LINK_REMOTE_RESOURCE"
_RULE_HTML_HIDDEN_TEXT = "HTML_HIDDEN_TEXT"
_RULE_REPLY_TO_MISMATCH = "REPLY_TO_MISMATCH"
_RULE_RETURN_PATH_MISMATCH = "RETURN_PATH_MISMATCH"
_RULE_PROMPT_INJECTION = "PROMPT_INJECTION_INDICATOR"
_RULE_SOCIAL_ENGINEERING = "SOCIAL_ENGINEERING_PRESSURE"


# =============================================================================
# offline reference data (all lexical; no network)
# =============================================================================

#: Minimal, *illustrative* map of well-known brand names to their canonical
#: registrable domain, used by the all-ASCII typosquat heuristic. Deliberately
#: small and offline; a shipped build would source this from a reviewed,
#: versioned list. NOT exhaustive — see the coverage note in the task report.
#: The canonical registrable domain lets us flag ``paypal.co`` (wrong TLD) as a
#: typosquat while leaving the genuine ``paypal.com`` untouched.
_KNOWN_BRAND_DOMAINS: dict[str, str] = {
    "paypal": "paypal.com",
    "google": "google.com",
    "microsoft": "microsoft.com",
    "apple": "apple.com",
    "amazon": "amazon.com",
    "facebook": "facebook.com",
    "netflix": "netflix.com",
    "linkedin": "linkedin.com",
    "dropbox": "dropbox.com",
    "instagram": "instagram.com",
    "whatsapp": "whatsapp.com",
    "chase": "chase.com",
    "wellsfargo": "wellsfargo.com",
    "bankofamerica": "bankofamerica.com",
    "coinbase": "coinbase.com",
    "binance": "binance.com",
    "docusign": "docusign.com",
    "office365": "office365.com",
}

#: Set view of brand *names* for fast membership tests.
_KNOWN_BRANDS: frozenset[str] = frozenset(_KNOWN_BRAND_DOMAINS)

#: HTML attributes whose external http(s) target is an *auto-loaded* remote
#: resource (not a user-clicked anchor). Mirrors the resource/action attributes
#: the parser extracts. A finding here means "this markup would fetch an
#: external resource on render" — which NerveShield never does (T-05).
_REMOTE_RESOURCE_ATTRS: frozenset[str] = frozenset(
    {"action", "background", "poster", "formaction", "data", "src"}
)

#: Lexically recognized URL-shortener hosts. Recognition is offline: we do NOT
#: expand or resolve any shortened link (R-005.2, T-05). NOT exhaustive.
_SHORTENER_HOSTS: frozenset[str] = frozenset(
    {
        "bit.ly",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "ow.ly",
        "buff.ly",
        "is.gd",
        "rebrand.ly",
        "cutt.ly",
        "rb.gy",
        "shorturl.at",
        "tiny.cc",
    }
)

#: Minimal built-in public-suffix set for registrable-domain (eTLD+1) parsing.
#: Multi-label suffixes are listed explicitly; everything else falls back to the
#: final label. This is *not* the full Mozilla PSL (no new dependency) — see the
#: coverage note. Ordered longest-match-first at lookup time.
_MULTI_LABEL_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk",
        "org.uk",
        "gov.uk",
        "ac.uk",
        "co.jp",
        "co.in",
        "co.nz",
        "co.za",
        "com.au",
        "com.br",
        "com.cn",
        "com.mx",
        "com.sg",
        "com.hk",
        "com.tr",
    }
)

#: Characters that are visually confusable with ASCII letters when all-ASCII
#: (digit-for-letter / punctuation substitutions). Used by the typosquat
#: heuristic to fold a candidate toward a canonical brand skeleton.
_ASCII_CONFUSABLE_FOLD: dict[str, str] = {
    "0": "o",
    "1": "l",
    "3": "e",
    "4": "a",
    "5": "s",
    "7": "t",
    "8": "b",
    "9": "g",
    "$": "s",
    "@": "a",
    "|": "l",
    "!": "i",
}

# -- phrase indicators (lexical; no single regex proves an attack) -----------

#: AI-prompt-injection lexical patterns (T-10/T-11). Each is an INERT indicator.
_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(?:all\s+)?(?:your\s+)?previous\s+instructions", re.I),
    re.compile(r"disregard\s+(?:your\s+|the\s+)?(?:system\s+)?(?:prompt|instructions|rules)", re.I),
    re.compile(r"forget\s+(?:all\s+)?(?:prior|previous|your)\s+(?:rules|instructions)", re.I),
    re.compile(r"\byou\s+are\s+now\s+(?:dan|an?\s+unrestricted|a\s+different)", re.I),
    re.compile(r"\b(?:system|assistant)\s*:\s*(?:override|ignore|reveal|disable)", re.I),
    re.compile(r"act\s+as\s+(?:an?\s+)?unrestricted", re.I),
    re.compile(r"override\s+(?:safety|your\s+safety|the\s+safety)", re.I),
    re.compile(r"\bnew\s+instructions\b.*\bfollow\b", re.I),
    re.compile(r"\bfollow\b.*\bnew\s+instructions\b", re.I),
)

#: Social-engineering pressure / account-takeover / payment / secret-harvesting
#: lexical patterns (design §5.2 phrases). Advisory evidence only.
_SOCIAL_ENGINEERING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bverify\s+your\s+account\s+(?:now|immediately)\b", re.I),
    re.compile(
        r"\byour\s+account\s+(?:will\s+be|has\s+been)\s+(?:suspended|locked|closed)\b",
        re.I,
    ),
    re.compile(r"\bconfirm\s+your\s+password\b", re.I),
    re.compile(r"\bupdate\s+your\s+payment\s+(?:details|information)\b", re.I),
    re.compile(r"\bwire\s+(?:the\s+)?(?:funds|money|payment)\b", re.I),
)


# =============================================================================
# public entry points
# =============================================================================


def inspect_offline(evidence: ParsedEvidence) -> list[SecurityFinding]:
    """Run all parsed-evidence inspections and return deterministic findings.

    Pure function of ``evidence``: no network, no DNS, no URL resolution. The
    returned list is ordered deterministically (URL/domain findings in link
    order, then image findings in image order, then HTML text findings in part
    order, then phrase findings) and every ``finding_key`` is unique, so the
    same input always yields byte-identical findings (R-005 determinism).
    """
    if not isinstance(evidence, ParsedEvidence):
        raise TypeError("evidence must be a ParsedEvidence")

    findings: list[SecurityFinding] = []
    findings.extend(_inspect_links(evidence))
    findings.extend(_inspect_images(evidence))
    findings.extend(_inspect_html_parts(evidence))
    findings.extend(_inspect_phrases(evidence))
    return findings


def inspect_message(
    evidence: ParsedEvidence,
    *,
    addresses: SenderAddresses | None = None,
) -> list[SecurityFinding]:
    """Run offline evidence inspections plus offline sender-provenance checks.

    Convenience composition: :func:`inspect_offline` followed by
    :func:`inspect_offline_sender_provenance`. Sender findings are appended after the
    evidence findings so ordering stays deterministic. Still performs zero
    network I/O (R-005.2, T-05).
    """
    findings = inspect_offline(evidence)
    if addresses is not None:
        findings.extend(inspect_offline_sender_provenance(addresses))
    return findings


def inspect_offline_sender_provenance(
    addresses: SenderAddresses,
) -> list[SecurityFinding]:
    """Emit Task 1.5 offline sender-provenance findings (R-005, T-03), no network.

    This is the *richer, registrable-domain* sender check that belongs to the
    Task 1.5 URL/domain inspection taxonomy: it compares registrable domains
    (eTLD+1) rather than bare domains and promotes ``REPLY_TO_MISMATCH`` /
    ``RETURN_PATH_MISMATCH`` / ``DOMAIN_CONFUSABLE_IDNA`` to *rule_ids*. It is
    deliberately **named distinctly** from the Task 1.4
    :func:`backend.mail_security.auth_evidence.inspect_sender_provenance`
    (which keeps its bare-domain comparison and ``auth.sender.*`` rule-id
    taxonomy) so the two detectors never collide on name, signature or
    ``finding_key`` (review findings 1 and 3). Both reuse the one shared
    confusable / domain canonicalization in
    :mod:`backend.mail_security._domains`.

    These are observations, never trust decisions; the Task 1.6 policy decides
    disposition.
    """
    if not isinstance(addresses, SenderAddresses):
        raise TypeError("addresses must be a SenderAddresses")

    findings: list[SecurityFinding] = []
    from_domain = _domain_of(addresses.from_address)

    if from_domain and addresses.reply_to is not None:
        reply_domain = _domain_of(addresses.reply_to)
        if reply_domain and _registrable(reply_domain) != _registrable(from_domain):
            findings.append(
                _finding(
                    finding_key="offline-sender-reply-to-mismatch",
                    rule_id=_RULE_REPLY_TO_MISMATCH,
                    category=FindingCategory.SENDER_IMPERSONATION,
                    severity=FindingSeverity.MEDIUM,
                    status=FindingStatus.OBSERVED,
                    explanation_code="REPLY_TO_DOMAIN_MISMATCH",
                    evidence_refs=["sender:reply-to"],
                )
            )

    if from_domain and addresses.return_path is not None:
        return_domain = _domain_of(addresses.return_path)
        if return_domain and _registrable(return_domain) != _registrable(from_domain):
            findings.append(
                _finding(
                    finding_key="offline-sender-return-path-mismatch",
                    rule_id=_RULE_RETURN_PATH_MISMATCH,
                    category=FindingCategory.SENDER_IMPERSONATION,
                    severity=FindingSeverity.LOW,
                    status=FindingStatus.OBSERVED,
                    explanation_code="RETURN_PATH_DOMAIN_MISMATCH",
                    evidence_refs=["sender:return-path"],
                )
            )

    if from_domain:
        findings.extend(
            _domain_deception_findings(
                from_domain,
                finding_key_prefix="offline-sender-from",
                evidence_ref="sender:from",
            )
        )

    return findings


# =============================================================================
# link / URL inspections
# =============================================================================


def _inspect_links(evidence: ParsedEvidence) -> list[SecurityFinding]:
    """Inspect each extracted link lexically (no resolution)."""
    findings: list[SecurityFinding] = []
    for index, link in enumerate(evidence.links):
        target_host = _url_host(link.target)
        prefix = f"link-{index}"
        ref = link.evidence_ref

        # Visible-text vs actual-href mismatch: when the display text itself
        # looks like a URL/host that disagrees with the real target host.
        display_host = _url_host(link.display_text)
        if (
            display_host
            and target_host
            and _registrable(display_host) != _registrable(target_host)
        ):
            findings.append(
                _finding(
                    finding_key=f"{prefix}-display-mismatch",
                    rule_id=_RULE_DISPLAY_TARGET_MISMATCH,
                    category=FindingCategory.URL_DECEPTION,
                    severity=FindingSeverity.MEDIUM,
                    status=FindingStatus.SUSPECTED,
                    explanation_code="DISPLAY_HOST_DIFFERS_FROM_TARGET",
                    evidence_refs=[ref],
                )
            )

        if not target_host:
            continue

        # Confusable / typosquat domain findings on the actual target host.
        for finding in _domain_deception_findings(
            target_host, finding_key_prefix=prefix, evidence_ref=ref
        ):
            findings.append(finding)

        # Subdomain deception: a known brand appearing as a *subdomain* label of
        # a different registrable domain (e.g. paypal.com.evil.example).
        if _is_subdomain_brand_deception(target_host):
            findings.append(
                _finding(
                    finding_key=f"{prefix}-subdomain-deception",
                    rule_id=_RULE_SUBDOMAIN_DECEPTION,
                    category=FindingCategory.URL_DECEPTION,
                    severity=FindingSeverity.MEDIUM,
                    status=FindingStatus.SUSPECTED,
                    explanation_code="BRAND_IN_DECEPTIVE_SUBDOMAIN",
                    evidence_refs=[ref],
                )
            )

        # Shortened link: recognized lexically, never expanded.
        if _registrable(target_host) in _SHORTENER_HOSTS:
            findings.append(
                _finding(
                    finding_key=f"{prefix}-shortened",
                    rule_id=_RULE_SHORTENED_LINK,
                    category=FindingCategory.URL_DECEPTION,
                    severity=FindingSeverity.LOW,
                    status=FindingStatus.OBSERVED,
                    explanation_code="SHORTENED_LINK_NOT_EXPANDED",
                    evidence_refs=[ref],
                )
            )

        # External link remote-resource (folded in from Task 1.3): an external
        # http(s) target referenced by a *resource/action* attribute
        # (form action / background / data / poster / formaction) is a remote
        # resource recorded without any fetch (T-05). A plain clickable
        # ``<a href>`` anchor or a bare text URL requires an explicit user click
        # and is NOT treated as an auto-loaded remote resource, so ordinary mail
        # with ordinary links produces no false-positive here (R-005).
        if (
            link.attribute is not None
            and link.attribute.lower() in _REMOTE_RESOURCE_ATTRS
            and _is_external_http(link.target)
        ):
            findings.append(
                _finding(
                    finding_key=f"{prefix}-remote-resource",
                    rule_id=_RULE_LINK_REMOTE_RESOURCE,
                    category=FindingCategory.TRACKING,
                    severity=FindingSeverity.INFO,
                    status=FindingStatus.OBSERVED,
                    explanation_code="EXTERNAL_LINK_RESOURCE",
                    evidence_refs=[ref],
                )
            )

    return findings


def _inspect_images(evidence: ParsedEvidence) -> list[SecurityFinding]:
    """Flag remote images and basic tracking-pixel heuristics (no fetch)."""
    findings: list[SecurityFinding] = []
    for index, image in enumerate(evidence.images):
        if not image.is_remote:
            continue
        prefix = f"image-{index}"
        ref = image.evidence_ref
        if _looks_like_tracking_pixel(image.target):
            findings.append(
                _finding(
                    finding_key=f"{prefix}-tracking-pixel",
                    rule_id=_RULE_REMOTE_IMAGE_PIXEL,
                    category=FindingCategory.TRACKING,
                    severity=FindingSeverity.LOW,
                    status=FindingStatus.SUSPECTED,
                    explanation_code="REMOTE_TRACKING_PIXEL",
                    evidence_refs=[ref],
                )
            )
        else:
            findings.append(
                _finding(
                    finding_key=f"{prefix}-remote-image",
                    rule_id=_RULE_REMOTE_IMAGE,
                    category=FindingCategory.TRACKING,
                    severity=FindingSeverity.INFO,
                    status=FindingStatus.OBSERVED,
                    explanation_code="REMOTE_IMAGE_RESOURCE",
                    evidence_refs=[ref],
                )
            )
        # Prompt-injection hidden in alt text is captured as an inert finding.
        if image.alt_text and _matches_any(image.alt_text, _PROMPT_INJECTION_PATTERNS):
            findings.append(
                _finding(
                    finding_key=f"{prefix}-alt-prompt-injection",
                    rule_id=_RULE_PROMPT_INJECTION,
                    category=FindingCategory.PROMPT_INJECTION,
                    severity=FindingSeverity.MEDIUM,
                    status=FindingStatus.OBSERVED,
                    explanation_code="PROMPT_INJECTION_IN_ALT_TEXT",
                    evidence_refs=[ref],
                )
            )
    return findings


# =============================================================================
# HTML structural inspections
# =============================================================================

#: Inline-style fragments that render text invisible or effectively hidden.
_HIDDEN_STYLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"display\s*:\s*none", re.I),
    re.compile(r"visibility\s*:\s*hidden", re.I),
    re.compile(r"font-size\s*:\s*0(?:px|pt|em|%)?\b", re.I),
    re.compile(r"opacity\s*:\s*0(?:\.0+)?\b", re.I),
    re.compile(r"(?:left|top)\s*:\s*-\d{3,}px", re.I),
    re.compile(r"text-indent\s*:\s*-\d{3,}px", re.I),
    re.compile(r"width\s*:\s*0(?:px)?\s*;\s*height\s*:\s*0(?:px)?", re.I),
)

#: White-on-white (or same fg/bg) heuristic: color and background both #fff.
_WHITE_ON_WHITE = re.compile(
    r"color\s*:\s*#?(?:fff(?:fff)?|white)\b[^\"']*background[^:]*:\s*#?(?:fff(?:fff)?|white)\b",
    re.I,
)

#: 1x1 (or near-zero) sized <img> — a classic tracking pixel in markup.
_TINY_IMG = re.compile(
    r"<img\b[^>]*\b(?:width|height)\s*=\s*[\"']?\s*(?:0|1)\s*[\"']?[^>]*"
    r"\b(?:width|height)\s*=\s*[\"']?\s*(?:0|1)\s*[\"']?",
    re.I,
)


def _inspect_html_parts(evidence: ParsedEvidence) -> list[SecurityFinding]:
    """Detect hidden/zero-size/offscreen text and markup tracking pixels."""
    findings: list[SecurityFinding] = []
    for index, part in enumerate(evidence.text_parts):
        if part.subtype != "html":
            continue
        html = part.raw
        prefix = f"html-{index}"
        ref = part.evidence_ref
        if _has_hidden_text(html):
            findings.append(
                _finding(
                    finding_key=f"{prefix}-hidden-text",
                    rule_id=_RULE_HTML_HIDDEN_TEXT,
                    category=FindingCategory.HTML_RISK,
                    severity=FindingSeverity.LOW,
                    status=FindingStatus.SUSPECTED,
                    explanation_code="HIDDEN_OR_OFFSCREEN_TEXT",
                    evidence_refs=[ref],
                )
            )
        if _TINY_IMG.search(html):
            findings.append(
                _finding(
                    finding_key=f"{prefix}-markup-pixel",
                    rule_id=_RULE_HTML_HIDDEN_TEXT,
                    category=FindingCategory.HTML_RISK,
                    severity=FindingSeverity.LOW,
                    status=FindingStatus.SUSPECTED,
                    explanation_code="TINY_TRACKING_IMAGE_MARKUP",
                    evidence_refs=[ref],
                )
            )
    return findings


def _has_hidden_text(html: str) -> bool:
    for pattern in _HIDDEN_STYLE_PATTERNS:
        if pattern.search(html):
            return True
    if _WHITE_ON_WHITE.search(html):
        return True
    return False


# =============================================================================
# phrase inspections
# =============================================================================


def _inspect_phrases(evidence: ParsedEvidence) -> list[SecurityFinding]:
    """Scan decoded text parts for inert prompt-injection / pressure phrases."""
    findings: list[SecurityFinding] = []
    # Collect the evidence_refs of the parts that matched, in part order, so the
    # phrase findings point back at the exact text parts they came from
    # (R-005.5). Bounded by the model's evidence_refs cap; parts are few.
    injection_refs: list[str] = []
    social_refs: list[str] = []
    for part in evidence.text_parts:
        text = part.normalized or part.raw
        if _matches_any(text, _PROMPT_INJECTION_PATTERNS):
            injection_refs.append(part.evidence_ref)
        if _matches_any(text, _SOCIAL_ENGINEERING_PATTERNS):
            social_refs.append(part.evidence_ref)

    if injection_refs:
        findings.append(
            _finding(
                finding_key="phrase-prompt-injection",
                rule_id=_RULE_PROMPT_INJECTION,
                category=FindingCategory.PROMPT_INJECTION,
                severity=FindingSeverity.MEDIUM,
                status=FindingStatus.OBSERVED,
                explanation_code="PROMPT_INJECTION_PHRASE",
                evidence_refs=_dedupe(injection_refs),
            )
        )
    if social_refs:
        findings.append(
            _finding(
                finding_key="phrase-social-engineering",
                rule_id=_RULE_SOCIAL_ENGINEERING,
                category=FindingCategory.SOCIAL_ENGINEERING,
                severity=FindingSeverity.LOW,
                status=FindingStatus.SUSPECTED,
                explanation_code="SOCIAL_ENGINEERING_PRESSURE",
                evidence_refs=_dedupe(social_refs),
            )
        )
    return findings


def _dedupe(refs: list[str]) -> list[str]:
    """Order-preserving de-duplication, bounded to the model's refs cap (24)."""
    seen: set[str] = set()
    unique: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            unique.append(ref)
        if len(unique) >= 24:
            break
    return unique


# =============================================================================
# domain-deception helpers (offline, shared by sender + URL paths)
# =============================================================================


def _domain_deception_findings(
    host: str, *, finding_key_prefix: str, evidence_ref: str
) -> list[SecurityFinding]:
    """Emit confusable-IDNA and all-ASCII typosquat findings for a host.

    ``evidence_ref`` is the opaque reference of the originating link (or a
    synthetic ``sender:*`` token for the From domain) that the emitted findings
    point back at (R-005.5).
    """
    findings: list[SecurityFinding] = []
    if _is_confusable_domain(host):
        findings.append(
            _finding(
                finding_key=f"{finding_key_prefix}-confusable",
                rule_id=_RULE_CONFUSABLE_IDNA,
                category=FindingCategory.URL_DECEPTION,
                severity=FindingSeverity.HIGH,
                status=FindingStatus.SUSPECTED,
                explanation_code="IDNA_UNICODE_CONFUSABLE",
                evidence_refs=[evidence_ref],
            )
        )
    elif _is_ascii_typosquat(host):
        # Only flag a typosquat when the host is all-ASCII (the confusable rule
        # already covers non-ASCII lookalikes); avoids double-flagging.
        findings.append(
            _finding(
                finding_key=f"{finding_key_prefix}-typosquat",
                rule_id=_RULE_TYPOSQUAT_ASCII,
                category=FindingCategory.URL_DECEPTION,
                severity=FindingSeverity.MEDIUM,
                status=FindingStatus.SUSPECTED,
                explanation_code="ASCII_BRAND_TYPOSQUAT",
                evidence_refs=[evidence_ref],
            )
        )
    return findings


def _is_ascii_typosquat(host: str) -> bool:
    """All-ASCII typosquat / lookalike heuristic (folded-in Task 1.4 gap).

    Offline only. Flags a host whose registrable *name* (eTLD+1 minus the public
    suffix) is a near-miss of a known brand via any of:

    - confusable digit/punctuation-for-letter substitution that folds onto a
      brand (``paypa1`` -> ``paypal``, ``g00gle`` -> ``google``);
    - a brand name with an appended hyphenated suffix (``paypal-support``);
    - a brand name under a *different* public suffix than the canonical one the
      genuine brand uses here is too weak to assert; instead we flag a short
      edit-distance near-miss of a brand (distance 1, same length band).

    The genuine brand name itself never flags.
    """
    if not host or not host.isascii():
        return False
    registrable = _registrable(host)
    name = registrable.split(".", 1)[0] if "." in registrable else registrable
    if not name:
        return False
    name = name.lower()

    # Exact brand name: genuine only when the registrable domain matches the
    # brand's canonical domain. A brand name under a different registrable
    # domain (e.g. ``paypal.co`` vs ``paypal.com``) is a typosquat.
    if name in _KNOWN_BRANDS:
        return registrable != _KNOWN_BRAND_DOMAINS[name]

    # Hyphenated brand suffix: a label that starts with "<brand>-".
    for brand in _KNOWN_BRANDS:
        if name.startswith(f"{brand}-") or name.endswith(f"-{brand}"):
            return True

    # Confusable digit/punctuation fold onto a brand (paypa1 -> paypal).
    folded = "".join(_ASCII_CONFUSABLE_FOLD.get(ch, ch) for ch in name)
    if folded != name and folded in _KNOWN_BRANDS:
        return True

    # Short edit-distance near-miss to a brand of comparable length.
    for brand in _KNOWN_BRANDS:
        if abs(len(name) - len(brand)) > 1:
            continue
        if _edit_distance_le_1(name, brand):
            return True

    return False


def _is_subdomain_brand_deception(host: str) -> bool:
    """Flag a known brand appearing as a non-registrable subdomain label.

    E.g. ``paypal.com.evil.example`` or ``login.paypal.secure.example`` where
    the registrable domain is NOT the brand's. Offline, lexical only.
    """
    if not host:
        return False
    registrable = _registrable(host)
    reg_name = registrable.split(".", 1)[0] if "." in registrable else registrable
    labels = host.lower().split(".")
    # Subdomain labels are everything left of the registrable domain.
    reg_label_count = registrable.count(".") + 1
    subdomain_labels = labels[: max(0, len(labels) - reg_label_count)]
    for label in subdomain_labels:
        if label in _KNOWN_BRANDS and reg_name.lower() not in _KNOWN_BRANDS:
            return True
    return False


# =============================================================================
# URL / host parsing helpers (offline, lexical)
# =============================================================================


def _url_host(value: str) -> str | None:
    """Extract and normalize the host from a URL-ish string, offline.

    Handles percent-encoding and punycode normalization lexically. Returns
    ``None`` when ``value`` has no recognizable host. Never resolves anything.
    """
    if not value:
        return None
    text = value.strip()
    # Percent-decode a bounded number of times to normalize encoded hosts.
    text = _percent_decode(text)
    lowered = text.lower()

    # Require a scheme or protocol-relative form, or a bare host that looks like
    # a domain. This avoids treating arbitrary display text as a URL.
    if "://" in lowered:
        rest = text.split("://", 1)[1]
    elif lowered.startswith("//"):
        rest = text[2:]
    elif lowered.startswith("www."):
        rest = text
    else:
        return None

    # Strip userinfo, path, query, fragment and port.
    rest = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[-1]
    rest = rest.split(":", 1)[0]
    host = rest.strip().strip(".")
    return host.lower() or None


def _percent_decode(value: str, *, rounds: int = 2) -> str:
    """Decode ``%XX`` escapes lexically, bounded rounds, never raising."""
    text = value
    pattern = re.compile(r"%([0-9A-Fa-f]{2})")
    for _ in range(rounds):
        if "%" not in text:
            break

        def _sub(match: re.Match[str]) -> str:
            try:
                return bytes.fromhex(match.group(1)).decode("latin-1")
            except ValueError:  # pragma: no cover - regex guarantees hex
                return match.group(0)

        decoded = pattern.sub(_sub, text)
        if decoded == text:
            break
        text = decoded
    return text


def _registrable(host: str) -> str:
    """Return the registrable domain (eTLD+1) using the minimal built-in PSL.

    Not the full Mozilla PSL (no new dependency): multi-label suffixes in
    :data:`_MULTI_LABEL_SUFFIXES` are matched longest-first, otherwise the last
    two labels are used. Single-label hosts return as-is. Offline, pure.
    """
    if not host:
        return ""
    labels = host.lower().strip(".").split(".")
    if len(labels) <= 1:
        return host.lower()

    # Longest multi-label public-suffix match first.
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LABEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


def _is_external_http(target: str) -> bool:
    """Return True for an external http(s)/protocol-relative target (no cid:)."""
    lowered = target.strip().lower()
    return (
        lowered.startswith("http://")
        or lowered.startswith("https://")
        or lowered.startswith("//")
    )


def _looks_like_tracking_pixel(target: str) -> bool:
    """Heuristic: a remote image URL that looks like a 1x1 tracking beacon.

    Offline lexical signal based on the URL text only (we never fetch to check
    the pixel size, T-05): tiny-image filenames, ``pixel``/``beacon``/``track``
    path tokens, or a per-recipient id query parameter.
    """
    lowered = _percent_decode(target).lower()
    tokens = ("pixel", "/track", "beacon", "/open?", "spacer.gif", "1x1", "pxl")
    if any(token in lowered for token in tokens):
        return True
    # id-bearing query on an image is a common open-tracking pattern.
    if "?" in lowered and re.search(r"[?&](uid|eid|mid|rcpt|email|u|e)=", lowered):
        return True
    return False


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for pattern in patterns)


def _edit_distance_le_1(a: str, b: str) -> bool:
    """Return True iff Levenshtein distance between ``a`` and ``b`` is <= 1.

    Bounded, allocation-light check (no full DP matrix needed for distance 1).
    """
    if a == b:
        return False  # identical is not a typosquat
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        # Exactly one substitution allowed.
        diffs = sum(1 for x, y in zip(a, b, strict=False) if x != y)
        return diffs == 1
    # One insertion/deletion: the shorter must be the longer with one char cut.
    shorter, longer = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


# =============================================================================
# small shared helpers
# =============================================================================


def _finding(
    *,
    finding_key: str,
    rule_id: str,
    category: FindingCategory,
    severity: FindingSeverity,
    status: FindingStatus,
    explanation_code: Code,
    evidence_refs: list[str] | None = None,
) -> SecurityFinding:
    """Build a metadata-only typed finding (no raw body text, I-05/R-022.4).

    ``evidence_refs`` are the *opaque* evidence references the finding points
    back at — the parser's stable per-part / per-link / per-image
    ``evidence_ref`` tokens (e.g. ``part:text:html:0:link:0``) or a synthetic
    ``sender:*`` token for address-level findings. They carry no raw body text
    by construction (the :class:`OpaqueRef` pattern forbids it) and preserve the
    traceability R-005.5 / design §9 require.
    """
    return SecurityFinding(
        finding_key=finding_key,
        rule_id=rule_id,
        category=category,
        severity=severity,
        status=status,
        detector_version=INSPECTIONS_VERSION,
        evidence_refs=list(evidence_refs) if evidence_refs else [],
        explanation_code=explanation_code,
    )


__all__ = [
    "INSPECTIONS_VERSION",
    "SenderAddresses",
    "inspect_message",
    "inspect_offline",
    "inspect_offline_sender_provenance",
]
