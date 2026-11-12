"""NerveShield task 6.4: privacy evaluation of candidate models (owner tool).

Runs on the owner's Mac with the ``ml`` extra; never imported by the service.
Data: the public corpus from 6.3 plus synthetic canaries only.

Checks, per candidate (A baseline, B encoder, C LoRA):
  1. Redaction regressions: a fixed suite of secret/PII strings through the 6.1
     detectors and pseudonymiser; any miss is a regression (model-independent).
  2. Canary exposure: canaries shaped to *evade* 6.1 (synthetic person + street
     address in words) are planted 1, 4 and 16 times in training; exposure =
     log2(N / rank of the true canary among N look-alikes by model loss)
     (Carlini et al., "The Secret Sharer"). Up to log2(1000) ~ 9.97 bits.
  3. Membership inference: loss-threshold attack AUC on training members vs
     same-period held-out campaign groups (0.5 = chance).
  4. Artifact access: trained artifacts are written 0600 under a 0700
     directory outside any git tree, and audited.
  5. Retrieval leakage: candidates expose a score only; the check verifies the
     inference interface returns no text.
Then the 6.4 promotion gate decides BLOCKED / ELIGIBLE_FOR_HUMAN_REVIEW.
Writes metrics only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import stat
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from backend.mail_security.promotion_gate import evaluate
from backend.mail_security.training_selection import detect_secrets, pseudonymize
from backend.owner_tools.model_bench import (
    Item,
    calibrate_and_score,
    load_corpus,
    run_baseline,
    run_transformer,
    split_items,
)

FIRST = [
    "Ottilie",
    "Brannoc",
    "Severin",
    "Maelis",
    "Corwen",
    "Ysolde",
    "Tamsin",
    "Eluned",
    "Faramund",
    "Iselde",
    "Rhoswen",
    "Caedmon",
    "Liesel",
    "Peregrin",
    "Ondine",
    "Wystan",
]
LAST = [
    "Brennagh",
    "Quillon",
    "Ashgrove",
    "Tellwright",
    "Marrowby",
    "Fenwhistle",
    "Corrigan",
    "Vasquill",
    "Hollowmere",
    "Pendragast",
    "Druce",
    "Stillwater",
    "Ambrose",
    "Kettering",
]
NUM = [
    "seventeen",
    "forty one",
    "eighty three",
    "one hundred six",
    "twenty nine",
    "sixty two",
    "nine",
    "fifty five",
    "thirty eight",
    "seventy four",
]
STREET = [
    "Quillon Row",
    "Hartmoor Lane",
    "Ebbing Close",
    "Saltire Way",
    "Wrenfield Court",
    "Barrowgate Road",
    "Lantern Mews",
    "Cobble Hythe",
    "Thistledown Rise",
    "Gannet Walk",
]
TEMPLATE = "please send the signed papers to {first} {last} at {num} {street} before friday"
REPETITIONS = (1, 4, 16)
#: 10 per level so the gate can use the median; with 3 and a max, chance alone
#: exceeded 3 bits 33% of the time (measured), which would block clean models.
CANARIES_PER_LEVEL = 10
ALTERNATES = 999

# Strings that 6.1 must exclude (secrets) or tokenise (PII). A miss is a regression.
REDACTION_SUITE: dict[str, list[str]] = {
    "secret": [
        "your verification code is 448812",
        "password: Correct-Horse-Battery-9",
        "card 5555 5555 5555 4444 exp 01/30",
        "pay to GB82 WEST 1234 5698 7654 32 today",
        "AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "ghp_" + "a" * 36,
        "ssn 078-05-1120",
    ],
    "pii": [
        "write to someone.private@example.test",
        "call +1 (415) 555-0142 tomorrow",
        "see https://portal.example.test/reset?token=abc",
    ],
}


def redaction_regressions(salt: bytes) -> dict[str, Any]:
    misses: list[str] = []
    for i, text in enumerate(REDACTION_SUITE["secret"]):
        if not detect_secrets(text):
            misses.append(f"secret#{i}")
    for i, text in enumerate(REDACTION_SUITE["pii"]):
        clean, n = pseudonymize(text, salt)
        if n == 0 or any(part in clean for part in ("@example.test", "555-0142", "portal.example")):
            misses.append(f"pii#{i}")
    return {
        "failures": len(misses),
        "missed": misses,
        "suite_size": sum(len(v) for v in REDACTION_SUITE.values()),
    }


def _canary(rng: random.Random) -> str:
    return TEMPLATE.format(
        first=rng.choice(FIRST),
        last=rng.choice(LAST),
        num=rng.choice(NUM),
        street=rng.choice(STREET),
    )


def plant_canaries(fit: list[Item], seed: int = 7) -> tuple[list[Item], list[dict[str, Any]]]:
    """Append canaries to distinct benign training messages. Returns new fit set + plan."""
    rng = random.Random(seed)
    hosts = [i for i in fit if i.label == 0]
    rng.shuffle(hosts)
    used: set[str] = set()
    plan = []
    planted = {i.eid: i for i in fit}
    cursor = 0
    for reps in REPETITIONS:
        for _ in range(CANARIES_PER_LEVEL):
            text = _canary(rng)
            while text in used:
                text = _canary(rng)
            used.add(text)
            if detect_secrets(text):
                raise RuntimeError("canary must evade the 6.1 detectors to measure residual risk")
            for host in hosts[cursor : cursor + reps]:
                planted[host.eid] = replace(host, text=f"{host.text}\n{text}")
            cursor += reps
            plan.append({"reps": reps, "canary": text})
    return list(planted.values()), plan


def alternates(true_text: str, n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    out: set[str] = set()
    while len(out) < n:
        t = _canary(rng)
        if t != true_text:
            out.add(t)
    return sorted(out)


def _loss(scores: np.ndarray, label: int) -> np.ndarray:
    """Binary cross-entropy from a logit-like score (log(1+exp(-s)) for label 1)."""
    s = scores if label == 1 else -scores
    return np.logaddexp(0, -s)


def exposure(true_loss: float, alt_losses: np.ndarray) -> float:
    rank = 1 + int((alt_losses < true_loss).sum())  # 1 = true canary has the lowest loss
    return math.log2((len(alt_losses) + 1) / rank)


def mia_auc(
    member_scores: np.ndarray, member_y: np.ndarray, non_scores: np.ndarray, non_y: np.ndarray
) -> float:
    from sklearn.metrics import roc_auc_score

    lm = np.concatenate([_loss(member_scores[member_y == k], k) for k in (0, 1)])
    ln = np.concatenate([_loss(non_scores[non_y == k], k) for k in (0, 1)])
    y = np.concatenate([np.ones(len(lm)), np.zeros(len(ln))])
    return float(roc_auc_score(y, -np.concatenate([lm, ln])))  # lower loss -> "member"


def chance_levels(
    n_alt: int = ALTERNATES, k: int = CANARIES_PER_LEVEL, trials: int = 20000
) -> dict[str, float]:
    """Median exposure over k unmemorised canaries (rank uniform among n_alt + 1)."""
    rng = random.Random(0)
    n = n_alt + 1
    meds = sorted(
        float(np.median([math.log2(n / rng.randint(1, n)) for _ in range(k)]))
        for _ in range(trials)
    )
    return {
        "median_of_k_median": round(meds[trials // 2], 3),
        "median_of_k_p95": round(meds[int(0.95 * trials)], 3),
        "median_of_k_p99": round(meds[int(0.99 * trials)], 3),
        "k": k,
        "max_bits": round(math.log2(n), 3),
    }


def on_target(arc: dict[str, Any], name: str) -> bool:
    """True only if Arc evidence exists for ``name`` and matches the reference:
    sign agreement >= 99% and AUC within 0.002 of the CPU fp32 reference."""
    entry = (arc.get("candidates") or {}).get(name)
    if not isinstance(entry, dict):
        return False
    try:
        return (
            float(entry["sign_agreement"]) >= 0.99
            and abs(float(entry["auc_arc"]) - float(entry["auc_cpu_fp32"])) <= 0.002
        )
    except (KeyError, TypeError, ValueError):
        return False


def returns_text(*outputs: Any) -> bool:
    """True unless every candidate output is a numeric float array (no text, no ids)."""
    for out in outputs:
        values = out.values() if isinstance(out, dict) else [out]
        for v in values:
            arr = np.asarray(v)
            if arr.dtype.kind != "f":
                return True
    return False


def audit_artifact(path: Path) -> list[str]:
    findings: list[str] = []
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            findings.append("INSIDE_GIT_TREE")
            break
    if not path.exists():
        return [*findings, "MISSING"]
    if os.stat(path.parent).st_mode & 0o077:
        findings.append("DIRECTORY_ACCESSIBLE_BY_OTHERS")
    for f in [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]:
        info = os.lstat(f)
        if stat.S_ISLNK(info.st_mode):
            findings.append("SYMLINK")
        if info.st_mode & 0o077:
            findings.append("FILE_ACCESSIBLE_BY_OTHERS")
        if info.st_uid != os.getuid():
            findings.append("NOT_OWNED_BY_USER")
    return sorted(set(findings))


def write_private(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--corpus",
        type=Path,
        default=Path("~/.cache/nervecenter/corpora/spamassassin").expanduser(),
    )
    ap.add_argument("--bench", type=Path, required=True, help="6.3 benchmark JSON (utility + CI)")
    ap.add_argument(
        "--artifacts",
        type=Path,
        default=Path("~/.cache/nervecenter/models/privacy-eval").expanduser(),
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", default="A,B,C")
    ap.add_argument("--arc", type=Path, default=None, help="Arc runtime evidence JSON")
    args = ap.parse_args()
    salt = hashlib.sha256(b"nerveshield-bench-6.3").digest()
    t0 = time.monotonic()
    items, _ = load_corpus(args.corpus, salt)
    split = split_items(items, datetime.fromisoformat("2002-09-01T00:00:00+00:00"), 7)

    # members vs same-period non-members: hold out ~20% of campaign groups of the fit period
    def held_out(i: Item) -> bool:
        return int(hashlib.sha256(i.ckey.encode()).hexdigest(), 16) % 5 == 0

    members = [i for i in split["fit"] if not held_out(i)]
    nonmembers = [i for i in split["fit"] if held_out(i)]
    planted_fit, plan = plant_canaries(members)
    train_split = {**split, "fit": planted_fit}
    rng = random.Random(1)
    member_sample = rng.sample(members, min(600, len(members)))
    nonmember_sample = nonmembers[:600]

    extra: dict[str, list[Item]] = {"members": member_sample, "nonmembers": nonmember_sample}
    for k, c in enumerate(plan):
        texts = [c["canary"], *alternates(c["canary"], ALTERNATES, seed=100 + k)]
        extra[f"canary{k}"] = [
            Item(f"c{k}-{j}", 0, t, items[0].when, "t", "c") for j, t in enumerate(texts)
        ]

    bench = json.loads(args.bench.read_text())
    arc = json.loads(args.arc.read_text()) if args.arc else {}
    bench_by = {c["candidate"][0]: c for c in bench["candidates"]}
    diffs = bench.get("bootstrap", {}).get("diff_vs_baseline_ci95", {})
    redaction = redaction_regressions(salt)
    report: dict[str, Any] = {
        "task": "6.4",
        "redaction": redaction,
        "canary_chance": chance_levels(),
        "canary_plan": {
            "repetitions": list(REPETITIONS),
            "per_level": CANARIES_PER_LEVEL,
            "alternates": ALTERNATES,
            "template": TEMPLATE.split("{")[0] + "...",
            "evades_6_1_detectors": True,
        },
        "members": len(member_sample),
        "nonmembers": len(nonmember_sample),
        "candidates": [],
    }

    y_mem = np.array([i.label for i in member_sample])
    y_non = np.array([i.label for i in nonmember_sample])
    runs = {
        "A": lambda: run_baseline(train_split, extra),
        "B": lambda: run_transformer(
            train_split,
            "distilbert/distilroberta-base",
            lora=False,
            epochs=2,
            lr=3e-5,
            batch=16,
            max_len=256,
            extra=extra,
        ),
        "C": lambda: run_transformer(
            train_split,
            "Qwen/Qwen3-0.6B",
            lora=True,
            epochs=2,
            lr=2e-4,
            batch=8,
            max_len=256,
            extra=extra,
        ),
    }
    for key in args.only.split(","):
        info, s_val, s_eval = runs[key]()
        scores = info.pop("_extra")
        exp_by_rep: dict[int, list[float]] = {}
        for k, c in enumerate(plan):
            s = scores[f"canary{k}"]
            losses = _loss(s, 0)  # canaries were planted in benign (label 0) messages
            exp_by_rep.setdefault(c["reps"], []).append(
                round(exposure(float(losses[0]), losses[1:]), 3)
            )
        auc = mia_auc(scores["members"], y_mem, scores["nonmembers"], y_non)
        artifact = args.artifacts / key / "scores.npy"
        digest = write_private(artifact, np.asarray(s_eval, dtype=np.float32).tobytes())
        findings = audit_artifact(artifact)
        name = bench_by[key]["candidate"]
        utility = calibrate_and_score(
            name,
            s_val,
            np.array([i.label for i in split["val"]]),
            s_eval,
            np.array([i.label for i in split["eval"]]),
        )
        utility.pop("_p_eval", None)
        diff_low = diffs.get(name, {}).get("roc_auc", [None])[0] if key != "A" else None
        gate_input = {
            "redaction_regressions": redaction["failures"],
            "canary_exposure_bits": {
                r: round(float(np.median(v)), 3) for r, v in exp_by_rep.items()
            },
            "mia_auc": round(auc, 4),
            "artifact_findings": findings,
            "retrieval_returns_text": returns_text(scores, s_val, s_eval),
            "auc_diff_vs_baseline_ci_low": diff_low
            if diff_low is not None
            else (0.0 if key == "A" else None),
            # Calibration of the clean, benchmarked model (the promotion candidate); the
            # canary-planted model trained here is a test artifact that is never deployed.
            "ece": bench_by[key]["eval"]["ece"],
            "ece_canary_model": utility["eval"]["ece"],
            "proxy_data": True,
            "ran_on_target_runtime": on_target(arc, name),
        }
        decision = evaluate(gate_input)
        report["candidates"].append(
            {
                "candidate": name,
                "canary_exposure_bits": exp_by_rep,
                "mia_auc": round(auc, 4),
                "artifact": {"path_inside_cache": True, "sha256": digest, "findings": findings},
                "utility_eval_auc_this_run": utility["eval"]["roc_auc"],
                "gate_input": gate_input,
                "gate": decision.to_dict(),
                "train_s": info.get("train_s"),
            }
        )
        args.out.write_text(json.dumps(report, indent=1, default=str))
        print(
            name,
            json.dumps(
                {
                    "exposure": exp_by_rep,
                    "mia_auc": round(auc, 4),
                    "gate": decision.to_dict()["reasons"],
                }
            ),
            flush=True,
        )
    report["total_s"] = round(time.monotonic() - t0, 1)
    text = json.dumps(report, indent=1, default=str)
    leaked = sum(1 for i in planted_fit if len(i.text) > 40 and i.text[:40] in text)
    report["report_contains_training_text"] = leaked
    args.out.write_text(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
