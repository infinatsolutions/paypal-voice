"""Golden routing set: what people actually say (recognizer-style text) -> capability.

Every registry example is included automatically, so a new capability can't ship without
its phrasing being routed correctly.
"""
from datetime import datetime, timezone

import pytest

from paypal_voice.nlu import classify
from paypal_voice.normalize import normalize
from paypal_voice.registry import CAPABILITIES

NOW = datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)

GOLDEN = [
    # balance
    ("what's my balance", "balance"), ("whats my paypal balance", "balance"), ("how much money do i have", "balance"),
    ("um how much is in my account", "balance"), ("pay pal balance please", "balance"), ("what's in my paypal", "balance"),
    # activity
    ("what came in this week", "activity"), ("how much did i make last month", "activity"), ("recent activity", "activity"),
    ("what did i spend yesterday", "activity"), ("show my transactions", "activity"), ("who paid me today", "activity"),
    ("how much have i earned in october", "activity"), ("latest payments", "activity"),
    # find payment
    ("did jordan pay me", "find_payment"), ("has priya paid", "find_payment"), ("when did sam pay me", "find_payment"),
    ("did maya send me the money", "find_payment"),
    # invoices
    ("who owes me money", "invoices"), ("any unpaid invoices", "invoices"), ("show my invoices", "invoices"),
    ("outstanding invoices", "invoices"), ("overdue bills", "invoices"),
    ("bill sam forty dollars for framing", "invoice_create"), ("send an invoice to jordan@example.com for 85 dollars", "invoice_create"),
    ("invoice maya 45 for a print", "invoice_create"), ("request 25 dollars from maya", "invoice_create"),
    ("charge priya 55 for the small print set", "invoice_create"), ("create a new invoice for sam", "invoice_create"),
    ("remind jordan about the invoice", "invoice_remind"), ("send a reminder", "invoice_remind"), ("remind the first one", "invoice_remind"),
    ("cancel priya's invoice", "invoice_cancel"), ("cancel the invoice to jordan", "invoice_cancel"),
    # send money (personal)
    ("send twenty dollars to sam", "send_money"), ("pay maya twelve fifty for dinner", "send_money"),
    ("send forty bucks to sam at example dot com", "send_money"), ("give my mom fifty dollars", "send_money"),
    ("i want to send money to alex", "send_money"), ("can you pay sam 20", "send_money"), ("transfer 100 dollars to maya", "send_money"),
    ("tip jordan 5 dollars", "send_money"), ("pay jordan 60 for the print as goods and services", "send_money"),
    # personal checks
    ("how much does it cost to send 50 dollars to sam", "fee_check"), ("what's the fee to send 100 pounds to maya", "fee_check"),
    ("are there fees to send money to sam", "fee_check"),
    ("did my payment go through", "payment_status"), ("did sam get the money", "payment_status"),
    ("can i send money to mexico", "send_capabilities"), ("can i pay someone in the uk", "send_capabilities"),
    ("who am i signed in as", "whoami"), ("which paypal account is this", "whoami"), ("am i logged in", "whoami"),
    # business actions
    ("make a payment link for 25 dollars for a small print", "payment_link"), ("create a buy now link for 120", "payment_link"),
    ("refund sam", "refund"), ("refund the last payment", "refund"), ("refund ten dollars to jordan", "refund"),
    ("give sam his money back", "refund"),
    ("any disputes", "disputes"), ("do i have any claims", "disputes"), ("chargebacks", "disputes"),
    ("accept the claim", "dispute_accept"), ("accept the first dispute", "dispute_accept"),
    ("show my subscriptions", "subscriptions"), ("who's subscribed", "subscriptions"),
    ("cancel maya's subscription", "subscription_cancel"),
    ("pay out 50 dollars to sam@example.com", "payout"), ("send a payout to jordan for 30", "payout"), ("pay my contractor 200", "payout"),
    ("how much is 100 dollars in euros", "exchange_rate"), ("exchange rate to pounds", "exchange_rate"), ("convert 50 dollars to pesos", "exchange_rate"),
    # honest handoffs
    ("how much cashback do i have", "rewards"), ("check my rewards", "rewards"), ("any offers for me", "rewards"),
    ("what's in my savings", "savings"), ("transfer 100 to my bank", "bank_transfer"), ("cash out", "bank_transfer"),
    ("withdraw two hundred dollars", "bank_transfer"), ("lock my debit card", "card_controls"), ("what's my bitcoin worth", "crypto"),
]

GOLDEN += [(ex, c.name) for c in CAPABILITIES for ex in c.examples]


@pytest.mark.parametrize("said,expected", GOLDEN)
def test_routes(said, expected):
    u = classify(normalize(said, NOW))
    assert u.capability is not None, f"no route for {said!r}"
    assert u.capability.name == expected, f"{said!r} -> {u.capability.name} (candidates {u.candidates})"


NOT_MONEY = ["what's my balance", "who owes me money", "show my transactions", "any disputes", "help", "hello there",
             "did jordan pay me", "who paid me today", "what did i spend yesterday", "how much did i make", "send a reminder",
             "pay attention", "can you tell me about fees", "what's the weather"]


@pytest.mark.parametrize("said", NOT_MONEY)
def test_no_accidental_money_intent(said):
    u = classify(normalize(said, NOW))
    assert u.capability is None or u.capability.tier != "T2" or u.capability.name not in ("send_money", "payout", "refund"), said


def test_golden_set_is_large():
    assert len(GOLDEN) >= 120
