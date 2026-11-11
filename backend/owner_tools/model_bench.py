"""NerveShield task 6.3: baseline vs encoder vs LoRA benchmark (owner tool).

Runs on the owner's Mac only. Needs the ``ml`` extra; nothing in the service
imports it. Data: the SpamAssassin public corpus (spam vs ham, a proxy for
malicious vs benign), parsed by the production bounded MIME parser and
pseudonymised with the 6.1 function, split with the 6.2 leakage-safe rule using
real thread and campaign keys.

Candidates
  A  hashed word+char n-grams -> logistic regression (CPU)
  B  DistilRoBERTa fine-tune (MPS)
  C  LoRA (peft) on Qwen3-0.6B sequence classification (MPS, bf16)
  D  frozen Qwen3-Embedding-0.6B (the model ArcForge serves on the Arc iGPUs)
     -> logistic regression head

Protocol: train on TRAIN groups; choose the threshold and fit Platt calibration
on VAL (the latest 15% of the training period by time); report on EVAL only.
Writes a JSON report with metrics, timings and environment; never text.
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import platform
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from backend.mail_security.dataset_manifest import ExampleMeta, Split, assign_splits
from backend.mail_security.parser import parse_mime_evidence
from backend.mail_security.split_keys import campaign_keys, thread_key
from backend.mail_security.training_selection import (
    SelectedExample,
    SourceKind,
    TrainingLabel,
    pseudonymize,
)

SETS = {"easy_ham": 0, "easy_ham_2": 0, "hard_ham": 0, "spam": 1, "spam_2": 1}
_TAG_RE = re.compile(r"<[^>]{0,2000}>")
MAX_CHARS = 4000


@dataclass
class Item:
    eid: str
    label: int
    text: str
    when: datetime
    tkey: str
    ckey: str = ""


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


def _parse_date(value: str | None) -> datetime | None:
    try:
        when = email.utils.parsedate_to_datetime(value or "")
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def received_time(headers: list[tuple[str, str]]) -> tuple[datetime | None, str]:
    """Time the message reached the mailbox: the date in the topmost ``Received``
    header (written by the receiving MTA). ``Date`` is sender-controlled and spam
    forges it, so it is only a fallback."""
    for name, value in headers:
        if name.lower() == "received" and ";" in value:
            when = _parse_date(value.rsplit(";", 1)[1].strip())
            if when is not None:
                return when.astimezone(UTC), "received"
            break
    when = _parse_date(_header(headers, "date"))
    return (when.astimezone(UTC), "date") if when else (None, "none")


def load_corpus(root: Path, salt: bytes) -> tuple[list[Item], dict[str, int]]:
    items: list[Item] = []
    stats = {"files": 0, "no_date": 0, "parse_incomplete": 0}
    for set_name, label in SETS.items():
        for path in sorted((root / set_name).iterdir()):
            if path.name == "cmds" or not path.is_file():
                continue
            stats["files"] += 1
            raw = path.read_bytes()
            ev = parse_mime_evidence(raw)
            if not ev.inspection_complete:
                stats["parse_incomplete"] += 1
            subject = _header(ev.headers, "subject") or ""
            parts = []
            for part in ev.text_parts:
                body = (
                    _TAG_RE.sub(" ", part.normalized) if part.subtype == "html" else part.normalized
                )
                parts.append(body)
            text, _ = pseudonymize(f"{subject}\n{' '.join(parts)}"[:MAX_CHARS], salt)
            when, source = received_time(ev.headers)
            if when is None:
                stats["no_date"] += 1
                continue
            stats[f"time_from_{source}"] = stats.get(f"time_from_{source}", 0) + 1
            eid = f"{set_name}/{path.name[:5]}"
            tkey = thread_key(
                salt,
                "corpus",
                message_id=_header(ev.headers, "message-id"),
                in_reply_to=_header(ev.headers, "in-reply-to"),
                references=_header(ev.headers, "references"),
                fallback=hashlib.sha256(raw).hexdigest(),
            )
            items.append(Item(eid, label, text, when.astimezone(UTC), tkey))
    ckeys = campaign_keys(salt, "corpus", {i.eid: i.text for i in items})
    for i in items:
        i.ckey = ckeys[i.eid]
    return items, stats


def split_items(items: list[Item], cutoff: datetime, embargo_days: int) -> dict[str, list[Item]]:
    examples = [
        SelectedExample(
            i.eid,
            TrainingLabel.MALICIOUS if i.label else TrainingLabel.BENIGN,
            "corpus",
            "public-corpus",
            SourceKind.SYNTHETIC,
            i.text,
        )
        for i in items
    ]
    meta = {i.eid: ExampleMeta(i.when, i.tkey, i.ckey) for i in items}
    assigned, dropped = assign_splits(
        examples, meta, cutoff=cutoff, embargo=timedelta(days=embargo_days)
    )
    by_id = {i.eid: i for i in items}
    train = sorted(
        (by_id[e] for e, s in assigned.items() if s is Split.TRAIN), key=lambda i: (i.when, i.eid)
    )
    evals = [by_id[e] for e, s in sorted(assigned.items()) if s is Split.EVAL]
    k = int(len(train) * 0.85)
    return {
        "fit": train[:k],
        "val": train[k:],
        "eval": evals,
        "dropped": [by_id[e] for e in dropped],
    }


# -- metrics -----------------------------------------------------------------------------


def _auc(y: np.ndarray, s: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, s)) if len(set(y.tolist())) == 2 else float("nan")


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        m = (p >= lo) & (p < hi) if hi < 1 else (p >= lo) & (p <= hi)
        if m.any():
            total += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(total)


def metrics(y: np.ndarray, p: np.ndarray, threshold: float) -> dict[str, Any]:
    from sklearn.metrics import average_precision_score, brier_score_loss

    pred = p >= threshold
    tp = int((pred & (y == 1)).sum())
    fp = int((pred & (y == 0)).sum())
    fn = int((~pred & (y == 1)).sum())
    tn = int((~pred & (y == 0)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    order = np.argsort(-p)
    ys = y[order]
    neg = max(int((y == 0).sum()), 1)
    pos = max(int((y == 1).sum()), 1)
    fpr_curve = np.cumsum(ys == 0) / neg
    tpr_curve = np.cumsum(ys == 1) / pos
    ok = fpr_curve <= 0.01
    recall_at_1pct_fpr = float(tpr_curve[ok].max()) if ok.any() else 0.0
    return {
        "n": int(len(y)),
        "positives": int(y.sum()),
        "threshold": round(threshold, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0,
        "fpr": round(fp / (fp + tn), 4) if fp + tn else 0.0,
        "roc_auc": round(_auc(y, p), 4),
        "pr_auc": round(float(average_precision_score(y, p)), 4),
        "brier": round(float(brier_score_loss(y, p)), 4),
        "ece": round(ece(y, p), 4),
        "recall_at_1pct_fpr": round(recall_at_1pct_fpr, 4),
        # false positives are weighted 5x: flagging a legitimate message costs more
        # user trust than one missed spam (owner may change the ratio)
        "cost_fp5_fn1": fp * 5 + fn,
    }


def calibrate_and_score(
    name: str, s_val: np.ndarray, y_val: np.ndarray, s_eval: np.ndarray, y_eval: np.ndarray
) -> dict[str, Any]:
    """Platt-calibrate on VAL, pick the cost-minimising threshold on VAL, report EVAL."""
    from sklearn.linear_model import LogisticRegression

    platt = LogisticRegression(C=1e4).fit(s_val.reshape(-1, 1), y_val)
    p_val = platt.predict_proba(s_val.reshape(-1, 1))[:, 1]
    p_eval = platt.predict_proba(s_eval.reshape(-1, 1))[:, 1]
    grid = np.unique(np.concatenate([p_val, [0.5]]))
    best = min(grid, key=lambda t: (metrics(y_val, p_val, float(t))["cost_fp5_fn1"], -t))
    raw_eval = 1 / (1 + np.exp(-s_eval)) if s_eval.min() < 0 or s_eval.max() > 1 else s_eval
    return {
        "candidate": name,
        "_p_eval": p_eval,  # removed before writing the report
        "eval": metrics(y_eval, p_eval, float(best)),
        "eval_uncalibrated_ece": round(ece(y_eval, np.clip(raw_eval, 0, 1)), 4),
        "val": metrics(y_val, p_val, float(best)),
    }


def _recall_at_fpr(y: np.ndarray, p: np.ndarray, max_fpr: float = 0.01) -> float:
    order = np.argsort(-p, kind="stable")
    ys = y[order]
    fpr = np.cumsum(ys == 0) / max(int((y == 0).sum()), 1)
    tpr = np.cumsum(ys == 1) / max(int((y == 1).sum()), 1)
    ok = fpr <= max_fpr
    return float(tpr[ok].max()) if ok.any() else 0.0


def paired_bootstrap(
    y: np.ndarray, scores: dict[str, np.ndarray], baseline: str, n: int = 1000, seed: int = 0
) -> dict[str, Any]:
    """95% intervals per candidate and for (candidate - baseline), same resamples."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    rng = np.random.default_rng(seed)
    fns = {
        "roc_auc": roc_auc_score,
        "pr_auc": average_precision_score,
        "recall_at_1pct_fpr": _recall_at_fpr,
    }
    draws: dict[str, dict[str, list[float]]] = {c: {m: [] for m in fns} for c in scores}
    for _ in range(n):
        idx = rng.integers(0, len(y), len(y))
        if len(set(y[idx].tolist())) < 2:
            continue
        for c, p in scores.items():
            for m, fn in fns.items():
                draws[c][m].append(float(fn(y[idx], p[idx])))

    def ci(values: list[float]) -> list[float]:
        return [
            round(float(np.percentile(values, 2.5)), 4),
            round(float(np.percentile(values, 97.5)), 4),
        ]

    out: dict[str, Any] = {
        "resamples": n,
        "baseline": baseline,
        "ci95": {},
        "diff_vs_baseline_ci95": {},
    }
    for c in scores:
        out["ci95"][c] = {m: ci(v) for m, v in draws[c].items()}
        if c != baseline:
            out["diff_vs_baseline_ci95"][c] = {
                m: ci([a - b for a, b in zip(draws[c][m], draws[baseline][m], strict=True)])
                for m in fns
            }
    return out


