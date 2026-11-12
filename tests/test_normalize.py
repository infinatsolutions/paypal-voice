from datetime import datetime, timezone
from decimal import Decimal

import pytest

from paypal_voice.normalize import normalize

NOW = datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("said,amount", [
    ("send forty dollars to sam", "$40"),
    ("send twelve fifty to sam", "$12.50"),
    ("send two twenty five to amy", "$2.25"),
    ("send a hundred dollars to mike", "$100"),
    ("send one thousand two hundred dollars", "$1,200"),
    ("send $1,200.50 to bob@x.com", "$1,200.50"),
    ("send 5 dollars and 20 cents to my mom", "$5.20"),
    ("refund twenty five euros", "€25"),
    ("forty bucks to sam", "$40"),
    ("send 40 to sam", "$40"),
    ("send fifteen pounds", "£15"),
    ("send 7 point 5 dollars", "$7.50"),
])
def test_amounts(said, amount):
    assert str(normalize(said, NOW).amount_or_bare()) == amount


def test_spoken_email_and_brand():
    p = normalize("Send forty dollars to Sam dot Rivera at gmail dot com on Pay Pal", NOW)
    assert p.emails == ["sam.rivera@gmail.com"]
    assert "paypal" in p.text


def test_phone_is_not_confused_with_amount():
    p = normalize("text Sam at 408 836 3374 twenty bucks", NOW)
    assert p.phones == ["+14088363374"]
    assert str(p.amounts[0]) == "$20"


@pytest.mark.parametrize("said,label,days", [
    ("what did i spend today", "today", 0),
    ("what came in yesterday", "yesterday", 1),
    ("last 10 days", "the last 10 days", 10),
    ("activity this week", "this week", 6),
    ("how much did i make last month", "last month", 30),
    ("what did i get in october", "October", 30),
])
def test_date_ranges(said, label, days):
    r = normalize(said, NOW).date_range
    assert r.label == label
    assert (r.end - r.start).days <= days


def test_month_in_future_means_last_year():
    r = normalize("payments in december", NOW).date_range
    assert r.start.year == 2025


def test_fillers_and_punctuation_removed():
    p = normalize("Um, what's my, uh, balance?", NOW)
    assert p.text == "what's my balance"


def test_paypal_ids():
    p = normalize("status of invoice INV2-ABCD-EFGH-IJKL-MNOP", NOW)
    assert p.ids == ["INV2-ABCD-EFGH-IJKL-MNOP"]


def test_bare_number_only_when_no_currency():
    p = normalize("send 30 euros to maya in 2 days", NOW)
    assert p.amount_or_bare().currency == "EUR" and p.amount_or_bare().value == Decimal(30)
