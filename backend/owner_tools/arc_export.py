"""Export 6.3 candidates B and C to OpenVINO and prepare an Arc iGPU run (owner tool).

Mac side only: retrains seed-0 B (DistilRoBERTa) and C (Qwen3-0.6B LoRA, merged)
on the public-corpus split, converts both to OpenVINO IR (fp16 weights),
pre-tokenises the eval set to fixed-length arrays, and records PyTorch reference
scores on exactly those arrays. The host side is ``arc_host_run.py``.

Network is blocked in-process (openvino-telemetry is installed with openvino).
Output directory holds model weights trained on public data only; keep it out
of git (default under ``~/.cache``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
from datetime import datetime
from pathlib import Path


def _block_network() -> None:
    def refuse(*_a, **_k):
        raise RuntimeError("network blocked during export")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]


def _convert(ov, torch, model, ids, mask, *, use_cache_off: bool):  # noqa: ANN001, ANN202
    """Trace a logits-only wrapper (HF outputs carry a KV cache the tracer rejects)."""

    class LogitsOnly(torch.nn.Module):
        def __init__(self, inner) -> None:  # noqa: ANN001
            super().__init__()
            self.inner = inner

        def forward(self, input_ids, attention_mask):  # noqa: ANN001, ANN202
            extra = {"use_cache": False} if use_cache_off else {}
            return self.inner(input_ids=input_ids, attention_mask=attention_mask, **extra).logits

    example = (torch.from_numpy(ids[:2]), torch.from_numpy(mask[:2]))
    ov_model = ov.convert_model(LogitsOnly(model).eval(), example_input=example)
    for port, name in zip(ov_model.inputs, ("input_ids", "attention_mask"), strict=True):
        port.get_tensor().set_names({name})
    return ov_model


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out", type=Path, default=Path("~/.cache/nervecenter/models/arc-export").expanduser()
    )
    ap.add_argument("--max-len", type=int, default=256)
    args = ap.parse_args()

    import numpy as np
    import torch

    from backend.owner_tools.model_bench import load_corpus, run_transformer, split_items

    salt = hashlib.sha256(b"nerveshield-bench-6.3").digest()
    items, _ = load_corpus(Path("~/.cache/nervecenter/corpora/spamassassin").expanduser(), salt)
    split = split_items(items, datetime.fromisoformat("2002-09-01T00:00:00+00:00"), 7)
    # Models are downloaded during training; block the network only for conversion.
    specs = {
        "B": ("distilbert/distilroberta-base", False, 3e-5, 16),
        "C": ("Qwen/Qwen3-0.6B", True, 2e-4, 8),
    }
    trained = {}
    for key, (model_id, lora, lr, batch) in specs.items():
        info, _, s_eval = run_transformer(
            split,
            model_id,
            lora=lora,
            epochs=2,
            lr=lr,
            batch=batch,
            max_len=args.max_len,
            seed=0,
            keep_model=True,
        )
        trained[key] = (info["_model"], s_eval)

    _block_network()
    import openvino as ov

    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    labels = np.array([i.label for i in split["eval"]], dtype=np.int8)
    meta: dict[str, object] = {
        "eval_n": int(len(labels)),
        "max_len": args.max_len,
        "candidates": {},
    }
    for key, ((model, tok), s_eval) in trained.items():
        if hasattr(model, "merge_and_unload"):
            model = model.merge_and_unload()
        model = model.to("cpu").to(torch.float32).eval()
        enc = tok(
            [i.text for i in split["eval"]],
            truncation=True,
            max_length=args.max_len,
            padding="max_length",
            return_tensors="np",
        )
        ids = enc["input_ids"].astype(np.int64)
        mask = enc["attention_mask"].astype(np.int64)
        with torch.no_grad():
            ref = []
            for k in range(0, len(ids), 32):
                logits = model(
                    input_ids=torch.from_numpy(ids[k : k + 32]),
                    attention_mask=torch.from_numpy(mask[k : k + 32]),
                ).logits
                ref.append((logits[:, 1] - logits[:, 0]).numpy())
        ref_scores = np.concatenate(ref)
        hf_dir = args.out / key / "hf"
        model.save_pretrained(hf_dir)  # lets a failed conversion be retried without retraining
        tok.save_pretrained(hf_dir)
        ov_model = _convert(ov, torch, model, ids, mask, use_cache_off="Qwen" in specs[key][0])
        ov_model.reshape({"input_ids": [-1, args.max_len], "attention_mask": [-1, args.max_len]})
        d = args.out / key
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        ov.save_model(ov_model, d / "model.xml", compress_to_fp16=True)
        np.savez(d / "inputs.npz", input_ids=ids, attention_mask=mask)
        np.save(d / "reference_cpu_fp32.npy", ref_scores.astype(np.float32))
        np.save(d / "reference_mps_training_run.npy", np.asarray(s_eval, dtype=np.float32))
        meta["candidates"][key] = {  # type: ignore[index]
            "model": specs[key][0],
            "lora_merged": specs[key][1],
            "xml_sha256": hashlib.sha256((d / "model.xml").read_bytes()).hexdigest(),
            "bin_bytes": (d / "model.bin").stat().st_size,
        }
    np.save(args.out / "labels.npy", labels)
    (args.out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
