"""Bills & financial-responsibilities plane (workstream G, spec §7).

Durable Bill / BillCandidate records + deterministic extraction (evidence +
per-field confidence) + upcoming / monthly-calendar queries + duplicate
detection + escalation ladder + the Today ``coming_up`` provider.

Hard rules (spec §7): NerveCenter never assumes a bill was paid without
confirmation, never transfers money or purchases, and preserves both
evidence AND uncertainties for every extracted field.

Public surface
--------------
- :class:`BillService` — orchestration (list / upcoming / calendar /
  confirm / accept / reject / dispute / stale sweep / attention refresh).
- :func:`bill_escalation_level` — the deterministic 0–3 escalation ladder
  consumed by the Attention v2 workstream (K).
- :func:`register_bills_today_provider` — registers the Today provider
  (the integrator calls it at startup; this package never edits
  ``backend/api/endpoints/today.py``).
- :mod:`backend.bills.extractor` — the pure, deterministic extraction
  (``extract_bill_candidate`` + the PDF text-layer helper).

Import note: the extraction reuses the house date parser from
``backend.communications.detection.dates`` via a lazy import (at first use),
because that package re-enters ``backend.attention`` / ``backend.api.app`` on
first import — keeping the bills package importable in any order.
"""

from .service import (
    BillService,
    BillsTodayProvider,
    bill_escalation_level,
    register_bills_today_provider,
)

__all__ = [
    "BillService",
    "BillsTodayProvider",
    "bill_escalation_level",
    "register_bills_today_provider",
]
