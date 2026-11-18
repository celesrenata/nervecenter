"""Durable voicemails and reconstructed threads (voicemail-persist).

Backs :class:`backend.voicemail.service.VoicemailService`. The in-memory service
stays the read model; these rows are what survives an API restart.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class VoicemailThreadRow(Base):
    """One thread per caller number (``vthr-<digits>``)."""

    __tablename__ = "voicemail_threads"

    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    caller_phone: Mapped[str] = mapped_column(String(64), nullable=False)
    caller_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    voicemail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unread: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    needs_callback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    callback_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attention_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VoicemailRow(Base):
    """One ingested voicemail, unique by the relay's ``voicemail_id``."""

    __tablename__ = "voicemails"

    voicemail_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    caller_phone: Mapped[str] = mapped_column(String(64), nullable=False)
    caller_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    transcript: Mapped[str] = mapped_column(Text, nullable=False, default="")
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    needs_callback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    callback_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="share-sheet")

    __table_args__ = (Index("ix_voicemails_thread_received", "thread_id", "received_at"),)
