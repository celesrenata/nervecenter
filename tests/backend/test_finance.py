"""Financial helper (NS-ADR-102): planner fixtures, imports, reminders, read-only."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.endpoints import finance as ep
from backend.finance import parsers, planner, reminders
from backend.persistence.connection import make_session_factory
from backend.persistence.models.finance import DebtAccountRow, LedgerEntryRow, StatementImportRow

ROOT = Path(__file__).resolve().parents[2]
D = planner.Debt


def _fixture_debts() -> list[planner.Debt]:
    # A: $100.00 at 24% APR, min $20. B: $50.00 at 12% APR, min $10. Extra $10.
    return [D("A", "A", 10000, Decimal("24"), 2000), D("B", "B", 5000, Decimal("12"), 1000)]


def _total(res: dict, key: str = "payments") -> int:
    return sum(sum(m[key].values()) for m in res["schedule"])


def test_avalanche_snowball_match_hand_computed_fixture() -> None:
    res = planner.plan(_fixture_debts(), extra_cents=1000, start=date(2026, 12, 15))
    av, sb, mins = res["avalanche"], res["snowball"], res["minimums"]
    # Hand computed (half-up cents per debt per month).
    assert (av["months"], av["total_interest_cents"]) == (4, 603)
    assert (sb["months"], sb["total_interest_cents"]) == (4, 655)
    assert (mins["months"], mins["total_interest_cents"]) == (6, 802)
    assert av["interest_saved_cents"] == 199 and sb["interest_saved_cents"] == 147
    assert av["payoff_date"] == "2027-03-01" and av["schedule"][0]["date"] == "2026-12-01"
    first = av["schedule"][0]
    assert first["payments"] == {"A": 3000, "B": 1000} and first["balance_cents"] == 7200 + 4050
    assert sb["schedule"][0]["payments"] == {"A": 2000, "B": 2000}
    assert _total(av) - 0 == 10000 + 5000 + av["total_interest_cents"]


def test_zero_apr_and_never_reached_are_honest() -> None:
    zero = planner.plan([D("Z", "Z", 120000, Decimal("0"), 10000)], start=date(2026, 1, 1))
    assert zero["avalanche"]["months"] == 12 and zero["avalanche"]["total_interest_cents"] == 0
    stuck = planner.plan([D("S", "S", 1000000, Decimal("24"), 10000)], start=date(2026, 1, 1))
    for key in ("minimums", "avalanche", "snowball"):
        assert stuck[key]["never_reached"] is True
        assert stuck[key]["payoff_date"] is None and stuck[key]["total_interest_cents"] is None
    assert stuck["avalanche"]["interest_saved_cents"] is None
    rescued = planner.plan([D("S", "S", 1000000, Decimal("24"), 10000)], extra_cents=200000,
                           start=date(2026, 1, 1))
    assert rescued["avalanche"]["never_reached"] is False
    assert rescued["avalanche"]["interest_saved_cents"] is None  # baseline never finishes


APPLE = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchaser\n"
    "11/03/2026,11/04/2026,COFFEE SHOP 123,Coffee Shop,Restaurants,Purchase,4.50,Me\n"
    "11/05/2026,11/06/2026,ACH DEPOSIT INTERNET TRANSFER,Apple Card,Payment,Payment,-100.00,Me\n"
    "11/07/2026,11/08/2026,COFFEE SHOP 123,Coffee Shop,Restaurants,Purchase,4.50,Me\n"
    "11/07/2026,11/08/2026,COFFEE SHOP 123,Coffee Shop,Restaurants,Purchase,4.50,Me\n"
)
OFX = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261102120000<TRNAMT>-23.45<FITID>F1<NAME>GROCER<MEMO>FOOD
</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261110<TRNAMT>200.00<FITID>F2<NAME>PAYMENT THANK YOU
</STMTTRN>
</BANKTRANLIST></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>
"""


def test_parse_apple_card_csv() -> None:
    rows, flavor = parsers.parse_csv(APPLE.encode())
    assert flavor == "apple_card" and len(rows) == 4
    assert rows[0] == parsers.Row(date(2026, 11, 3), -450, "Coffee Shop")  # purchase = money out
    assert rows[1].amount_cents == 10000  # payment = money in


def test_parse_ofx_sgml() -> None:
    rows = parsers.parse_ofx(OFX)
    assert [(r.entry_date, r.amount_cents, r.fitid) for r in rows] == [
        (date(2026, 11, 2), -2345, "F1"), (date(2026, 11, 10), 20000, "F2")]
    assert rows[0].description == "GROCER FOOD"


def test_parse_rejects_garbage_without_leaking_content() -> None:
    for blob in (b"", b"not,a,statement\n1,2,3\n", b"<OFX></OFX>"):
        with pytest.raises(parsers.StatementError):
            parsers.parse_statement(blob)


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        for model in (DebtAccountRow, StatementImportRow, LedgerEntryRow):
            await conn.run_sync(model.__table__.create)
    yield eng
    await eng.dispose()


@pytest.fixture
def client(engine):
    app = FastAPI()
    app.include_router(ep.router)
    app.state.db_engine = engine
    return TestClient(app)


