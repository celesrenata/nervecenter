"""Spike 8.1 evaluation on the public SpamAssassin corpus (no owner mail).

Reference "campaigns" are the existing content MinHash clusters
(``split_keys.campaign_keys`` over pseudonymised text). The question is how
much of that grouping survives when only content-free structural tokens are
used, and what the privacy properties of the fingerprints are.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from backend.mail_security.parser import parse_mime_evidence
from backend.mail_security.research import campaign_clustering as cc
from backend.mail_security.split_keys import campaign_keys
from backend.mail_security.training_selection import pseudonymize
from backend.owner_tools.model_bench import SETS, _header, received_time

_SALT = b"spike-8.1-public-corpus"
_SECRET = b"spike-8.1-epoch-secret-public-corpus-only"


def _pairs(groups: dict[str, list[str]]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for members in groups.values():
        m = sorted(members)
        out.update((a, b) for i, a in enumerate(m) for b in m[i + 1 :])
    return out


def load(root: Path):
    rows = []
    for set_name, label in SETS.items():
        for path in sorted((root / set_name).iterdir()):
            if path.name == "cmds" or not path.is_file():
                continue
            ev = parse_mime_evidence(path.read_bytes())
            when, _src = received_time(ev.headers)
            if when is None:
                continue
            subject = _header(ev.headers, "subject") or ""
            text, _ = pseudonymize(
                f"{subject}\n{' '.join(p.normalized for p in ev.text_parts)}"[:4000], _SALT
            )
            rows.append((f"{set_name}/{path.name[:12]}", label, when, ev, text))
    return rows


def evaluate(root: Path) -> dict:
    rows = load(root)
    labels = {r[0]: r[1] for r in rows}
    when = {r[0]: r[2] for r in rows}
    ref = campaign_keys(_SECRET, "corpus", {r[0]: r[4] for r in rows})
    base_key = cc.epoch_key(_SECRET, min(when.values()))
    tokens = {r[0]: cc.structural_tokens(r[3]) for r in rows}
    keyed = {i: cc.keyed(t, base_key) for i, t in tokens.items()}
    sigs = {i: cc.minhash(k, base_key) for i, k in keyed.items()}

    spam = [i for i in labels if labels[i] == 1]
    ref_groups: dict[str, list[str]] = defaultdict(list)
    for i in spam:
        ref_groups[ref[i]].append(i)
    ref_pairs = _pairs({k: v for k, v in ref_groups.items() if len(v) >= 2})

    out: dict = {
        "corpus": {"messages": len(rows), "spam": len(spam), "ham": len(rows) - len(spam)},
        "reference": {
            "spam_campaigns_size_ge2": sum(1 for v in ref_groups.values() if len(v) >= 2),
            "spam_in_campaigns": sum(len(v) for v in ref_groups.values() if len(v) >= 2),
            "pairs": len(ref_pairs),
        },
        "thresholds": {},
    }
    for th in (0.6, 0.7, 0.8, 0.9):
        roots = cc.cluster(sigs, threshold=th)
        groups: dict[str, list[str]] = defaultdict(list)
        for i, r in roots.items():
            groups[r].append(i)
        spam_groups = {r: [i for i in m if labels[i] == 1] for r, m in groups.items()}
        s_pairs = _pairs({r: m for r, m in spam_groups.items() if len(m) >= 2})
        tp = len(s_pairs & ref_pairs)
        big = [m for m in groups.values() if len(m) >= 3]
        pure = sum(1 for m in big if len({labels[i] for i in m}) == 1)
        ham_in_spammy = sum(
            sum(1 for i in m if labels[i] == 0)
            for m in big
            if sum(labels[i] for i in m) * 2 > len(m)
        )
        out["thresholds"][str(th)] = {
            "clusters_size_ge3": len(big),
            "largest": max((len(m) for m in groups.values()), default=0),
            "pair_precision_vs_reference": round(tp / len(s_pairs), 4) if s_pairs else None,
            "pair_recall_vs_reference": round(tp / len(ref_pairs), 4) if ref_pairs else None,
            "label_pure_cluster_share": round(pure / len(big), 4) if big else None,
            "ham_in_spam_majority_clusters": ham_in_spammy,
            "ham_total": len(rows) - len(spam),
        }

    # Privacy: re-identification risk and cross-epoch unlinkability.
    sets = Counter(keyed.values())
    unique = sum(1 for k in keyed.values() if sets[k] == 1)
    next_key = cc.epoch_key(_SECRET, min(when.values()) + timedelta(days=7))
    sample = sorted(keyed)[:500]
    shared = sum(len(keyed[i] & cc.keyed(tokens[i], next_key)) for i in sample)
    sim_cross = sum(
        cc.similarity(sigs[i], cc.minhash(cc.keyed(tokens[i], next_key), next_key)) for i in sample
    ) / len(sample)
    epochs_spanned = [
        len({int(when[i].timestamp() // timedelta(days=7).total_seconds()) for i in m})
        for m in ref_groups.values()
        if len(m) >= 2
    ]
    store = cc.CampaignStore(k=3)
    roots = cc.cluster(sigs, threshold=0.8)
    for i in sorted(roots, key=lambda x: when[x]):
        store.observe(roots[i], sigs[i], when[i])
    out["privacy"] = {
        "distinct_structural_vocab": len({t for ts in tokens.values() for t in ts}),
        "messages_with_unique_fingerprint_set": round(unique / len(rows), 4),
        "cross_epoch_shared_keyed_tokens": shared,
        "cross_epoch_mean_signature_similarity": round(sim_cross, 4),
        "reference_campaigns_spanning_gt1_week": round(
            sum(1 for e in epochs_spanned if e > 1) / len(epochs_spanned), 4
        )
        if epochs_spanned
        else None,
        "store_clusters": len(store.clusters),
        "releasable_aggregates_k3": len(store.aggregates()),
    }
    return out


if __name__ == "__main__":  # pragma: no cover
    root = (
        Path(sys.argv[1]).expanduser()
        if len(sys.argv) > 1
        else Path("~/.cache/nervecenter/corpora/spamassassin").expanduser()
    )
    print(json.dumps(evaluate(root), indent=1))
