"""NerveShield Task 4.1: strict SemanticObservation contract.

Proves the pydantic model and ``semantic-observation.schema.json`` stay in
sync and that untrusted model output is rejected when it carries unknown
keys, overlong strings, invalid confidence, or omits required fields.
Fixtures only; no model, network, or mail content.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backend.mail_security import SemanticObservation

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / ".kiro/specs/nerve-shield/schemas/semantic-observation.schema.json"
)
SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text())
_MODEL_SCHEMA: dict[str, Any] = SemanticObservation.model_json_schema()


def _payload() -> dict[str, Any]:
    """A fully populated, valid observation payload (JSON-shaped)."""
    component = {
        "evidence_ref": "ev:link:1",
        "risk_level": "LOW",
        "indicator_codes": ["MISMATCHED_HOST"],
        "model_confidence": 0.5,
    }
    return {
        "schema_version": "1.0",
        "source_ref": "src:msg:1",
        "observer_role": "TEST_FIXTURE",
        "gist": "short summary",
        "claimed_identity": {
            "claimed_role": "BUSINESS",
            "entity_refs": ["ent:1"],
            "identity_consistency": "INCONSISTENT",
        },
        "message_category": "BILL_OR_INVOICE",
        "requests": [
            {
                "request_type": "PAY",
                "claimed_urgency": "HIGH",
                "evidence_refs": ["ev:1"],
                "high_impact": True,
                "requires_independent_verification": True,
            }
        ],
        "links": [component],
        "images": [dict(component, evidence_ref="ev:img:1")],
        "attachments": [dict(component, evidence_ref="ev:att:1")],
        "risk_indicators": [
            {"indicator_code": "URGENCY", "evidence_refs": ["ev:1"], "model_confidence": None}
        ],
        "contradictions": [{"contradiction_code": "NAME_VS_ADDR", "evidence_refs": ["ev:1"]}],
        "missing_evidence": ["ORIGINAL_HEADERS"],
        "evidence_refs": ["ev:1", "ev:2"],
    }


def _paths() -> list[tuple[str, ...]]:
    """Locations of every object in the payload, for unknown-key injection."""
    return [
        (),
        ("claimed_identity",),
        ("requests", "0"),
        ("links", "0"),
        ("images", "0"),
        ("attachments", "0"),
        ("risk_indicators", "0"),
        ("contradictions", "0"),
    ]


def _walk(payload: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    node: Any = payload
    for part in path:
        node = node[int(part)] if part.isdigit() else node[part]
    return node


def test_valid_payload_accepted() -> None:
    obs = SemanticObservation.model_validate(_payload())
    assert obs.model_dump(mode="json") == _payload()


# -- extra fields forbidden --------------------------------------------------


@pytest.mark.parametrize("path", _paths(), ids=lambda p: "/".join(p) or "root")
@pytest.mark.parametrize(
    "key", ["tool_call", "instructions", "action", "system_prompt", "__proto__"]
)
def test_unknown_keys_rejected_at_every_level(path: tuple[str, ...], key: str) -> None:
    payload = _payload()
    _walk(payload, path)[key] = "do something"
    with pytest.raises(ValidationError):
        SemanticObservation.model_validate(payload)


# -- overlong strings and oversize collections rejected -----------------------


def test_gist_boundary() -> None:
    ok = _payload()
    ok["gist"] = "x" * 600
    SemanticObservation.model_validate(ok)
    bad = _payload()
    bad["gist"] = "x" * 601
    with pytest.raises(ValidationError):
        SemanticObservation.model_validate(bad)


@pytest.mark.parametrize(
    ("path", "field", "limit"),
    [
        ((), "source_ref", 200),
        (("links", "0"), "evidence_ref", 200),
        (("links", "0", "indicator_codes"), "0", 80),
        (("risk_indicators", "0"), "indicator_code", 80),
        (("contradictions", "0"), "contradiction_code", 80),
        ((), "missing_evidence", 80),
        ((), "evidence_refs", 200),
    ],
)
def test_overlong_bounded_strings_rejected(path: tuple[str, ...], field: str, limit: int) -> None:
    payload = _payload()
    node: Any = _walk(payload, path)
    long_value = "A" * (limit + 1)
    if field == "0":
        node[0] = long_value
    elif isinstance(node[field], list):
        node[field] = [long_value]
    else:
        node[field] = long_value
    with pytest.raises(ValidationError):
        SemanticObservation.model_validate(payload)


@pytest.mark.parametrize("value", ["has space", "a@b.example", "line\nbreak", "", "<script>"])
def test_free_text_in_opaque_refs_rejected(value: str) -> None:
    for field in ("source_ref",):
        payload = _payload()
        payload[field] = value
        with pytest.raises(ValidationError):
            SemanticObservation.model_validate(payload)
    payload = _payload()
    payload["evidence_refs"] = [value]
    with pytest.raises(ValidationError):
        SemanticObservation.model_validate(payload)


@pytest.mark.parametrize("value", ["lower_case", "1STARTS_DIGIT", "HAS SPACE", ""])
def test_codes_must_be_upper_snake(value: str) -> None:
    payload = _payload()
    payload["missing_evidence"] = [value]
    with pytest.raises(ValidationError):
        SemanticObservation.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("requests", 32),
        ("links", 128),
        ("images", 128),
        ("attachments", 128),
        ("risk_indicators", 64),
        ("contradictions", 24),
        ("missing_evidence", 64),
        ("evidence_refs", 256),
    ],
)
def test_collection_limits_match_schema(field: str, limit: int) -> None:
    assert SCHEMA["properties"][field]["maxItems"] == limit
    payload = _payload()
    template = copy.deepcopy(payload[field][0]) if payload[field] else None
    if field == "missing_evidence":
        payload[field] = [f"CODE_{i}" for i in range(limit + 1)]
    elif field == "evidence_refs":
        payload[field] = [f"ev:{i}" for i in range(limit + 1)]
    else:
        assert template is not None
        payload[field] = [copy.deepcopy(template) for _ in range(limit + 1)]
    with pytest.raises(ValidationError):
        SemanticObservation.model_validate(payload)


def test_duplicate_refs_rejected() -> None:
    payload = _payload()
    payload["evidence_refs"] = ["ev:1", "ev:1"]
    with pytest.raises(ValidationError):
        SemanticObservation.model_validate(payload)


# -- invalid confidence rejected ---------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [-0.0001, 1.0001, 2, -1, math.nan, math.inf, -math.inf, "0.5", True, False, [0.5], {"v": 1}],
)
def test_invalid_confidence_rejected(bad: object) -> None:
    for where in (("links", "0"), ("images", "0"), ("attachments", "0"), ("risk_indicators", "0")):
        payload = _payload()
        _walk(payload, where)["model_confidence"] = bad
        with pytest.raises(ValidationError):
            SemanticObservation.model_validate(payload)


@pytest.mark.parametrize("good", [0, 0.0, 0.5, 1, 1.0, None])
def test_valid_confidence_accepted(good: float | None) -> None:
    payload = _payload()
    payload["links"][0]["model_confidence"] = good
    SemanticObservation.model_validate(payload)


def test_confidence_is_required_field() -> None:
    payload = _payload()
    del payload["links"][0]["model_confidence"]
    with pytest.raises(ValidationError):
        SemanticObservation.model_validate(payload)


def test_non_bool_flags_rejected() -> None:
    for field in ("high_impact", "requires_independent_verification"):
        for bad in ("yes", "true", 1, 0, None):
            payload = _payload()
            payload["requests"][0][field] = bad
            with pytest.raises(ValidationError):
                SemanticObservation.model_validate(payload)


# -- required fields, enums, version ------------------------------------------


@pytest.mark.parametrize("field", SCHEMA["required"])
def test_every_schema_required_field_is_required(field: str) -> None:
    payload = _payload()
    del payload[field]
    with pytest.raises(ValidationError):
        SemanticObservation.model_validate(payload)


@pytest.mark.parametrize("version", ["1.1", "2.0", "", "1"])
def test_schema_version_pinned(version: str) -> None:
    payload = _payload()
    payload["schema_version"] = version
    with pytest.raises(ValidationError):
        SemanticObservation.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ((), "observer_role"),
        ((), "message_category"),
        (("claimed_identity",), "claimed_role"),
        (("claimed_identity",), "identity_consistency"),
        (("requests", "0"), "request_type"),
        (("requests", "0"), "claimed_urgency"),
        (("links", "0"), "risk_level"),
    ],
)
def test_unknown_enum_values_rejected(path: tuple[str, ...], field: str) -> None:
    payload = _payload()
    _walk(payload, path)[field] = "EXECUTE_TOOL"
    with pytest.raises(ValidationError):
        SemanticObservation.model_validate(payload)


# -- schema file and pydantic model in sync -----------------------------------


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in node:
        return _resolve(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
    return node


def _canon(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Reduce a JSON schema node to the constraint facets we compare."""
    node = _resolve(node, defs)
    out: dict[str, Any] = {}
    if "const" in node:
        out["const"] = node["const"]
    if "enum" in node:
        out["enum"] = sorted(node["enum"])
    if "enum" in out:
        return out  # pydantic adds a redundant "type": "string" next to enum
    # pydantic renders Optional[float] as anyOf [number, null]; schema uses a type list.
    if "anyOf" in node:
        branches = [_canon(b, defs) for b in node["anyOf"]]
        types = sorted(b["type"] for b in branches)
        out["type"] = types
        for b in branches:
            for k, v in b.items():
                if k != "type":
                    out[k] = v
    elif "type" in node:
        out["type"] = sorted(node["type"]) if isinstance(node["type"], list) else node["type"]
    for key in (
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "maxItems",
        "uniqueItems",
    ):
        if key in node:
            out[key] = node[key]
    if node.get("type") == "object" or "properties" in node:
        out["additionalProperties"] = node.get("additionalProperties", True)
        out["required"] = sorted(node.get("required", []))
        out["properties"] = {
            name: _canon(sub, defs) for name, sub in node.get("properties", {}).items()
        }
    if "items" in node:
        out["items"] = _canon(node["items"], defs)
    return out


