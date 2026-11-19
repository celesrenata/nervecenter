"""New Ask adapters: citation-safe ids, scrubbed and clipped text, None when the service is absent,
no excluded store. Fakes only."""
from __future__ import annotations
import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import pytest
import backend.api  # noqa: F401  (import order: avoids a circular import at cold start)
from backend.context.retrieval import policy
from backend.context.retrieval.intent import parse_intent
from backend.context.retrieval.sources import ADAPTERS
from backend.context.retrieval.sources import briefing as briefing_mod
from backend.context.retrieval.sources import finance as finance_mod
from backend.context.retrieval.sources import health as health_mod
from backend.context.retrieval.sources import knowledge as knowledge_mod
from backend.context.retrieval.sources import obligations as obligations_mod
from backend.context.retrieval.sources import planning as planning_mod
from backend.context.retrieval.sources import routines as routines_mod
from backend.context.retrieval.sources import status as status_mod
from backend.context.retrieval.types import Record
NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
FAKE_TOKEN = "sk-" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
LONG = "x" * 2000
ID_RE = re.compile(r"^[a-z]+-[0-9a-f]{10}$")
NEW_DOMAINS = ("finance", "obligations", "knowledge", "health", "routines", "briefing", "status")
def intent(question: str = "show everything"):
    return parse_intent(question, NOW)
def check_records(records: list[Record], domain: str) -> None:
    assert records, "adapter returned nothing"
    for r in records:
        assert r.domain == domain
        assert ID_RE.match(r.id) and policy.citation_safe(r.id), r.id
        assert r.id.startswith(policy.prefix_for(domain) + "-")
        blob = f"{r.title} {r.snippet}"
        assert FAKE_TOKEN not in blob
        assert len(r.snippet) <= policy.SNIPPET_CHARS and len(r.title) <= policy.TITLE_CHARS
        assert "\n" not in r.snippet and "\n" not in r.title
# -- registry and exclusions ---------------------------------------------------------------------
def test_every_new_domain_is_registered_in_both_tables() -> None:
    for domain in NEW_DOMAINS:
        assert domain in ADAPTERS and domain in policy.DOMAINS
        assert policy.link_for(domain).startswith("/")
    assert set(ADAPTERS) == set(policy.DOMAINS)
def test_no_adapter_exists_for_any_excluded_store() -> None:
    assert not set(ADAPTERS) & set(policy.EXCLUDED_STORES)
    prefixes = {policy.prefix_for(d) for d in policy.DOMAINS}
    assert len(prefixes) == len(policy.DOMAINS)  # ids stay unambiguous
    for store in policy.EXCLUDED_STORES:
        assert store not in policy.DOMAINS
def test_new_adapter_sources_never_touch_excluded_stores() -> None:
    root = Path(planning_mod.__file__).parent
    forbidden = ("mh_forms", "mental_health", "MentalHealth", "personal_api_tokens", "auth_sessions",
                 "mail_evidence", "mail_intel", "mail_thread_ledger", "omniroute_keys", "secrets",
                 "raw_mime", "app_logs", "relay_seen_messages")
    for name in ("finance", "obligations", "knowledge", "health", "routines", "briefing", "status"):
        tree = ast.parse((root / f"{name}.py").read_text())
        for node in ast.walk(tree):  # docstrings may name excluded stores; code may not
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                        getattr(node.body[0], "value", None), ast.Constant):
                    node.body[0].value.value = ""  # type: ignore[attr-defined]
        code = ast.unparse(tree)
        for token in forbidden:
            assert token not in code, (name, token)
def test_finance_never_reads_ledger_lines() -> None:
    source = Path(finance_mod.__file__).read_text()
    assert "LedgerEntryRow.description" in source  # grouped payee label only
    assert "group_by" in source and "func.sum" in source
    assert "finance_ledger" in policy.EXCLUDED_STORES
# -- None when the service is missing --------------------------------------------------------------
@pytest.mark.parametrize("domain", NEW_DOMAINS)
async def test_adapter_returns_none_when_its_service_is_missing(domain: str) -> None:
    assert await ADAPTERS[domain](SimpleNamespace(), intent(), 10) is None
@pytest.mark.parametrize("domain", ["finance", "obligations", "health", "bills"])
async def test_structured_entry_returns_none_when_its_service_is_missing(domain: str) -> None:
    entry = ADAPTERS[domain].structured  # type: ignore[attr-defined]
    assert await entry(SimpleNamespace(), intent("what is my biggest bill?"), 100) is None