# -- candidates --------------------------------------------------------------------------


def run_baseline(
    split: dict[str, list[Item]], extra: dict[str, list[Item]] | None = None
) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    from scipy.sparse import hstack
    from sklearn.feature_extraction.text import HashingVectorizer
    from sklearn.linear_model import LogisticRegression

    words = HashingVectorizer(n_features=2**20, ngram_range=(1, 2), alternate_sign=False, norm="l2")
    chars = HashingVectorizer(
        n_features=2**20, analyzer="char_wb", ngram_range=(3, 5), alternate_sign=False, norm="l2"
    )

    def feats(items: list[Item]):
        texts = [i.text for i in items]
        return hstack([words.transform(texts), chars.transform(texts)]).tocsr()

    t = time.monotonic()
    x_fit = feats(split["fit"])
    y_fit = np.array([i.label for i in split["fit"]])
    model = LogisticRegression(C=4.0, max_iter=2000, class_weight="balanced").fit(x_fit, y_fit)
    train_s = time.monotonic() - t
    t = time.monotonic()
    s_val = model.decision_function(feats(split["val"]))
    s_eval = model.decision_function(feats(split["eval"]))
    infer_s = time.monotonic() - t
    n = len(split["val"]) + len(split["eval"])
    return (
        {
            "_extra": {k: model.decision_function(feats(v)) for k, v in (extra or {}).items()},
            "train_s": round(train_s, 1),
            "infer_ms_per_item": round(1000 * infer_s / n, 3),
            "device": "cpu",
            "params": "2x2^20 hashed features",
        },
        s_val,
        s_eval,
    )


