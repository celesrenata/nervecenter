"""Deterministic SYNTHETIC labeled-example generator (task 3.2).

Generator design (fixed up front; NOT tuned to make a model look good)
---------------------------------------------------------------------
* Every example is built from small hand-written templates with invented,
  non-existent names. No real mail, no real domains, no historical corpus.
* MALICIOUS: one of three families (credential phish, payment fraud, malware
  lure). Each picks a look-alike/decorated domain (brand + lure word + cheap
  TLD, occasionally digit substitution), a sender domain, and rule ids from a
  malicious pool.
* BENIGN: one of three families (newsletter, institution, receipt) with plain
  organisation domains and rule ids from a benign pool.
* Overlap is built in on purpose and fixed: tracking-pixel, remote-image and
  shortened-link rules appear on both classes with different probabilities.

Known limits (read before quoting any number)
---------------------------------------------
* The classes are separable almost trivially by domain character n-grams alone.
  High scores on this data are EXPECTED and MEANINGLESS for real-world mail.
* Held-out data comes from the same templates (different seed): it measures
  memorisation of templates, not generalisation, and not robustness to
  adversaries, drift or real-world base rates (50% malicious here).
* Randomness: :class:`random.Random` seeded with an int. Output is stable for a
  given CPython minor version; cross-version identity is not guaranteed.
"""

from __future__ import annotations

import random

from backend.mail_security.classifier.features import (
    FeatureRecord,
    Label,
    LabeledExample,
    char_ngrams,
)

_BRANDS = ("northbank", "cloudmail", "paywave", "docushare", "shipfast", "tradeo")
_LURES = ("secure", "login", "verify", "update", "billing", "support", "invoice")
_BAD_TLDS = ("top", "xyz", "click", "info", "work")
_GOOD_ORGS = (
    "greenlibrary", "oakmont-school", "cityutilities", "riverside-clinic", "pinecafe",
    "hillviewcoop", "lakeshore-news", "maplehardware", "sunnyfarms", "bluebooks",
)  # fmt: skip
_GOOD_TLDS = ("com", "org", "edu", "net")
_GOOD_SENDER_PREFIX = ("mail", "news", "billing", "noreply", "info")

#: (rule_id, probability) pools. Shared ids are deliberately on both sides.
_MAL_RULES: tuple[tuple[str, float], ...] = (
    ("URL_DISPLAY_TARGET_MISMATCH", 0.6),
    ("DOMAIN_TYPOSQUAT_ASCII", 0.4),
    ("URL_SUBDOMAIN_DECEPTION", 0.3),
    ("URL_SHORTENED_LINK", 0.3),
    ("REPLY_TO_MISMATCH", 0.4),
    ("SOCIAL_ENGINEERING_PRESSURE", 0.5),
    ("auth.sender.mismatch", 0.3),
    ("REMOTE_IMAGE_TRACKING_PIXEL", 0.3),
)
_BEN_RULES: tuple[tuple[str, float], ...] = (
    ("REMOTE_IMAGE_TRACKING_PIXEL", 0.4),
    ("REMOTE_IMAGE_RESOURCE", 0.4),
    ("LINK_REMOTE_RESOURCE", 0.3),
    ("URL_SHORTENED_LINK", 0.05),
    ("auth.results.provenance", 0.5),
)

_MAL_FAMILIES = ("credential_phish", "payment_fraud", "malware_lure")
_BEN_FAMILIES = ("newsletter", "institution", "receipt")


