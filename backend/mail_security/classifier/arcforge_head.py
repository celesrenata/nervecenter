"""Embedding + linear head model ``arcforge_embed_head`` (NerveShield task 3.3, model half).

A pure-Python logistic-regression head trained on top of embeddings that the ArcForge
service produced for de-identified :class:`FeatureRecord` feature strings.

* Deterministic: plain SGD with L2, a fixed epoch count and a shuffle order drawn from
  ``random.Random(seed)``. Same embeddings + labels + seed => identical parameters.
* Parameters serialize to JSON with an UNKEYED SHA-256 integrity digest (detects
  corruption, does not authenticate the producer), like the CPU baseline.
* Output is an UNCALIBRATED raw score (the logit margin). It is NOT a probability and
  no calibration has been performed (R-007.2); calibration needs real labeled data.
  It is never an authority (I-08).
* Trained only on SYNTHETIC or owner-attested de-identified data (design §6: no
  training on historical mail).
* The payload binds the head to the embedder identity it was trained against
  (``embedder_model_id`` / ``embedder_version``) as REPORTED by the service.

Standard library only. Nothing here is wired into the worker, policy or pipeline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import random
from collections.abc import Sequence
from typing import Any

from backend.mail_security.classifier.baseline import SCORE_NOTICE, IntegrityError, Prediction
from backend.mail_security.models import ScoreSemantics

MODEL_FORMAT = "nervshield.arcforge_embed_head.v1"
HEAD_MODEL_ID = MODEL_FORMAT
DEFAULT_EPOCHS = 30
DEFAULT_LEARNING_RATE = 0.5
DEFAULT_L2 = 1e-3
_DECIMALS = 10

HEAD_NOTICE = (
    "UNCALIBRATED raw score (logit margin) of a linear head over service embeddings; not a "
    "probability, not a decision, not an authority. Trained on SYNTHETIC/attested data only."
)


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _digest(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EmbedHeadModel:
    """Trained linear-head parameters + scoring over fixed-dimension embeddings."""

    def __init__(
        self,
        weights: Sequence[float],
        bias: float,
        *,
        seed: int,
        epochs: int,
        learning_rate: float,
        l2: float,
        train_count: int,
        embedder_model_id: str,
        embedder_version: str,
    ) -> None:
        self._weights = tuple(float(w) for w in weights)
        self._bias = float(bias)
        self._seed = seed
        self._epochs = epochs
        self._learning_rate = learning_rate
        self._l2 = l2
        self._train_count = train_count
        self._embedder_model_id = embedder_model_id
        self._embedder_version = embedder_version
        self._digest = _digest(self._payload())

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def dim(self) -> int:
        return len(self._weights)

    @property
    def model_id(self) -> str:
        return HEAD_MODEL_ID

    def predict(self, embedding: Sequence[float]) -> Prediction:
        if len(embedding) != len(self._weights):
            raise ValueError("embedding dimension does not match the head")
        score = self._bias
        for w, x in zip(self._weights, embedding, strict=True):
            score += w * x
        if not math.isfinite(score):
            raise ValueError("non-finite score")
        return Prediction(raw_score=score)

    def predict_batch(self, embeddings: Sequence[Sequence[float]]) -> list[Prediction]:
        return [self.predict(e) for e in embeddings]

    def _payload(self) -> dict[str, Any]:
        return {
            "format": MODEL_FORMAT,
            "score_semantics": ScoreSemantics.RAW_SCORE.value,
            "calibrated": False,
            "score_notice": HEAD_NOTICE,
            "dim": len(self._weights),
            "seed": self._seed,
            "epochs": self._epochs,
            "learning_rate": self._learning_rate,
            "l2": self._l2,
            "train_count": self._train_count,
            "embedder_model_id": self._embedder_model_id,
            "embedder_version": self._embedder_version,
            "bias": self._bias,
            "weights": list(self._weights),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["digest"] = self._digest
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EmbedHeadModel:
        """Load parameters, verifying the digest before trusting any value."""
        stored = payload.get("digest")
        if not isinstance(stored, str) or not hmac.compare_digest(stored, _digest(payload)):
            raise IntegrityError("model digest mismatch")
        if payload.get("format") != MODEL_FORMAT:
            raise ValueError("unsupported model format")
        if payload.get("score_semantics") != "RAW_SCORE" or payload.get("calibrated") is not False:
            raise ValueError("model must declare an uncalibrated RAW_SCORE")
        weights = payload["weights"]
        if not isinstance(weights, list) or len(weights) != payload["dim"] or not weights:
            raise ValueError("invalid weights")
        if any(not isinstance(w, int | float) or not math.isfinite(w) for w in weights):
            raise ValueError("invalid weights")
        return cls(
            weights,
            float(payload["bias"]),
            seed=int(payload["seed"]),
            epochs=int(payload["epochs"]),
            learning_rate=float(payload["learning_rate"]),
            l2=float(payload["l2"]),
            train_count=int(payload["train_count"]),
            embedder_model_id=str(payload["embedder_model_id"]),
            embedder_version=str(payload["embedder_version"]),
        )


def train_head(
    embeddings: Sequence[Sequence[float]],
    labels: Sequence[int],
    seed: int,
    *,
    embedder_model_id: str,
    embedder_version: str,
    epochs: int = DEFAULT_EPOCHS,
    learning_rate: float = DEFAULT_LEARNING_RATE,
    l2: float = DEFAULT_L2,
) -> EmbedHeadModel:
    """Deterministically train on embeddings with 0/1 labels (both classes required)."""
    if not embeddings or len(embeddings) != len(labels):
        raise ValueError("embeddings and labels must be non-empty and equally long")
    if set(labels) != {0, 1}:
        raise ValueError("training requires both classes")
    if epochs < 1 or not learning_rate > 0 or l2 < 0:
        raise ValueError("invalid training hyper-parameters")
    dim = len(embeddings[0])
    if dim < 1 or any(len(e) != dim for e in embeddings):
        raise ValueError("embeddings must share one positive dimension")
    if any(not math.isfinite(v) for e in embeddings for v in e):
        raise ValueError("embeddings must be finite")
    rng = random.Random(seed)
    weights = [0.0] * dim
    bias = 0.0
    order = list(range(len(embeddings)))
    for _ in range(epochs):
        rng.shuffle(order)
        for n in order:
            x = embeddings[n]
            z = bias + sum(w * v for w, v in zip(weights, x, strict=True))
            err = _sigmoid(z) - float(labels[n])
            for i in range(dim):
                weights[i] -= learning_rate * (err * x[i] + l2 * weights[i])
            bias -= learning_rate * err
    return EmbedHeadModel(
        [round(w, _DECIMALS) for w in weights],
        round(bias, _DECIMALS),
        seed=seed,
        epochs=epochs,
        learning_rate=learning_rate,
        l2=l2,
        train_count=len(embeddings),
        embedder_model_id=embedder_model_id,
        embedder_version=embedder_version,
    )


__all__ = [
    "HEAD_MODEL_ID",
    "HEAD_NOTICE",
    "SCORE_NOTICE",
    "EmbedHeadModel",
    "train_head",
]
