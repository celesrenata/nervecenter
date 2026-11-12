"""Host-side Arc iGPU run for exported candidates (owner tool, runs on a gremlin).

Executed with the host's ArcForge Python environment (OpenVINO + numpy only) inside
a sandboxed ``systemd-run``. Reads ``<dir>/<cand>/model.xml`` and ``inputs.npz``,
compiles on the given device, writes ``arc_scores.npy`` and prints timing JSON.
No network, no other files touched.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import openvino as ov


def main() -> None:
    root, device, cand = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    d = root / cand
    core = ov.Core()
    t = time.monotonic()
    compiled = core.compile_model(str(d / "model.xml"), device, {"PERFORMANCE_HINT": "LATENCY"})
    compile_s = time.monotonic() - t
    data = np.load(d / "inputs.npz")
    ids, mask = data["input_ids"], data["attention_mask"]
    req = compiled.create_infer_request()
    out = compiled.output(0)

    def run(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        logits = req.infer({"input_ids": a, "attention_mask": b})[out]
        return logits[:, 1] - logits[:, 0]

    run(ids[:1], mask[:1])  # warm-up
    lat = []
    for k in range(min(100, len(ids))):
        t = time.monotonic()
        run(ids[k : k + 1], mask[k : k + 1])
        lat.append(time.monotonic() - t)
    scores = []
    t = time.monotonic()
    for k in range(0, len(ids), 16):
        scores.append(run(ids[k : k + 16], mask[k : k + 16]))
    batch_s = time.monotonic() - t
    np.save(d / "arc_scores.npy", np.concatenate(scores).astype(np.float32))
    lat_ms = np.array(lat) * 1000
    print(
        json.dumps(
            {
                "candidate": cand,
                "device": device,
                "device_name": core.get_property(device, "FULL_DEVICE_NAME"),
                "openvino": ov.__version__,
                "compile_s": round(compile_s, 1),
                "latency_ms_batch1": {
                    "p50": round(float(np.median(lat_ms)), 2),
                    "p95": round(float(np.percentile(lat_ms, 95)), 2),
                },
                "throughput_items_per_s_batch16": round(len(ids) / batch_s, 1),
                "n": int(len(ids)),
            }
        )
    )


if __name__ == "__main__":
    main()
