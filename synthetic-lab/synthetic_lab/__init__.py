"""T003-05 synthetic lab — versioned fixture-only evaluation corpus.

AC10 (T003.AC10): labeled detection cases (recall/precision per §72),
attention scoring cases (§21/§58/§59), and ask QA pairs (ground truth for
T003.AC9). Replay is deterministic (fixed anchor + seed, no network, no
secrets); the evaluator reports pass/fail with evidence and a nonzero
exit on regression. Ask cases are ``pending`` while the AC9
recorded-fixture interface is absent — no results are invented.

Modules
-------
- ``schema``   — corpus models + loader (``corpus.json``)
- ``replay``   — deterministic replay against the in-memory planes
- ``evaluator``— pass/fail + evidence + precision/recall + regression gate
- ``cli``      — ``python -m synthetic_lab`` (or ``synthetic-lab/run.py``)
"""

from .evaluator import EvaluationReport, evaluate, run_evaluation
from .replay import replay_corpus
from .schema import (
    CORPUS_SCHEMA_VERSION,
    SyntheticCorpus,
    default_corpus_path,
    load_corpus,
    parse_corpus,
)

__all__ = [
    "CORPUS_SCHEMA_VERSION",
    "EvaluationReport",
    "default_corpus_path",
    "evaluate",
    "load_corpus",
    "parse_corpus",
    "replay_corpus",
    "run_evaluation",
    "SyntheticCorpus",
]
