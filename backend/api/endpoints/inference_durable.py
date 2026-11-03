"""Durable inference provider + combo endpoints (providers feature).

Companion to the read-only, env-driven
:mod:`backend.api.endpoints.inference` surface. Where that module answers
``GET /api/inference/providers`` from the ``NERVE_INFERENCE_PROVIDERS``
environment variable, this module serves the *persistent* registry:

- ``/api/inference/durable/providers`` — CRUD over ``inference_providers``
  (the ``api_key`` is stored in-band but every projection masks it).
- ``/api/inference/durable/providers/{id}/test`` — live test-connection probe.
- ``/api/inference/durable/providers/{id}/models`` — recall + persist the
  provider's model list.
- ``/api/inference/durable/combos`` — CRUD over ``inference_combos``.

Auth and degradation mirror the house convention: mutating routes are gated
by ``Depends(current_access)`` (401 without a bearer token); when no DB
engine is wired the routes answer an honest ``503`` — never a fabricated
acceptance. Read routes degrade to a 200 empty list (the providers list is
empty until someone creates one).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.auth import current_access
from backend.persistence.models import Agent, InferenceCombo, InferenceProvider
from backend.services.inference_providers import (
    ComboNameConflictError,
    ComboNotFoundError,
    InferenceComboService,
    InferenceProviderService,
    ProviderNameConflictError,
    ProviderNotFoundError,
)

router = APIRouter(prefix="/api/inference/durable", tags=["inference"])

#: Supported provider types (mirrors :mod:`backend.inference.adapter`).
_VALID_PROVIDER_TYPES = {"openai", "anthropic", "local", "huggingface"}


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ProviderCreateRequest(BaseModel):
    """Payload for ``POST /api/inference/durable/providers``.

    ``name`` is unique across the provider table; ``provider_type`` selects the
    adapter family; ``api_key`` is stored in-band (masked on read).
    """

    name: str = Field(..., min_length=1, description="Unique provider name")
    provider_type: str = Field(..., min_length=1, description="Adapter family")
    base_url: str | None = Field(default=None, description="Endpoint base URL")
    api_key: str | None = Field(default=None, description="API key (stored, masked on read)")
    capabilities: list[str] = Field(default_factory=list, description="Capability tags")
    models: list[str] = Field(default_factory=list, description="Known model ids")


class ProviderUpdateRequest(BaseModel):
    """Payload for ``PATCH /api/inference/durable/providers/{id}``.

    ``None`` fields leave the existing value unchanged (partial update). A
    supplied empty ``api_key`` clears the stored key.
    """

    name: str | None = Field(default=None, min_length=1)
    provider_type: str | None = Field(default=None, min_length=1)
    base_url: str | None = Field(default=None)
    api_key: str | None = Field(default=None)
    capabilities: list[str] | None = Field(default=None)
    models: list[str] | None = Field(default=None)


class ProviderProbeRequest(BaseModel):
    """Payload for ``POST /api/inference/durable/providers/probe``.

    Ad-hoc pre-save probe: run the live models-endpoint probe with transient
    (unpersisted) credentials so a provider's model list can be discovered
    before the provider row is created.
    """

    provider_type: str = Field(..., min_length=1, description="Adapter family")
    base_url: str | None = Field(default=None, description="Endpoint base URL")
    api_key: str | None = Field(default=None, description="API key (not persisted)")


class ComboCreateRequest(BaseModel):
    """Payload for ``POST /api/inference/durable/combos``.

    A combo binds a provider to a model; agents point at a combo via
    ``agents.combo_id``.
    """

    name: str = Field(..., min_length=1, description="Unique combo name")
    provider_id: str = Field(..., min_length=1, description="Provider id")
    model_id: str = Field(..., min_length=1, description="Model id for this combo")
    note: str | None = Field(default=None, description="Optional note")


class ComboUpdateRequest(BaseModel):
    """Payload for ``PATCH /api/inference/durable/combos/{id}``."""

    name: str | None = Field(default=None, min_length=1)
    provider_id: str | None = Field(default=None, min_length=1)
    model_id: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Service resolution
# ---------------------------------------------------------------------------


def _provider_service(request: Request) -> InferenceProviderService | None:
    """Resolve the durable-provider service for this request (or None)."""
    services = getattr(request.app.state, "services", {})
    svc = services.get("inference_providers") if isinstance(services, dict) else None
    if svc is not None:
        return svc
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        return None
    state = request.app.state
    memo = getattr(state, "_inference_providers_service", None)
    if memo is None:
        memo = InferenceProviderService(engine)
        setattr(state, "_inference_providers_service", memo)
    return memo


def _combo_service(request: Request) -> InferenceComboService | None:
    """Resolve the durable-combo service for this request (or None)."""
    services = getattr(request.app.state, "services", {})
    svc = services.get("inference_combos") if isinstance(services, dict) else None
    if svc is not None:
        return svc
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        return None
    state = request.app.state
    memo = getattr(state, "_inference_combos_service", None)
    if memo is None:
        memo = InferenceComboService(engine)
        setattr(state, "_inference_combos_service", memo)
    return memo


def _require_provider_service(request: Request) -> InferenceProviderService:
    svc = _provider_service(request)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Inference provider persistence is not configured; set NERVE_DB_URL.",
        )
    return svc


def _require_combo_service(request: Request) -> InferenceComboService:
    svc = _combo_service(request)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Inference combo persistence is not configured; set NERVE_DB_URL.",
        )
    return svc


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@router.get("/providers")
@router.get("/providers/")
async def list_providers(request: Request) -> list[dict[str, Any]]:
    """List durable providers (api_key masked). 503 when no DB engine."""
    svc = _require_provider_service(request)
    return await svc.list()


@router.get("/providers/{provider_id}")
async def get_provider(provider_id: str, request: Request) -> dict[str, Any]:
    """Fetch one durable provider by id (api_key masked). 404 when unknown."""
    svc = _require_provider_service(request)
    try:
        return await svc.get(provider_id)
    except ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/providers", status_code=201)
async def create_provider(
    request: Request,
    payload: ProviderCreateRequest,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Create a durable provider (auth-gated). 409 on name conflict."""
    if payload.provider_type not in _VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported provider_type '{payload.provider_type}'",
        )
    svc = _require_provider_service(request)
    try:
        return await svc.create(
            name=payload.name,
            provider_type=payload.provider_type,
            base_url=payload.base_url,
            api_key=payload.api_key,
            capabilities=payload.capabilities,
            models=payload.models,
        )
    except ProviderNameConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"A provider named '{payload.name}' already exists.",
        ) from exc


