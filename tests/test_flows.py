"""End-to-end voice conversations against the PayPal simulator.

Every request the backbone sends is checked against PayPal's official OpenAPI contract by the
simulator; a malformed body comes back as INVALID_REQUEST and fails these tests.
"""
import asyncio
import time
from decimal import Decimal

import httpx
import pytest

from paypal_voice.app_factory import build_engine
from paypal_voice.engine import Audit
from paypal_voice.paypal_client import PayPalClient, PayPalError, Token


def make(**cfg):
    engine, mock = build_engine("mock", audit_path="")
    for k, v in cfg.items():
        setattr(engine.config, k, v)
    return engine, mock


def signed_in(engine, sid="s-test-0001"):
    engine.session(sid).user_token = Token("A21AAmock-user", time.time() + 3600)
    return sid


async def say(engine, sid, *lines):
    out = []
    for line in lines:
        out.append(await engine.handle_turn(sid, line))
    return out


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ T0 reads

def test_balance_and_activity():
    engine, mock = make()
    r1, r2, r3 = run(say(engine, "s-owner-0001", "what's my balance", "what came in this week", "how much did i spend last week"))
    assert r1.speech == "You have $1,250.18 available, plus $92 on hold."
    assert "You received $160 this week" in r2.speech
    assert r3.speech.startswith("You spent $")
    tx = [q for q in mock.requests if q["path"] == "/v1/reporting/transactions"]
    assert tx and all(q["params"]["start_date"].endswith("Z") for q in tx)


def test_long_range_is_split_into_31_day_calls():
    engine, mock = make()
    run(say(engine, "s-owner-0002", "show my transactions for the last 90 days"))
    calls = [q for q in mock.requests if q["path"] == "/v1/reporting/transactions"]
    assert len(calls) == 3, "90 days needs three Transaction Search calls (31-day maximum)"


def test_more_reads_the_rest_of_a_list():
    engine, mock = make()
    sid = "s-owner-0003"
    first, = run(say(engine, sid, "transactions in the last 31 days"))
    assert "Say more for the rest." in first.speech
    nxt, = run(say(engine, sid, "more"))
    assert nxt.speech and nxt.speech != first.speech


# ------------------------------------------------------------------ T2 safety

def test_t2_needs_confirmation_in_a_later_turn_and_never_on_first_utterance():
    engine, mock = make()
    sid = signed_in(engine)
    r, = run(say(engine, sid, "yes send 20 dollars to sam"))
    assert r.state == "confirming"
    assert not any(q["path"] == "/v2/personal-payments/tokens" for q in mock.requests)


def test_send_money_paypal_ui_round_trip():
    engine, mock = make()
    sid = signed_in(engine)
    ask, done = run(say(engine, sid, "send twenty dollars to sam for coffee", "yes"))
    assert ask.expecting == "confirm" and "$20 to Sam Rivera" in ask.speech and "coffee" in ask.speech
    assert done.actions[0]["type"] == "open_url"
    url = done.actions[0]["url"]
    assert "/myaccount/transfer/send/external/pisp?token_id=PP-" in url and url.endswith(f"/paypal/return?sid={sid}")
    body = next(q for q in mock.requests if q["path"] == "/v2/personal-payments/tokens")["body"]
    assert body == {"receiver": {"id": "sam@example.com", "type": "EMAIL"}, "amount": {"currency_code": "USD", "value": "20.00"},
                    "payment_type": "PERSONAL", "intent": "CAPTURE", "external_reference_id": body["external_reference_id"],
                    "note_to_receiver": "coffee"}
    token_id = url.split("token_id=")[1].split("&")[0]
    mock.approve_token(token_id)
    status, = run(say(engine, sid, "did my payment go through"))
    assert status.speech == "Your $20 to Sam Rivera went through."
    back = run(engine.paypal_return(sid, {"status": "success", "transactionId": "9XY", "returnCode": "SUCCESS"}))
    assert back.speech.startswith("Done. $20 is on its way to Sam Rivera")