def _pick_rules(rng: random.Random, pool: tuple[tuple[str, float], ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rule_id, probability in pool:
        if rng.random() < probability:
            counts[rule_id] = 1 + (1 if rng.random() < 0.25 else 0)
    return counts


def _malicious_domain(rng: random.Random) -> str:
    brand = rng.choice(_BRANDS)
    if rng.random() < 0.3:  # digit substitution look-alike
        brand = brand.replace("o", "0", 1) if "o" in brand else brand.replace("a", "4", 1)
    lure = rng.choice(_LURES)
    tld = rng.choice(_BAD_TLDS)
    shape = rng.randrange(3)
    if shape == 0:
        return f"{brand}-{lure}.{tld}"
    if shape == 1:
        return f"{lure}.{brand}.{tld}"
    return f"{brand}{lure}{rng.randrange(10, 99)}.{tld}"


def _benign_domain(rng: random.Random) -> str:
    return f"{rng.choice(_GOOD_ORGS)}.{rng.choice(_GOOD_TLDS)}"


def _tokens(domain: str) -> tuple[str, ...]:
    return tuple(label for label in domain.split(".") if label)[:8]


def _malicious(rng: random.Random) -> FeatureRecord:
    family = rng.choice(_MAL_FAMILIES)
    url_domain = _malicious_domain(rng)
    domains = [url_domain]
    if rng.random() < 0.3:
        domains.append(_malicious_domain(rng))
    sender = url_domain if rng.random() < 0.4 else _malicious_domain(rng)
    attachments = 1 if family == "malware_lure" else (1 if rng.random() < 0.1 else 0)
    url_count = rng.randrange(1, 4)
    grams: list[str] = []
    for d in domains:
        grams.extend(g for g in char_ngrams(d) if g not in grams)
    return FeatureRecord(
        url_domain_ngrams=tuple(grams[:128]),
        finding_rule_counts=_pick_rules(rng, _MAL_RULES),
        url_count=url_count,
        distinct_url_domain_count=len(set(domains)),
        attachment_count=attachments,
        mime_part_count=rng.randrange(1, 4) + attachments,
        remote_image_count=rng.randrange(0, 2),
        sender_domain_tokens=_tokens(sender),
    )


def _benign(rng: random.Random) -> FeatureRecord:
    family = rng.choice(_BEN_FAMILIES)
    domains = [_benign_domain(rng)]
    if family == "newsletter" and rng.random() < 0.5:
        domains.append(_benign_domain(rng))
    sender_base = domains[0] if rng.random() < 0.8 else _benign_domain(rng)
    sender = f"{rng.choice(_GOOD_SENDER_PREFIX)}.{sender_base}"
    attachments = 1 if (family == "receipt" and rng.random() < 0.5) else 0
    url_count = rng.randrange(0, 6) if family == "newsletter" else rng.randrange(0, 3)
    grams: list[str] = []
    for d in domains:
        grams.extend(g for g in char_ngrams(d) if g not in grams)
    return FeatureRecord(
        url_domain_ngrams=tuple(grams[:128]),
        finding_rule_counts=_pick_rules(rng, _BEN_RULES),
        url_count=url_count,
        distinct_url_domain_count=len(set(domains)) if url_count else 0,
        attachment_count=attachments,
        mime_part_count=rng.randrange(1, 4) + attachments,
        remote_image_count=rng.randrange(0, 5) if family == "newsletter" else rng.randrange(0, 2),
        sender_domain_tokens=_tokens(sender),
    )


def generate_examples(
    seed: int, count: int, malicious_fraction: float = 0.5
) -> list[LabeledExample]:
    """Return ``count`` SYNTHETIC labeled examples, deterministic for ``seed``.

    Each label is an independent draw against ``malicious_fraction``, so the
    class mix is approximate. A record encodes its class only through the
    template distributions described in the module docstring.
    """
    if count < 0:
        raise ValueError("count must be >= 0")
    if not 0.0 <= malicious_fraction <= 1.0:
        raise ValueError("malicious_fraction must be within [0, 1]")
    rng = random.Random(seed)
    examples: list[LabeledExample] = []
    for _ in range(count):
        if rng.random() < malicious_fraction:
            examples.append(LabeledExample(features=_malicious(rng), label=Label.MALICIOUS))
        else:
            examples.append(LabeledExample(features=_benign(rng), label=Label.BENIGN))
    return examples