@router.post("/providers/probe")
async def probe_provider(
    request: Request,
    payload: ProviderProbeRequest,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Probe a provider's models endpoint with unpersisted credentials.

    Returns the same shape as the per-provider ``/test`` endpoint:
    ``{"success", "models_found", "models", "error", "latency_ms"}``.
    Nothing is written to the registry. 422 on an unsupported provider_type.
    """
    if payload.provider_type not in _VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported provider_type '{payload.provider_type}'",
        )
    svc = _require_provider_service(request)
    try:
        return await svc.probe_ad_hoc(
            provider_type=payload.provider_type,
            base_url=payload.base_url,
            api_key=payload.api_key,
        )
    except ProviderNotFoundError as exc:  # defensive: probe never needs a row
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    request: Request,
    payload: ProviderUpdateRequest,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Partially update a durable provider (auth-gated). 404 / 409 on error."""
    if payload.provider_type is not None and payload.provider_type not in _VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported provider_type '{payload.provider_type}'",
        )
    svc = _require_provider_service(request)
    try:
        return await svc.update(
            provider_id,
            name=payload.name,
            provider_type=payload.provider_type,
            base_url=payload.base_url,
            api_key=payload.api_key,
            capabilities=payload.capabilities,
            models=payload.models,
        )
    except ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderNameConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"A provider named '{payload.name}' already exists.",
        ) from exc


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: str,
    request: Request,
    claims: dict[str, Any] = Depends(current_access),
) -> None:
    """Delete a durable provider (auth-gated). 404 when unknown."""
    svc = _require_provider_service(request)
    try:
        await svc.delete(provider_id)
    except ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/providers/{provider_id}/test")
async def test_provider_connection(
    provider_id: str,
    request: Request,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Live test-connection probe (auth-gated). Returns success/models/error."""
    svc = _require_provider_service(request)
    try:
        return await svc.test_connection(provider_id)
    except ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/providers/{provider_id}/models")
async def recall_provider_models(
    provider_id: str,
    request: Request,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Recall the provider's model list and persist it (auth-gated)."""
    svc = _require_provider_service(request)
    try:
        return await svc.recall_models(provider_id)
    except ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Combos
# ---------------------------------------------------------------------------


@router.get("/combos")
@router.get("/combos/")
async def list_combos(request: Request) -> list[dict[str, Any]]:
    """List durable combos with provider names. 503 when no DB engine."""
    svc = _require_combo_service(request)
    return await svc.list()


@router.get("/combos/{combo_id}")
async def get_combo(combo_id: str, request: Request) -> dict[str, Any]:
    """Fetch one durable combo by id. 404 when unknown."""
    svc = _require_combo_service(request)
    try:
        return await svc.get(combo_id)
    except ComboNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/combos/{combo_id}/agents")
async def list_combo_agents(combo_id: str, request: Request) -> list[dict[str, Any]]:
    """List the agents bound to a combo (cascade preview)."""
    svc = _require_combo_service(request)
    try:
        return await svc.list_bound_agents(combo_id)
    except ComboNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/combos", status_code=201)
async def create_combo(
    request: Request,
    payload: ComboCreateRequest,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Create a durable combo (auth-gated). 404 on unknown provider, 409 on name."""
    svc = _require_combo_service(request)
    try:
        return await svc.create(
            name=payload.name,
            provider_id=payload.provider_id,
            model_id=payload.model_id,
            note=payload.note,
        )
    except ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ComboNameConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"A combo named '{payload.name}' already exists.",
        ) from exc


@router.patch("/combos/{combo_id}")
async def update_combo(
    combo_id: str,
    request: Request,
    payload: ComboUpdateRequest,
    claims: dict[str, Any] = Depends(current_access),
) -> dict[str, Any]:
    """Partially update a durable combo (auth-gated). 404 / 409 on error."""
    svc = _require_combo_service(request)
    try:
        return await svc.update(
            combo_id,
            name=payload.name,
            provider_id=payload.provider_id,
            model_id=payload.model_id,
            note=payload.note,
        )
    except ComboNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ComboNameConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"A combo named '{payload.name}' already exists.",
        ) from exc


@router.delete("/combos/{combo_id}", status_code=204)
async def delete_combo(
    combo_id: str,
    request: Request,
    claims: dict[str, Any] = Depends(current_access),
) -> None:
    """Delete a durable combo and unbind all bound agents (auth-gated)."""
    svc = _require_combo_service(request)
    try:
        await svc.delete(combo_id)
    except ComboNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