def test_goods_and_services_sets_purchase():
    engine, mock = make()
    sid = signed_in(engine)
    run(say(engine, sid, "pay jordan 60 dollars for the print as goods and services", "yes"))
    body = next(q for q in mock.requests if q["path"] == "/v2/personal-payments/tokens")["body"]
    assert body["payment_type"] == "PURCHASE"


def test_no_cancels_and_nothing_is_sent():
    engine, mock = make()
    sid = signed_in(engine)
    _, r = run(say(engine, sid, "send 20 dollars to sam", "no"))
    assert "Nothing was sent" in r.speech
    assert not any(q["path"] == "/v2/personal-payments/tokens" for q in mock.requests)


def test_change_amount_during_confirmation():
    engine, mock = make()
    sid = signed_in(engine)
    _, changed, _ = run(say(engine, sid, "send 20 dollars to sam", "actually make it 25", "yes"))
    assert "$25" in changed.speech and changed.expecting == "confirm"
    body = next(q for q in mock.requests if q["path"] == "/v2/personal-payments/tokens")["body"]
    assert body["amount"]["value"] == "25.00"


def test_confirmation_expires(monkeypatch):
    engine, mock = make()
    sid = signed_in(engine)
    run(say(engine, sid, "send 20 dollars to sam"))
    engine.session(sid).pending.asked_at -= 600
    r, = run(say(engine, sid, "yes"))
    assert "expired" in r.speech
    assert not any(q["path"] == "/v2/personal-payments/tokens" for q in mock.requests)


def test_second_factor_required_when_enabled():
    engine, mock = make(second_factor=True)
    sid = "s-owner-0004"
    _, tap = run(say(engine, sid, "refund sam", "yes"))
    assert tap.actions[0]["type"] == "second_factor"
    assert not any("/refund" in q["path"] for q in mock.requests)
    wrong = run(engine.second_factor(sid, "not-the-token"))
    assert wrong.state == "idle" and not any("/refund" in q["path"] for q in mock.requests)
    ok = run(engine.second_factor(sid, tap.actions[0]["token"]))
    assert ok.speech == "Refunded $40 to Sam Rivera."


def test_spending_limits():
    engine, mock = make(max_per_action=Decimal("100"), daily_limit=Decimal("150"))
    sid = signed_in(engine)
    over, = run(say(engine, sid, "send 200 dollars to sam"))
    assert "over your $100 limit" in over.speech
    run(say(engine, sid, "send 90 dollars to sam", "yes"))
    daily, = run(say(engine, sid, "send 80 dollars to maya"))
    assert "daily voice limit" in daily.speech


def test_unknown_person_is_asked_for_and_remembered():
    engine, mock = make()
    sid = signed_in(engine)
    a, b, c, d = run(say(engine, sid, "send money to alex", "alex@example.com", "twenty five", "yes"))
    assert a.speech == "What's Alex's email or phone number?"
    assert b.expecting == "slot:amount"
    assert "$25 to Alex at alex@example.com" in c.speech
    assert engine.contacts.find("alex")["id"] == "alex@example.com"


def test_cancel_mid_flow():
    engine, mock = make()
    sid = signed_in(engine)
    _, r = run(say(engine, sid, "send money to alex", "never mind"))
    assert r.speech == "Cancelled. Nothing was sent."
    assert engine.session(sid).pending is None


def test_idempotent_retry_creates_one_effect():
    engine, mock = make()
    sid = "s-owner-0005"
    run(say(engine, sid, "refund sam", "yes"))
    req = next(q for q in mock.requests if q["path"].endswith("/refund"))
    first = mock.handle(httpx.Request("POST", f"https://mock{req['path']}", json=req["body"] or {},
                                      headers={"Authorization": "Bearer x", "PayPal-Request-Id": req["request_id"]}))
    assert first.headers["paypal-debug-id"] == "mockdbg-replay"


