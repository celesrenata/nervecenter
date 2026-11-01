"""Inference provider registry endpoint for the web ``/inference`` view.

``GET /api/inference/providers`` returns the sanitized
``NERVE_INFERENCE_PROVIDERS`` entries (never a key value — see
:mod:`backend.inference.providers`) plus, when the live OmniRoute
transport is configured, an ``omniroute`` entry flagged ``default:
true``. When nothing is configured, ``data`` is an honest empty list —
the endpoint never fabricates providers.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from backend.api.endpoints._helpers import list_response
from backend.api.models import ListResponse
from backend.inference.providers import build_provider_pool, omniroute_default_info

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
    _, providers = build_provider_pool()
    data: list[dict[str, Any]] = [dict(entry) for entry in providers]
    omniroute_info = omniroute_default_info()
    if omniroute_info is not None:
        data.append(dict(omniroute_info))
    return list_response(data)
