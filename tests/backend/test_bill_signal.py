"""The bill-signal gate: real bills qualify, marketing and receipts do not."""
from __future__ import annotations

import pytest

from backend.bills.signal import bill_signal

BILLS = [
    ("Your City Power bill is ready", "Amount due: $84.20. Due November 20.", 8420),
    ("Statement available", "Your statement balance of $1,204.10 is due on 12/01.", 120410),
    ("Invoice #4471", "Invoice total $42.00, payment due within 30 days.", 4200),
    ("Payment reminder", "Your minimum payment due is $35.00 by Friday.", 3500),
    ("Your bill", "Your bill is $61.99 and will be charged on Nov 18.", 6199),
    ("Autopay scheduled", "A scheduled payment of $120.00 will be withdrawn on 11/20.", 12000),
    ("Past due notice", "Past due amount $210.55. Please pay to avoid late fees.", 21055),
    ("Water service", "Account balance: $ 58.40 due 11/30", 5840),
    ("AWS Invoice Available", "Invoice amount USD 12.34 for account 1234", 1234),
    ("Invoice", "Invoice total 1,005.00 USD due on receipt", 100500),
    ("Tuition",
     "Amount due | Term | Fall | Status | Unpaid | " + "x " * 30 + "| $2,500.00", 250000),
]

NOT_BILLS = [
    ("48 hours only: 40% off everything", "Save up to $50. Sale ends Sunday. Shop now!"),
    ("Your rewards expire soon", "Use code AUTUMN to save $20 on orders over $100 by Friday."),
    ("Your order has shipped", "Order total $59.99. Arriving Friday."),
    ("Receipt for your purchase", "You paid $4.99 with Apple Pay."),
    ("Payment received", "Thank you for your payment of $84.20. Your balance is $0.00."),
    ("Weekly digest", "Top stories this week. Subscriptions start at $5/month."),
    ("Sign-in alert", "A new device signed in to your account."),
    ("Hotel deal", "Rooms from $129 a night, limited-time offer."),
    ("", ""),
]


@pytest.mark.parametrize(("subject", "body", "cents"), BILLS)
def test_real_bills_qualify_with_the_anchored_amount(subject, body, cents) -> None:
    sig = bill_signal(subject, body)
    assert sig is not None, f"{subject!r} should read as a bill"
    assert sig.amount_cents == cents


@pytest.mark.parametrize(("subject", "body"), NOT_BILLS)
def test_marketing_receipts_and_noise_do_not_qualify(subject, body) -> None:
    assert bill_signal(subject, body) is None


def test_the_anchored_amount_wins_over_an_earlier_price() -> None:
    body = "Upgrade to Pro for $9.99/month. Your current balance: $73.12 due Nov 30."
    assert bill_signal("Your statement", body).amount_cents == 7312


def test_a_bill_phrase_without_an_amount_does_not_qualify() -> None:
    assert bill_signal("Your bill is ready", "Sign in to see the amount due.") is None


def test_html_inside_the_text_part_is_stripped_before_matching() -> None:
    body = ('<table><tr><td style="color:#336699;font-weight:bold">Amount due</td>'
            '<td class="cell" style="padding:4px 8px; border:1px solid #ccc">&nbsp;</td>'
            '<td style="text-align:right">&#36;1,250.00</td></tr></table>'
            '<style>.x{color:red}</style>')
    assert bill_signal("Account statement", body).amount_cents == 125000


@pytest.mark.parametrize("hostile", [
    "Amount due " + "<" * 50_000 + " $10.00",
    "Amount due " + "<a " * 20_000 + "$10.00",
    "<style>" + "x" * 100_000,
])
def test_tag_stripping_is_bounded_on_hostile_input(hostile) -> None:
    import time

    start = time.perf_counter()
    bill_signal("bill", hostile)  # the amount is far outside the window: None is fine
    assert time.perf_counter() - start < 1.0, "the gate must stay linear on hostile markup"