# ------------------------------------------------------------------ business writes

def test_invoice_create_and_send():
    engine, mock = make()
    sid = "s-owner-0006"
    ask, done = run(say(engine, sid, "bill maya 45 dollars for a framed print", "yes"))
    assert ask.speech == "Send Maya Chen at maya@example.com an invoice for $45 for framed print. Send it?"
    assert done.speech == "Sent. Maya Chen has an invoice for $45 for framed print."
    created = [i for i in mock.invoices.values() if i["primary_recipients"][0]["billing_info"]["email_address"] == "maya@example.com"]
    assert created and created[0]["status"] == "SENT"


def test_remind_by_ordinal_after_listing():
    engine, mock = make()
    sid = "s-owner-0007"
    lst, ask, done = run(say(engine, sid, "who owes me money", "remind the second one", "yes"))
    assert "Priya Nair" in ask.speech
    assert any(q["path"].endswith("/remind") for q in mock.requests)


def test_ambiguous_reference_asks_which_one():
    engine, mock = make()
    sid = "s-owner-0008"
    ask, pick, done = run(say(engine, sid, "remind them about the invoice", "priya", "yes"))
    assert ask.speech.startswith("Which one:")
    assert "Priya Nair" in pick.speech and done.state == "done"


def test_refund_over_original_amount_is_spoken_plainly():
    engine, mock = make()
    sid = "s-owner-0009"
    _, r = run(say(engine, sid, "refund 50 dollars to sam", "yes"))
    assert r.speech == "That's more than the original payment."
    assert r.state == "error"


def test_payouts_gated_until_enabled():
    engine, mock = make(payouts_enabled=False)
    r, = run(say(engine, "s-owner-0010", "pay out 30 dollars to jordan@example.com"))
    assert r.state == "handoff" and "Payouts" in r.speech


def test_dispute_accept_requires_yes():
    engine, mock = make()
    sid = "s-owner-0011"
    _, ask, done = run(say(engine, sid, "any disputes", "accept the claim", "yes"))
    assert "can't be undone" in ask.speech
    assert done.speech == "Claim accepted. The buyer will get $65 back."


# ------------------------------------------------------------------ modes, availability, honesty

def test_personal_needs_sign_in():
    engine, mock = make()
    r, = run(say(engine, "s-anon-0001", "send 20 dollars to sam"))
    assert r.speech.startswith("First, sign in with PayPal.")
    assert r.actions[0]["purpose"] == "sign_in"


def test_personal_payments_gated_without_approval():
    engine, mock = make(personal_payments_enabled=False)
    sid = signed_in(engine)
    r, = run(say(engine, sid, "send 20 dollars to sam"))
    assert "Personal Payments" in r.speech and r.state == "handoff"


def test_personal_mode_explains_partner_only_activity():
    engine, mock = make(account_mode="PERSONAL")
    r, = run(say(engine, "s-pers-0001", "what's my balance"))
    assert "partner program" in r.speech


def test_handoff_for_no_api():
    engine, mock = make()
    r, = run(say(engine, "s-any-0001", "how much cashback do i have"))
    assert r.state == "handoff" and r.actions[0]["url"] == "https://www.paypal.com/rewards"
    assert not mock.requests


def test_sign_in_flow_checks_state():
    engine, mock = make()
    sid = "s-login-0001"
    r, = run(say(engine, sid, "sign in"))
    state = r.actions[0]["url"].split("state=")[1].split("&")[0]
    bad = run(engine.complete_sign_in(f"{sid}.wrong", "good-code"))
    assert bad.state == "error"
    ok = run(engine.complete_sign_in(state.replace("%2E", "."), "good-code"))
    assert ok.speech.startswith("You're signed in as Taylor Brooks")


