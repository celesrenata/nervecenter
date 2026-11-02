"""Inference provider + combo ORM models (providers feature).

Backs the providers / combos surface the web flagged as missing:

- ``inference_providers`` — a durable, editable inference provider
  (``provider_type``, ``base_url``, the API key stored in-band, and the
  ``models`` JSONB list recalled from the live provider). The API key lives
  here so the test-connection and model-recall endpoints read it from the DB;
  it is *masked* on every read projection and only ever returned to an
  authenticated principal.
- ``inference_combos`` — a named group binding one provider to one model.
  The combo is the unit agents bind to (``agents.combo_id``); changing a
  combo's provider or model re-binds every agent pointing at it, so a single
  edit cascades to all agents.

Conventions match :mod:`incidents_agents`: identifier columns are
``String()`` (the ORM's ``UUID(as_uuid=False)`` maps cleanly onto them),
JSONB uses the :data:`Jsonb` wrapper, indexes follow ``ix_<table>_<column>``,
and timestamps use ``func.now()`` server defaults.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Jsonb, UUIDv4


class InferenceProvider(Base):
    """ORM mapping of a durable inference provider record."""

    __tablename__ = "inference_providers"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String, nullable=True)
    #: The provider API key. Stored in-band so the live test / recall
    #: endpoints can authenticate; always masked on read, never echoed.
    api_key: Mapped[str | None] = mapped_column(String, nullable=True)
    #: Capabilities, e.g. ``["generation", "embeddings"]``.
    capabilities: Mapped[list[str]] = mapped_column(Jsonb, default=list, nullable=False)
    #: Model ids recalled from the live provider (or hand-entered). Empty
    #: list when nothing has been recalled yet.
    models: Mapped[list[str]] = mapped_column(Jsonb, default=list, nullable=False)


class InferenceCombo(Base):
    """ORM mapping of a named provider+model combo bound to agents."""

    __tablename__ = "inference_combos"

    id: Mapped[UUIDv4]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    schema_version: Mapped[str] = mapped_column(String(16), default="v0")

    #: Human group handle; agents bind to this. Renamed in place — the
    #: re-point cascades to every bound agent on update.
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    provider_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    #: Optional note describing the intended use of this binding.
    note: Mapped[str | None] = mapped_column(String, nullable=True)