def _torch_device():
    import torch

    return torch.device("mps" if torch.backends.mps.is_available() else "cpu")


def run_transformer(
    split: dict[str, list[Item]],
    model_id: str,
    *,
    lora: bool,
    epochs: int,
    lr: float,
    batch: int,
    max_len: int,
    seed: int = 0,
    extra: dict[str, list[Item]] | None = None,
):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.manual_seed(seed)
    device = _torch_device()
    tok = AutoTokenizer.from_pretrained(model_id)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    dtype = torch.bfloat16 if lora else torch.float32
    model = AutoModelForSequenceClassification.from_pretrained(
        model_id, num_labels=2, torch_dtype=dtype
    )
    model.config.pad_token_id = tok.pad_token_id
    trainable = sum(p.numel() for p in model.parameters())
    if lora:
        from peft import LoraConfig, get_peft_model

        cfg = LoraConfig(
            task_type="SEQ_CLS",
            r=16,
            lora_alpha=32,
            lora_dropout=0.05,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        )
        model = get_peft_model(model, cfg)
        trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    model.to(device)

    def batches(items: list[Item], shuffle: bool):
        idx = np.arange(len(items))
        if shuffle:
            np.random.default_rng(seed).shuffle(idx)
        for k in range(0, len(idx), batch):
            chunk = [items[j] for j in idx[k : k + batch]]
            enc = tok(
                [i.text for i in chunk],
                truncation=True,
                max_length=max_len,
                padding=True,
                return_tensors="pt",
            )
            yield (
                {k2: v.to(device) for k2, v in enc.items()},
                torch.tensor([i.label for i in chunk], device=device),
            )

    pos = sum(i.label for i in split["fit"])
    weight = torch.tensor(
        [1.0, (len(split["fit"]) - pos) / max(pos, 1)], device=device, dtype=torch.float32
    )
    opt = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad], lr=lr, weight_decay=0.01
    )
    t = time.monotonic()
    model.train()
    for _ in range(epochs):
        for enc, y in batches(split["fit"], shuffle=True):
            logits = model(**enc).logits.float()
            loss = torch.nn.functional.cross_entropy(logits, y, weight=weight)
            loss.backward()
            opt.step()
            opt.zero_grad()
    if device.type == "mps":
        torch.mps.synchronize()
    train_s = time.monotonic() - t

    @torch.no_grad()
    def score(items: list[Item]) -> np.ndarray:
        model.eval()
        out = []
        for enc, _ in batches(items, shuffle=False):
            logits = model(**enc).logits.float()
            out.append((logits[:, 1] - logits[:, 0]).cpu().numpy())
        return np.concatenate(out) if out else np.array([])

    t = time.monotonic()
    s_val, s_eval = score(split["val"]), score(split["eval"])
    infer_s = time.monotonic() - t
    n = len(split["val"]) + len(split["eval"])
    info = {
        "_extra": {k: score(v) for k, v in (extra or {}).items()},
        "model": model_id,
        "train_s": round(train_s, 1),
        "infer_ms_per_item": round(1000 * infer_s / n, 2),
        "device": device.type,
        "trainable_params": int(trainable),
        "epochs": epochs,
        "max_len": max_len,
        "batch": batch,
        "lr": lr,
        "dtype": str(dtype).replace("torch.", ""),
    }
    if device.type == "mps":
        info["mps_driver_allocated_gb"] = round(torch.mps.driver_allocated_memory() / 1e9, 2)
    if device.type == "mps":
        torch.mps.empty_cache()
    return info, s_val, s_eval