def test_fee_check_and_unregistered_foreign_payee():
    engine, mock = make()
    sid = signed_in(engine)
    fee, = run(say(engine, sid, "what's the fee to send 50 dollars to sam"))
    assert fee.speech == "Sending $50 to Sam Rivera is free from your PayPal balance, or $1.75 by card ending 4417."
    need, = run(say(engine, sid, "how much does it cost to send 50 dollars to newfriend@example.com"))
    assert "full name and country" in need.speech


def test_audit_chain_records_writes_and_verifies():
    engine, mock = make()
    sid = signed_in(engine)
    run(say(engine, sid, "send 20 dollars to sam", "yes", "refund sam", "yes"))
    log = engine.audit.memory
    assert [e["capability"] for e in log] == ["send_money", "refund"]
    assert all(e["paypal"] for e in log) and Audit.verify(log)
    log[0]["slots"]["amount"] = "$2000"
    assert not Audit.verify(log)


def test_every_capability_runs_clean_against_contracts():
    """Drive each capability once; the simulator rejects any request that violates PayPal's schema."""
    engine, mock = make()
    sid = signed_in(engine)
    script = ["who am i signed in as", "send 20 dollars to sam", "yes", "did my payment go through",
              "what's the fee to send 50 dollars to sam", "can i send money to mexico", "what's my balance",
              "what came in this week", "did jordan pay me", "who owes me money", "bill maya 45 dollars for a print", "yes",
              "remind jordan about the invoice", "yes", "cancel priya's invoice", "yes", "make a payment link for 25 dollars for a print",
              "yes", "refund sam", "yes", "any disputes", "accept the claim", "yes", "show my subscriptions",
              "cancel maya's subscription", "yes", "pay out 30 dollars to jordan@example.com", "yes", "how much is 100 dollars in euros"]
    results = run(say(engine, sid, *script))
    errors = [(line, r.speech) for line, r in zip(script, results) if r.state == "error"]
    assert not errors, errors
    assert not [q for q in mock.requests if q.get("contract_problems")], "a request violated PayPal's OpenAPI contract"
    assert all("status" in q for q in mock.requests if q["path"] != "/v1/oauth2/token")
    used = {c["op"] for c in engine.client.calls}
    assert {"v2.tokens.create", "invoices.create", "invoices.send", "invoices.remind", "invoices.cancel", "payment-resources.post",
            "captures.refund", "disputes.accept-claim", "subscriptions.cancel", "payouts.post", "quote_exchange_rates"} <= used


# ------------------------------------------------------------------ client behaviour

def test_client_retries_503_then_succeeds():
    hits = {"n": 0}

    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE", "message": "try later"})
        return httpx.Response(200, json={"ok": True})

    c = PayPalClient("mock", "id", "secret", transport=httpx.MockTransport(handler))
    assert run(c.request("GET", "/v1/reporting/balances", op="balances.get")) == {"ok": True}
    assert hits["n"] == 2


def test_client_raises_typed_error_with_debug_id():
    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(403, json={"name": "NOT_AUTHORIZED", "message": "Authorization failed due to insufficient permissions.",
                                         "debug_id": "abc123"})

    c = PayPalClient("mock", "id", "secret", transport=httpx.MockTransport(handler))
    with pytest.raises(PayPalError) as e:
        run(c.request("GET", "/v1/reporting/balances", op="balances.get"))
    assert e.value.name == "NOT_AUTHORIZED" and e.value.debug_id == "abc123"


def test_permission_error_gives_actionable_hint():
    engine, mock = make()

    def deny(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(403, json={"name": "NOT_AUTHORIZED", "message": "Authorization failed due to insufficient permissions.", "debug_id": "d1"})

    engine.client = PayPalClient("mock", "id", "secret", transport=httpx.MockTransport(deny))
    r, = run(say(engine, "s-owner-0012", "what's my balance"))
    assert r.speech == ("This app isn't allowed to do that yet. Turn on Transaction Search for this app in the PayPal "
                        "developer dashboard; it can take up to 9 hours.")
    assert "PayPal debug id: d1" in r.display["lines"]
