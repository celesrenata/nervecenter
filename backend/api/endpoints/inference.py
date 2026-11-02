"""Inference provider registry endpoint for the web ``/inference`` view.

``GET /api/inference/providers`` returns the sanitized
``NERVE_INFERENCE_PROVIDERS`` entries (never a key value — see
:mod:`backend.inference.providers`) plus, when the live OmniRoute
transport is configured, an ``omniroute`` entry flagged ``default:
true``. When nothing is configured, ``data`` is an honest empty list —
the endpoint never fabricates providers.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.api.endpoints._helpers import list_response
from backend.api.models import ListResponse
from backend.inference.providers import (
    available_models,
    build_provider_pool,
    default_inference,
    omniroute_default_info,
)

router = APIRouter(prefix="/api/inference", tags=["inference"])


@router.get("/providers", response_model=ListResponse)
@router.get("/providers/", response_model=ListResponse)
async def list_providers() -> ListResponse:
    """Return the configured inference providers, sanitized.

    Each entry is ``{provider, model_id, base_url, key_configured,
    capabilities}``. When the live OmniRoute transport is configured an
    extra entry (``provider: "omniroute"``, ``default: true``) is
    appended after the env-configured ones.
    """
    return list_response(_provider_rows())


def _provider_rows() -> list[dict[str, Any]]:
    """The sanitized provider rows shared by ``/providers`` and ``/available``."""
    _, providers = build_provider_pool()
    data: list[dict[str, Any]] = [dict(entry) for entry in providers]
    omniroute_info = omniroute_default_info()
    if omniroute_info is not None:
        data.append(dict(omniroute_info))
    return data


@router.get("/available")
@router.get("/available/")
async def list_available() -> JSONResponse:
    """Return the selectable provider/model surface for inference (contract §1).

    Result: ``{"providers": [...], "models": [...], "default": {"provider",
    "model"}}``. ``providers`` are the sanitized ``NERVE_INFERENCE_PROVIDERS``
    entries plus the OmniRoute default; ``models`` is ``NERVE_INFER_MODELS``
    (else the configured provider ``model_id``s, else ``[]``); ``default`` is the
    ``NERVE_INFER_DEFAULT_*`` env (else ``omniroute`` / ``nerve-ask``). All honest
    — never a fabricated provider or model.
    """
    return JSONResponse(
        status_code=200,
        content={
            "accepted": True,
            "executed": True,
            "service": "inference",
            "request_id": str(uuid4()),
            "timestamp": datetime.now(UTC).isoformat(),
            "result": {
                "providers": _provider_rows(),
                "models": available_models(),
                "default": default_inference(),
            },
        },
    )