def _model_canon() -> dict[str, Any]:
    return _canon(_MODEL_SCHEMA, _MODEL_SCHEMA.get("$defs", {}))


def _file_canon() -> dict[str, Any]:
    return _canon(SCHEMA, SCHEMA["$defs"])


def _strip_known_representation_gaps(node: Any) -> Any:
    """Drop facets pydantic cannot express in JSON schema.

    ``uniqueItems`` is enforced by model validators (not emitted by pydantic) and
    is covered by ``test_duplicate_refs_rejected``; ``schema_version`` const is
    a pydantic validator, represented as a plain bounded string in its schema.
    """
    if isinstance(node, dict):
        node = {k: _strip_known_representation_gaps(v) for k, v in node.items()}
        node.pop("uniqueItems", None)
        if "const" in node:
            node.pop("const")
            node["type"] = "string"
    return node


def test_schema_file_is_strict_everywhere() -> None:
    def check(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(SCHEMA)


def test_model_and_schema_file_in_sync() -> None:
    file_c = _strip_known_representation_gaps(_file_canon())
    model_c = _strip_known_representation_gaps(_model_canon())
    assert model_c == file_c


def test_unique_items_in_schema_are_enforced_by_model() -> None:
    """Every uniqueItems array in the schema file rejects duplicates in the model."""
    assert SCHEMA["properties"]["evidence_refs"]["uniqueItems"] is True
    assert SCHEMA["properties"]["missing_evidence"]["uniqueItems"] is True
    cases = [
        (("claimed_identity",), "entity_refs", "ent:1"),
        (("requests", "0"), "evidence_refs", "ev:1"),
        (("links", "0"), "indicator_codes", "CODE_A"),
        (("risk_indicators", "0"), "evidence_refs", "ev:1"),
        (("contradictions", "0"), "evidence_refs", "ev:1"),
        ((), "missing_evidence", "CODE_A"),
        ((), "evidence_refs", "ev:1"),
    ]
    for path, field, dup in cases:
        payload = _payload()
        _walk(payload, path)[field] = [dup, dup]
        with pytest.raises(ValidationError):
            SemanticObservation.model_validate(payload)


def test_schema_enums_match_model_enums() -> None:
    from backend.mail_security import models

    pairs = [
        (SCHEMA["properties"]["observer_role"]["enum"], models.ObserverRole),
        (SCHEMA["properties"]["message_category"]["enum"], models.MessageCategory),
        (
            SCHEMA["properties"]["claimed_identity"]["properties"]["claimed_role"]["enum"],
            models.ClaimedRole,
        ),
        (SCHEMA["$defs"]["request"]["properties"]["request_type"]["enum"], models.RequestType),
        (
            SCHEMA["$defs"]["request"]["properties"]["claimed_urgency"]["enum"],
            models.RequestUrgency,
        ),
        (
            SCHEMA["$defs"]["component"]["properties"]["risk_level"]["enum"],
            models.ComponentRiskLevel,
        ),
    ]
    for schema_values, enum_cls in pairs:
        assert sorted(schema_values) == sorted(m.value for m in enum_cls)