# -- finance -------------------------------------------------------------------------------------
def _finance_load(state: Any, cap: int):
    accounts = [{"id": "acct1", "name": f"Card {FAKE_TOKEN}", "institution": "Bank " + LONG[:50],
                 "balance_cents": 120050, "apr_pct": 19.9, "min_payment_cents": 3500, "due_day": 12,
                 "as_of": "2026-06-01"}]
    spend = [{"payee": "Acme Water\nCo " + LONG, "year": 2026, "month": 5, "spent_cents": 4550, "count": 3},
             {"payee": "Globex", "year": 2026, "month": 4, "spent_cents": 9000, "count": 1}]
    return accounts, spend
async def test_finance_records_and_structured_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load(state, cap):
        return _finance_load(state, cap)
    monkeypatch.setattr(finance_mod, "_load", load)
    state = SimpleNamespace()
    check_records(await finance_mod.finance(state, intent(), 10), "finance")
    rows = await finance_mod.finance_structured(state, intent(), 10)
    check_records([r.record for r in rows], "finance")
    spend_rows = [r for r in rows if r.fields["status"] == "spent"]
    assert [r.fields["amount_cents"] for r in spend_rows] == [4550, 9000]
    assert all(isinstance(r.fields["amount_cents"], int) for r in rows)
    assert len(await finance_mod.finance(state, intent(), 2)) == 2
# -- obligations ---------------------------------------------------------------------------------
def _obligation(i: int, otype: str = "owed_by_me", origin: str = "stated", due_days: int | None = 2):
    return SimpleNamespace(
        id=f"o{i}", title=f"Send tax form {FAKE_TOKEN}", obligation_type=otype, status="open",
        origin=origin, due_at=(NOW + timedelta(days=due_days)) if due_days is not None else None,
        created_at=NOW - timedelta(days=1), consequence=LONG, next_action="file it", priority="high")
async def test_obligations_records_and_structured_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_obligation(1), _obligation(2, "waiting_on", "inferred", None)]
    async def load(state, cap):
        return rows
    monkeypatch.setattr(obligations_mod, "_load", load)
    state = SimpleNamespace()
    records = await obligations_mod.obligations(state, intent(), 10)
    check_records(records, "obligations")
    structured = await obligations_mod.obligations_structured(state, intent(), 10)
    assert [r.fields["status"] for r in structured] == ["open", "unconfirmed"]
    assert structured[0].fields["direction"] == "you owe"
    assert structured[1].fields["direction"] == "owed to you"
    assert "unconfirmed" in structured[1].record.snippet and "no due date" in structured[1].record.snippet
# -- knowledge -----------------------------------------------------------------------------------
async def test_knowledge_records_match_terms_and_are_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    note = SimpleNamespace(id="n1", author="me", body=f"The lease renews in March {FAKE_TOKEN} {LONG}",
                           created_at=NOW - timedelta(days=3))
    other = SimpleNamespace(id="n2", author="me", body="Buy milk", created_at=NOW)
    async def fetch(factory):
        return [], [], [], [(note, "Home"), (other, "Errands")]
    monkeypatch.setattr(knowledge_mod, "_fetch", fetch)
    state = SimpleNamespace(repository_registry=SimpleNamespace(session_factory=lambda: None))
    records = await knowledge_mod.knowledge(state, intent("what do my notes say about the lease?"), 10)
    check_records(records, "knowledge")
    assert len(records) == 1 and "lease" in records[0].snippet
# -- health --------------------------------------------------------------------------------------
async def test_health_records_and_structured_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    samples = [{"kind": "sleep", "day": f"2026-06-0{d}", "value": 400.0 + d * 10, "unit": "min"}
               for d in (1, 2, 3)]
    async def load(state, i):
        return samples
    monkeypatch.setattr(health_mod, "_load", load)
    state = SimpleNamespace()
    check_records(await health_mod.health(state, intent("how did I sleep this week?"), 10), "health")
    rows = await health_mod.health_structured(state, intent("how did I sleep this week?"), 10)
    summary = [r for r in rows if r.fields["status"] == "summary"]
    assert len(summary) == 1 and summary[0].fields["count"] == 3
    assert summary[0].fields["average"] == pytest.approx(420.0)
    assert len([r for r in rows if r.fields["status"] == "sample"]) == 3