def run_frozen_embedding(split: dict[str, list[Item]], model_id: str, max_len: int, batch: int):
    import torch
    from sklearn.linear_model import LogisticRegression
    from transformers import AutoModel, AutoTokenizer

    device = _torch_device()
    tok = AutoTokenizer.from_pretrained(model_id, padding_side="left")
    model = AutoModel.from_pretrained(model_id, torch_dtype=torch.float32).to(device).eval()

    @torch.no_grad()
    def embed(items: list[Item]) -> np.ndarray:
        out = []
        for k in range(0, len(items), batch):
            enc = tok(
                [i.text for i in items[k : k + batch]],
                truncation=True,
                max_length=max_len,
                padding=True,
                return_tensors="pt",
            ).to(device)
            hidden = model(**enc).last_hidden_state[:, -1]  # last-token pooling (Qwen3-Embedding)
            out.append(torch.nn.functional.normalize(hidden, dim=-1).cpu().numpy())
        return np.concatenate(out)

    t = time.monotonic()
    e_fit = embed(split["fit"])
    embed_fit_s = time.monotonic() - t
    head = LogisticRegression(C=2.0, max_iter=4000, class_weight="balanced").fit(
        e_fit, np.array([i.label for i in split["fit"]])
    )
    t = time.monotonic()
    s_val = head.decision_function(embed(split["val"]))
    s_eval = head.decision_function(embed(split["eval"]))
    infer_s = time.monotonic() - t
    n = len(split["val"]) + len(split["eval"])
    if device.type == "mps":
        torch.mps.empty_cache()
    return (
        {
            "model": model_id,
            "train_s": round(embed_fit_s, 1),
            "infer_ms_per_item": round(1000 * infer_s / n, 2),
            "device": device.type,
            "trainable_params": int(e_fit.shape[1] + 1),
            "max_len": max_len,
            "note": "embedding fit time is mostly embedding the training set; head fit is seconds",
        },
        s_val,
        s_eval,
    )


