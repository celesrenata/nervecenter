"""Synthetic evaluation for spike 8.2 (no real mail).

Deterministic generator (seeded ``random.Random``) of legitimate threads and
attack families, scored with :func:`thread_continuity.assess`.

Legitimate categories (false-positive analysis):

* ``L_REPLY``: participant replies, auth as before.
* ``L_NEW_COLLEAGUE``: a new participant from another organization joins.
* ``L_LIST_FORWARD``: mailing-list/ARC forward breaks auth (``forwarded``).
* ``L_NEVER_AUTH``: participant whose mail never authenticated.
* ``L_TICKET_REPLY_TO``: Reply-To to the same organization's ticketing host.
* ``L_COLD_LEDGER``: genuine reply whose parent was never ingested.
* ``L_VENDOR_PAYMENT_CHANGE``: a real vendor changes bank details (a
  legitimate event that *should* trigger independent verification).

Attack categories (detection):

* ``A_UNRELATED_INJECT``: unrelated domain cites a real thread.
* ``A_LOOKALIKE``: lookalike of a participant cites a real thread.
* ``A_COMPROMISED_PAYMENT``: real, authenticated participant changes payee.
* ``A_SPOOFED_FROM_REPLY_TO``: participant domain forged (auth fails where it
  used to pass) with a Reply-To to the attacker.
* ``A_FABRICATED_REFS``: ``References`` to ids that never existed.
* ``A_RE_SUBJECT_ONLY``: "Re:" subject with no references.
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict

from backend.mail_security.research.thread_continuity import Ledger, Observed, Status, assess

ORGS = [
    "acme-corp.com",
    "northwind.io",
    "contoso.net",
    "globex.org",
    "initech.co",
    "umbrella.biz",
    "stark-ind.com",
    "wayne-ent.net",
    "hooli.dev",
    "pied-piper.app",
]
LOOKALIKE = {
    "acme-corp.com": "acme-c0rp.com",
    "northwind.io": "nortwind.io",
    "contoso.net": "cont0so.net",
    "globex.org": "globexx.org",
    "initech.co": "lnitech.co",
    "umbrella.biz": "umbre1la.biz",
    "stark-ind.com": "stark-lnd.com",
    "wayne-ent.net": "wayne-ent.co",
    "hooli.dev": "hool1.dev",
    "pied-piper.app": "pied-plper.app",
}
ATTACKER = ["mail-secure-pay.com", "invoice-portal.net", "docs-share.org"]

LEGIT = [
    "L_REPLY",
    "L_NEW_COLLEAGUE",
    "L_LIST_FORWARD",
    "L_NEVER_AUTH",
    "L_TICKET_REPLY_TO",
    "L_COLD_LEDGER",
    "L_VENDOR_PAYMENT_CHANGE",
]
ATTACKS = [
    "A_UNRELATED_INJECT",
    "A_LOOKALIKE",
    "A_COMPROMISED_PAYMENT",
    "A_SPOOFED_FROM_REPLY_TO",
    "A_FABRICATED_REFS",
    "A_RE_SUBJECT_ONLY",
]


def _thread(rng: random.Random, ledger: Ledger, n: int, *, auth: bool, payee: str | None):
    org = rng.choice(ORGS)
    ids = []
    for i in range(n):
        mid = f"<t{rng.getrandbits(64):x}.{i}@{org}>"
        ledger.add(
            Observed(
                message_id=mid,
                from_addr=f"pat@{org}",
                subject="Re: project" if i else "project",
                in_reply_to=ids[-1] if ids else None,
                references=tuple(ids),
                auth_pass=auth,
                payee_fp=payee,
            )
        )
        ids.append(mid)
    return org, ids


def scenario(rng: random.Random, kind: str) -> Status:
    ledger = Ledger(salt=rng.randbytes(16))
    n = rng.randint(1, 6)
    auth = kind != "L_NEVER_AUTH" and rng.random() < 0.9
    payee = (
        f"p{rng.getrandbits(32):x}"
        if kind in ("L_VENDOR_PAYMENT_CHANGE", "A_COMPROMISED_PAYMENT") or rng.random() < 0.2
        else None
    )
    org, ids = _thread(rng, ledger, n, auth=auth if kind != "L_NEVER_AUTH" else None, payee=payee)
    # A few unrelated threads so the ledger is not trivially small.
    for _ in range(rng.randint(1, 4)):
        _thread(rng, ledger, rng.randint(1, 3), auth=True, payee=None)
    cite = dict(in_reply_to=ids[-1], references=tuple(ids), subject="Re: project")
    if kind == "L_REPLY":
        m = Observed(
            message_id=f"<x@{org}>",
            from_addr=f"pat@{org}",
            auth_pass=auth,
            payee_fp=payee,
            **cite,
        )
    elif kind == "L_NEW_COLLEAGUE":
        other = rng.choice([o for o in ORGS if o != org and LOOKALIKE[o] != org])
        m = Observed(message_id=f"<x@{other}>", from_addr=f"new@{other}", auth_pass=True, **cite)
    elif kind == "L_LIST_FORWARD":
        m = Observed(
            message_id=f"<x@{org}>",
            from_addr=f"pat@{org}",
            auth_pass=False,
            forwarded=True,
            **cite,
        )
    elif kind == "L_NEVER_AUTH":
        m = Observed(message_id=f"<x@{org}>", from_addr=f"pat@{org}", auth_pass=None, **cite)
    elif kind == "L_TICKET_REPLY_TO":
        m = Observed(
            message_id=f"<x@{org}>",
            from_addr=f"pat@{org}",
            reply_to=f"support@tickets.{org}",
            auth_pass=auth,
            **cite,
        )
    elif kind == "L_COLD_LEDGER":
        m = Observed(
            message_id=f"<x@{org}>",
            from_addr=f"pat@{org}",
            auth_pass=True,
            in_reply_to="<never-ingested@x>",
            references=("<never-ingested@x>",),
            subject="Re: project",
        )
    elif kind == "L_VENDOR_PAYMENT_CHANGE":
        m = Observed(
            message_id=f"<x@{org}>",
            from_addr=f"pat@{org}",
            auth_pass=auth,
            payee_fp="pNEW",
            **cite,
        )
    elif kind == "A_UNRELATED_INJECT":
        a = rng.choice(ATTACKER)
        m = Observed(message_id=f"<x@{a}>", from_addr=f"billing@{a}", auth_pass=True, **cite)
    elif kind == "A_LOOKALIKE":
        lk = LOOKALIKE[org]
        m = Observed(message_id=f"<x@{lk}>", from_addr=f"pat@{lk}", auth_pass=True, **cite)
    elif kind == "A_COMPROMISED_PAYMENT":
        m = Observed(
            message_id=f"<x@{org}>",
            from_addr=f"pat@{org}",
            auth_pass=auth,
            payee_fp="pATTACKER",
            **cite,
        )
    elif kind == "A_SPOOFED_FROM_REPLY_TO":
        a = rng.choice(ATTACKER)
        m = Observed(
            message_id=f"<x@{a}>",
            from_addr=f"pat@{org}",
            auth_pass=False,
            reply_to=f"pat@{a}",
            **cite,
        )
    elif kind == "A_FABRICATED_REFS":
        a = rng.choice(ATTACKER)
        fake = (f"<{rng.getrandbits(64):x}@{org}>",)
        m = Observed(
            message_id=f"<x@{a}>",
            from_addr=f"pat@{a}",
            auth_pass=True,
            in_reply_to=fake[0],
            references=fake,
            subject="Re: invoice",
        )
    elif kind == "A_RE_SUBJECT_ONLY":
        a = rng.choice(ATTACKER)
        m = Observed(
            message_id=f"<x@{a}>",
            from_addr=f"pat@{a}",
            auth_pass=True,
            subject="RE: overdue invoice",
        )
    else:  # pragma: no cover
        raise ValueError(kind)
    return assess(m, ledger).status


def evaluate(seed: int = 7, per_kind: int = 400) -> dict:
    rng = random.Random(seed)
    table: dict[str, Counter[str]] = defaultdict(Counter)
    for kind in LEGIT + ATTACKS:
        for _ in range(per_kind):
            table[kind][scenario(rng, kind).value] += 1
    flagged = {Status.SUSPECT.value, Status.BROKEN.value}
    out: dict = {"seed": seed, "per_kind": per_kind, "categories": {}}
    for kind, c in table.items():
        total = sum(c.values())
        out["categories"][kind] = {
            "distribution": dict(sorted(c.items())),
            "flagged_rate": round(sum(c[s] for s in flagged) / total, 4),
            "broken_rate": round(c[Status.BROKEN.value] / total, 4),
        }
    legit = [out["categories"][k] for k in LEGIT if k != "L_VENDOR_PAYMENT_CHANGE"]
    out["legit_flagged_rate_excl_vendor_change"] = round(
        sum(x["flagged_rate"] for x in legit) / len(legit), 4
    )
    out["legit_broken_rate_excl_vendor_change"] = round(
        sum(x["broken_rate"] for x in legit) / len(legit), 4
    )
    return out


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(evaluate(), indent=1))