# -- routines ------------------------------------------------------------------------------------
async def test_routines_report_completion_for_the_day(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.persistence.repositories.routines_repo import RoutinesRepository
    from backend.routines.service import RoutinesService
    async def list_routines(self, *, enabled_only=False):
        return [{"id": "r1", "title": f"Morning stretch {FAKE_TOKEN}", "kind": "habit",
                 "schedule": {"times": ["07:00"]}},
                {"id": "r2", "title": "Evening walk", "kind": "habit", "schedule": {}}]
    async def list_check_ins(self, routine_id, *, since=None):
        if routine_id == "r1":
            return [SimpleNamespace(at=NOW.replace(hour=7), completed=True, note="private note")]
        return []
    monkeypatch.setattr(RoutinesService, "list_routines", list_routines)
    monkeypatch.setattr(RoutinesRepository, "list_check_ins", list_check_ins)
    state = SimpleNamespace(repository_registry=SimpleNamespace(session_factory=lambda: None))
    records = await routines_mod.routines(state, intent("did I do my morning routine"), 10)
    check_records(records, "routines")
    by_title = {r.title: r for r in records}
    assert "completed" in next(r for t, r in by_title.items() if "stretch" in t).snippet
    assert "not checked in" in by_title["Routine: Evening walk"].snippet
    assert all("private note" not in r.snippet for r in records)
# -- briefing ------------------------------------------------------------------------------------
async def test_briefing_records_from_the_today_composer() -> None:
    brief = SimpleNamespace(
        generated_at=NOW,
        actions=[SimpleNamespace(attention_id="a1", label=f"Reply to school {FAKE_TOKEN}", due=NOW)],
        waiting_on=[SimpleNamespace(request_id="w1", person="Sam", description=LONG, days_elapsed=4)],
        today=[SimpleNamespace(event_id="e1", title="Piano", start=NOW)],
        overnight=SimpleNamespace(emails=2, messages=1, voicemails=0),
    )
    class Svc:
        def compose(self, kind, now=None):
            return brief
    records = await briefing_mod.briefing(SimpleNamespace(briefing_service=Svc()), intent("show my briefing"), 10)
    check_records(records, "briefing")
    assert len(records) == 4 and any("Overnight" in r.title for r in records)
# -- status --------------------------------------------------------------------------------------
async def test_status_reports_counts_and_summaries_only() -> None:
    class Incidents:
        async def list_incidents(self):
            return [{"id": "i1", "title": f"Disk full {FAKE_TOKEN}", "severity": "high", "status": "open",
                     "description": "SECRET-BODY", "createdAt": NOW.isoformat()},
                    {"id": "i2", "title": "Old", "severity": "low", "status": "resolved"}]
    class Systems:
        async def list_systems(self):
            return [{"name": "gateway", "status": "ok", "message": "healthy", "checked_at": NOW.isoformat()}]
    state = SimpleNamespace(services={"incidents": Incidents(), "systems": Systems()})
    records = await status_mod.status(state, intent("is anything wrong right now?"), 20)
    check_records(records, "status")
    text = " ".join(f"{r.title} {r.snippet}" for r in records)
    assert "1 open (1 high)" in text and "SECRET-BODY" not in text and "Old" not in text
    assert any(r.title == "Systems health" for r in records)
async def test_status_skips_a_failing_source_and_keeps_the_rest() -> None:
    class Broken:
        async def list_incidents(self):
            raise RuntimeError("boom")
    class Systems:
        async def list_systems(self):
            return [{"name": "gateway", "status": "ok", "message": "", "checked_at": NOW.isoformat()}]
    state = SimpleNamespace(services={"incidents": Broken(), "systems": Systems()})
    records = await status_mod.status(state, intent("any systems down"), 20)
    assert records and all(r.id.startswith("stat-") for r in records)
# -- bills (widened) -----------------------------------------------------------------------------
async def test_bills_structured_includes_unconfirmed_candidates_and_all_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.bills.service import BillService
    bills = [{"id": f"b{i}", "payee": f"Payee{i}", "amount_cents": str(1000 + i), "currency": "USD",
              "status": "pending", "due_at": (NOW + timedelta(days=i)).isoformat()} for i in range(40)]
    async def list_bills(self, *, status=None):
        return bills
    async def list_candidates(self, *, status=None):
        return [{"id": "c1", "created_at": NOW.isoformat(), "duplicate_of": None,
                 "extracted_fields": {"payee": "Mystery", "amount_cents": 777}},
                {"id": "c2", "created_at": NOW.isoformat(), "duplicate_of": "b1",
                 "extracted_fields": {"payee": "Dup", "amount_cents": 1}}]
    monkeypatch.setattr(BillService, "list_bills", list_bills)
    monkeypatch.setattr(BillService, "list_candidates", list_candidates)
    state = SimpleNamespace(repository_registry=SimpleNamespace(session_factory=lambda: None))
    rows = await planning_mod.bills_structured(state, intent("what is my biggest bill?"), 5000)
    assert len(rows) == 41  # 40 bills + 1 non-duplicate candidate
    check_records([r.record for r in rows], "bills")
    candidate = rows[-1]
    assert candidate.fields["status"] == "unconfirmed" and "unconfirmed" in candidate.record.snippet
    assert all(isinstance(r.fields["amount_cents"], int) for r in rows)
    assert len(await planning_mod.bills_structured(state, intent(), 10)) == 10
