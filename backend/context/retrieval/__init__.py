"""Retrieval for Ask: real records, real ids, nothing invented.

``retrieve`` searches the personal-data services the screens already use and
returns :class:`Record` objects whose ids Ask can cite. See
:mod:`backend.context.retrieval.policy` for what Ask may and may not read.
"""
from backend.context.retrieval.service import RetrievalResult, retrieve
from backend.context.retrieval.types import Record

__all__ = ["Record", "RetrievalResult", "retrieve"]