async def _count(engine, model) -> int:
    async with make_session_factory(engine)() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_reimport_adds_zero_rows_and_overlap_dedupes(client, engine) -> None:
    first = client.post("/api/finance/imports?institution=Apple%20Card", content=APPLE.encode())
    assert first.status_code == 200 and first.json()["added"] == 4
    again = client.post("/api/finance/imports?institution=Apple%20Card", content=APPLE.encode())
    assert again.json() == {**again.json(), "duplicate": True, "added": 0}
    assert await _count(engine, LedgerEntryRow) == 4
    assert await _count(engine, StatementImportRow) == 1
    # A different file that overlaps (same first rows plus one new) only adds the new row.
    more = APPLE + "11/09/2026,11/10/2026,BOOKS,Books,Shopping,Purchase,12.00,Me\n"
    third = client.post("/api/finance/imports?institution=Apple%20Card", content=more.encode())
    assert third.json()["added"] == 1 and await _count(engine, LedgerEntryRow) == 5


async def test_ofx_import_and_ledger_listing(client, engine) -> None:
    r = client.post("/api/finance/imports?institution=Chase", content=OFX)
    assert r.json()["format"] == "ofx" and r.json()["added"] == 2
    led = client.get("/api/finance/ledger").json()
    assert led["total"] == 2 and led["entries"][0]["date"] == "2026-11-10"
    assert client.post("/api/finance/imports?institution=Chase", content=OFX).json()["added"] == 0


async def test_import_limits_and_errors(client) -> None:
    big = b"a" * (parsers.MAX_BYTES + 1)
    assert client.post("/api/finance/imports?institution=Chase", content=big).status_code == 413
    bad = client.post("/api/finance/imports?institution=Chase", content=b"nope")
    assert bad.status_code == 422 and "nope" not in bad.text
    assert client.post("/api/finance/imports?institution=Nowhere", content=OFX).status_code == 422
    assert client.post("/api/finance/imports?institution=Chase&account_id=zzz",
                       content=OFX).status_code == 422


def test_account_crud_and_validation(client) -> None:
    body = {"institution": "Chase", "kind": "credit_card", "name": "Sapphire",
            "balance_cents": 250000, "apr_pct": "22.99", "min_payment_cents": 7500, "due_day": 15}
    made = client.post("/api/finance/accounts", json=body)
    assert made.status_code == 201
    acc = made.json()
    assert client.get("/api/finance/accounts").json()["accounts"][0]["apr_pct"] == "22.99"
    up = client.patch(f"/api/finance/accounts/{acc['id']}", json={"balance_cents": 240000})
    assert up.json()["balance_cents"] == 240000
    assert client.post("/api/finance/accounts", json={**body, "apr_pct": "abc"}).status_code == 422
    assert client.post("/api/finance/accounts", json={**body, "apr_pct": "150"}).status_code == 422
    for bad in ({"institution": "X"}, {"balance_cents": -1}):
        assert client.post("/api/finance/accounts", json={**body, **bad}).status_code == 422
    plan = client.get("/api/finance/plan?extra_cents=5000").json()
    assert plan["avalanche"]["months"] and plan["total_balance_cents"] == 240000
    assert client.delete(f"/api/finance/accounts/{acc['id']}").status_code == 204
    assert client.delete(f"/api/finance/accounts/{acc['id']}").status_code == 404


def test_card_reminders_levels_and_month_end() -> None:
    accs = [{"id": "c1", "name": "Card", "institution": "Chase", "due_day": 31,
             "balance_cents": 100, "min_payment_cents": 25},
            {"id": "c2", "name": "Paid", "institution": "BECU", "due_day": 5, "balance_cents": 0}]
    out = reminders.card_reminders(accs, date(2026, 2, 27))
    assert len(out) == 1 and out[0]["due_date"] == "2026-02-28" and out[0]["level"] == 2
    far = reminders.card_reminders(accs, date(2026, 3, 1))[0]
    assert far["due_date"] == "2026-03-31" and far["level"] == 0
    assert reminders.next_due(5, date(2026, 12, 20)) == date(2027, 1, 5)


async def test_card_attention_created_once() -> None:
    class Attn:
        def __init__(self) -> None:
            self.items: list = []

        async def list_attention_items(self, state=None):
            return self.items

        async def create_attention_item(self, **kw):
            self.items.append(type("I", (), {"source_refs": kw["source_refs"]})())

    class Reg:
        attention = Attn()

    rem = [{"kind": "card_due", "id": "c1", "title": "Card", "due_date": "2026-11-20",
            "days_until": 1, "level": 2}]
    assert await reminders.mirror_card_attention(Reg, rem) == 1
    assert await reminders.mirror_card_attention(Reg, rem) == 0


def test_no_route_can_initiate_a_payment() -> None:
    routes = [(r.path, sorted(r.methods or [])) for r in ep.router.routes]
    forbidden = {"pay", "payment", "payments", "transfer", "transfers", "send", "charge",
                 "initiate"}
    for path, methods in routes:
        assert not forbidden & set(path.lower().split("/")), path
        assert set(methods) <= {"GET", "POST", "PATCH", "DELETE"}
    # The only POSTs create an account or upload a file.
    assert sorted(p for p, m in routes if "POST" in m) == [
        "/api/finance/accounts", "/api/finance/imports"]
    for rel in ("backend/finance", "backend/api/endpoints/finance.py"):
        base = ROOT / rel
        for f in ([base] if base.is_file() else base.glob("*.py")):
            text = f.read_text()
            net = r"^\s*(import|from)\s+(httpx|requests|aiohttp|plaid)"
            assert not re.search(net, text, re.M), f