# -- driver ------------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--corpus",
        type=Path,
        default=Path("~/.cache/nervecenter/corpora/spamassassin").expanduser(),
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cutoff", default="2002-10-01T00:00:00+00:00")
    ap.add_argument("--embargo-days", type=int, default=7)
    ap.add_argument("--only", default="A,B,C,D")
    ap.add_argument("--quick", action="store_true", help="tiny subset, for smoke testing")
    args = ap.parse_args()
    salt = hashlib.sha256(b"nerveshield-bench-6.3").digest()
    t0 = time.monotonic()
    items, stats = load_corpus(args.corpus, salt)
    if args.quick:
        items = items[::20]
    split = split_items(items, datetime.fromisoformat(args.cutoff), args.embargo_days)
    load_s = time.monotonic() - t0
    y_val = np.array([i.label for i in split["val"]])
    y_eval = np.array([i.label for i in split["eval"]])
    report: dict[str, Any] = {
        "task": "6.3",
        "corpus": "SpamAssassin public corpus (spam vs ham proxy)",
        "corpus_stats": stats,
        "load_and_split_s": round(load_s, 1),
        "split": {
            k: {
                "n": len(v),
                "positives": sum(i.label for i in v),
                "from": min(i.when for i in v).isoformat() if v else None,
                "to": max(i.when for i in v).isoformat() if v else None,
            }
            for k, v in split.items()
        },
        "campaigns": len({i.ckey for i in items}),
        "threads": len({i.tkey for i in items}),
        "environment": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "candidates": [],
    }
    try:
        import torch

        report["environment"]["torch"] = torch.__version__
        report["environment"]["mps"] = bool(torch.backends.mps.is_available())
    except ImportError:
        pass
    epochs = 1 if args.quick else 2
    runs = {
        "A": ("A_baseline_hashed_logreg", lambda: run_baseline(split)),
        "B": (
            "B_distilroberta_finetune",
            lambda: run_transformer(
                split,
                "distilbert/distilroberta-base",
                lora=False,
                epochs=epochs,
                lr=3e-5,
                batch=16,
                max_len=256,
            ),
        ),
        "C": (
            "C_qwen3_0.6b_lora",
            lambda: run_transformer(
                split, "Qwen/Qwen3-0.6B", lora=True, epochs=epochs, lr=2e-4, batch=8, max_len=256
            ),
        ),
        "D": (
            "D_qwen3_embedding_frozen_logreg",
            lambda: run_frozen_embedding(split, "Qwen/Qwen3-Embedding-0.6B", max_len=256, batch=16),
        ),
    }
    for key in args.only.split(","):
        name, fn = runs[key]
        info, s_val, s_eval = fn()
        result = calibrate_and_score(name, s_val, y_val, s_eval, y_eval)
        info.pop("_extra", None)
        result["run"] = info
        report["candidates"].append(result)
        partial = {
            **report,
            "candidates": [
                {k: v for k, v in c.items() if k != "_p_eval"} for c in report["candidates"]
            ],
        }
        args.out.write_text(json.dumps(partial, indent=1, default=str))
        print(name, json.dumps(result["eval"]), flush=True)
    scores = {c["candidate"]: c.pop("_p_eval") for c in report["candidates"]}
    if len(scores) > 1:
        base = next(iter(scores))
        report["bootstrap"] = paired_bootstrap(y_eval, scores, base)
    report["total_s"] = round(time.monotonic() - t0, 1)
    args.out.write_text(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
