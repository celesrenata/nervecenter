"""Pure-stdlib CPU baseline: hashed features + logistic regression (task 3.2).

* Features are hashed (``zlib.crc32``, stable across processes, unlike ``hash``)
  into a fixed-size space, so the model never stores raw feature strings.
* Training is plain SGD with L2, a fixed epoch count, and a shuffle order drawn
  from ``random.Random(seed)``: same data + seed => same parameters.
* Parameters serialize to JSON with a SHA-256 integrity digest. The digest is
  UNKEYED: it detects corruption/accidental edits, it does not authenticate who
  produced the file.
* Output is an UNCALIBRATED raw score (the logit margin). It is NOT a
  probability, must not be shown as one (R-007.2), and is never an authority
  (I-08). No calibration has been performed.
* Trained only on SYNTHETIC data in this task; see ``synthetic.py`` for limits.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import random
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from backend.mail_security.classifier.features import FeatureRecord, Label, LabeledExample
from backend.mail_security.models import ScoreSemantics

MODEL_FORMAT = "nervshield.cpu_baseline.v1"
DEFAULT_DIM = 4096
DEFAULT_EPOCHS = 8
DEFAULT_LEARNING_RATE = 0.1
DEFAULT_L2 = 1e-4
_WEIGHT_DECIMALS = 10

SCORE_NOTICE = (
    "UNCALIBRATED raw score (logit margin); not a probability, not a decision, "
    "not an authority. Trained on SYNTHETIC data only."
)


class IntegrityError(ValueError):
    """The serialized model does not match its integrity digest."""


@dataclass(frozen=True)
class Prediction:
    """One uncalibrated score. Deliberately has no probability field."""

    raw_score: float
    semantics: ScoreSemantics = ScoreSemantics.RAW_SCORE
    calibrated: bool = False


def _bucket(value: int, cap: int = 5) -> int:
    return min(value, cap)


def feature_strings(record: FeatureRecord) -> list[str]:
    """Flatten a record into namespaced feature strings (pre-hash)."""
    out = [f"ng:{g}" for g in record.url_domain_ngrams]
    out.extend(f"st:{t}" for t in record.sender_domain_tokens)
    for rule_id, count in sorted(record.finding_rule_counts.items()):
        out.append(f"r:{rule_id}")
        out.append(f"rc:{rule_id}:{_bucket(count, 3)}")
    out.append(f"u:{_bucket(record.url_count)}")
    out.append(f"d:{_bucket(record.distinct_url_domain_count)}")
    out.append(f"a:{_bucket(record.attachment_count)}")
    out.append(f"p:{_bucket(record.mime_part_count)}")
    out.append(f"i:{_bucket(record.remote_image_count)}")
    return out


def _vectorize(record: FeatureRecord, dim: int) -> dict[int, float]:
    vec: dict[int, float] = {}
    for text in feature_strings(record):
        idx = zlib.crc32(text.encode("ascii")) % dim
        vec[idx] = vec.get(idx, 0.0) + 1.0
    return vec


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _digest(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BaselineModel:
    """Trained hashed logistic-regression parameters + scoring."""

    def __init__(
        self,
        weights: dict[int, float],
        bias: float,
        dim: int,
        seed: int,
        epochs: int,
        train_count: int,
    ) -> None:
        self._weights = dict(weights)
        self._bias = bias
        self._dim = dim
        self._seed = seed
        self._epochs = epochs
        self._train_count = train_count
        self._digest = _digest(self._payload())

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def dim(self) -> int:
        return self._dim

    def predict(self, record: FeatureRecord) -> Prediction:
        score = self._bias
        for idx, value in _vectorize(record, self._dim).items():
            score += self._weights.get(idx, 0.0) * value
        return Prediction(raw_score=score)

    def predict_batch(self, records: Sequence[FeatureRecord]) -> list[Prediction]:
        return [self.predict(r) for r in records]

    def _payload(self) -> dict[str, Any]:
        return {
            "format": MODEL_FORMAT,
            "score_semantics": ScoreSemantics.RAW_SCORE.value,
            "calibrated": False,
            "score_notice": SCORE_NOTICE,
            "dim": self._dim,
            "seed": self._seed,
            "epochs": self._epochs,
            "train_count": self._train_count,
            "bias": self._bias,
            "weights": {str(i): w for i, w in sorted(self._weights.items())},
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["digest"] = self._digest
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BaselineModel:
        """Load parameters, verifying the digest before trusting any value."""
        stored = payload.get("digest")
        if not isinstance(stored, str) or not hmac.compare_digest(stored, _digest(payload)):
            raise IntegrityError("model digest mismatch")
        if payload.get("format") != MODEL_FORMAT:
            raise ValueError("unsupported model format")
        if payload.get("score_semantics") != "RAW_SCORE" or payload.get("calibrated") is not False:
            raise ValueError("model must declare an uncalibrated RAW_SCORE")
        dim = payload["dim"]
        weights = {int(k): float(v) for k, v in payload["weights"].items()}
        if not isinstance(dim, int) or dim <= 0 or any(not 0 <= i < dim for i in weights):
            raise ValueError("invalid weight index")
        return cls(
            weights,
            float(payload["bias"]),
            dim,
            int(payload["seed"]),
            int(payload["epochs"]),
            int(payload["train_count"]),
        )


def train_baseline(
    examples: Sequence[LabeledExample],
    seed: int,
    dim: int = DEFAULT_DIM,
    epochs: int = DEFAULT_EPOCHS,
    learning_rate: float = DEFAULT_LEARNING_RATE,
    l2: float = DEFAULT_L2,
) -> BaselineModel:
    """Deterministically train on labeled examples (both classes required)."""
    if not examples:
        raise ValueError("no training examples")
    labels = {e.label for e in examples}
    if labels != {Label.BENIGN, Label.MALICIOUS}:
        raise ValueError("training requires both classes")
    rows = [
        (_vectorize(e.features, dim), 1.0 if e.label is Label.MALICIOUS else 0.0) for e in examples
    ]
    rng = random.Random(seed)
    weights: dict[int, float] = {}
    bias = 0.0
    order = list(range(len(rows)))
    for _ in range(epochs):
        rng.shuffle(order)
        for n in order:
            vec, target = rows[n]
            z = bias + sum(weights.get(i, 0.0) * v for i, v in vec.items())
            err = _sigmoid(z) - target
            for i, v in vec.items():
                w = weights.get(i, 0.0)
                weights[i] = w - learning_rate * (err * v + l2 * w)
            bias -= learning_rate * err
    rounded = {i: round(w, _WEIGHT_DECIMALS) for i, w in weights.items()}
    return BaselineModel(
        {i: w for i, w in rounded.items() if w != 0.0},
        round(bias, _WEIGHT_DECIMALS),
        dim,
        seed,
        epochs,
        len(examples),
    )
