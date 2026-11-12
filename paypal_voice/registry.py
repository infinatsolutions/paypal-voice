"""Capability registry: the single table that maps spoken intents to PayPal operations.

Each Capability declares how it is recognised (patterns), what it needs (slots), where it is
allowed (account mode, availability), how dangerous it is (risk tier), which PayPal operations it
uses (for the coverage report), how to confirm it, and how to run it and speak the result.
Help text, the coverage table and the golden tests are all generated from this table.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Callable

from .normalize import DateRange, Money
from .paypal_client import PayPalError
from .speak import Reply, join_and, listing, money, plural, when

# availability
GA = "GA"                        # public REST API, acts on the API caller's own account
LIMITED = "LIMITED_RELEASE"      # PayPal must approve the app (Personal Payments, Log in with PayPal)
APPROVAL = "NEEDS_APPROVAL"      # GA API that PayPal enables per account (Payouts)
PARTNER = "PARTNER_ONLY"         # needs the PayPal partner network
NONE = "NOT_AVAILABLE"           # no API exists; hand off to PayPal

PERSONAL, OWNER, ANY = "PERSONAL", "OWNER", "ANY"


@dataclass
class Payee:
    id: str
    type: str                    # EMAIL | PHONE
    name: str = ""
    country_code: str = ""

    def spoken(self) -> str:
        return self.name or self.id


@dataclass
class Slot:
    name: str
    kind: str                    # money | payee | email | text | date_range | ref | country | currency | payment_type
    required: bool = True
    prompt: str = ""


@dataclass
class Capability:
    name: str
    summary: str
    examples: list[str]
    patterns: list[str]
    mode: str
    availability: str
    tier: str
    ops: list[str]
    run: Callable[["Ctx", dict], Awaitable[Reply]] | None = None
    slots: list[Slot] = field(default_factory=list)
    confirm: Callable[["Ctx", dict], str] | None = None
    exclude: list[str] = field(default_factory=list)
    priority: int = 0
    handoff: str = ""
    handoff_url: str = ""
    error_hint: str = ""
    money_slot: str = ""         # which slot carries the amount checked against spending limits
    ref_kind: str = ""           # what a "ref" slot points to (invoice, transaction, dispute, subscription)

    def matches(self, text: str) -> bool:
        if any(re.search(x, text) for x in self.exclude):
            return False
        return any(re.search(p, text) for p in self.patterns)


@dataclass
class Ctx:
    client: object               # PayPalClient
    session: object              # dialog.Session
    config: object               # engine.Config
    now: datetime


def _user(ctx: Ctx):
    return ctx.session.user_token


def _rfc3339(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =================================================================== PERSONAL (signed-in PayPal user)

async def run_whoami(ctx: Ctx, s: dict) -> Reply:
    u = await ctx.client.request("GET", "/v1/identity/openidconnect/userinfo", op="userinfo.get",
                                 params={"schema": "openid"}, user_token=_user(ctx))
    ctx.session.profile = u
    name = u.get("name") or " ".join(filter(None, [u.get("given_name"), u.get("family_name")]))
    kind = (u.get("account_type") or "").lower()
    speech = f"You're signed in as {name}" + (f", {u['email']}" if u.get("email") else "") + (f", a {kind} account." if kind else ".")
    return Reply(speech, {"title": "Signed in", "lines": [name, u.get("email", ""), u.get("account_type", "")]})


def confirm_send(ctx: Ctx, s: dict) -> str:
    p: Payee = s["payee"]
    kind = "for goods or services" if s.get("payment_type") == "PURCHASE" else "as a personal payment"
    note = f", note: {s['note']}" if s.get("note") else ""
    return f"Send {s['amount']} to {p.spoken()}{' at ' + p.id if p.name else ''} {kind}{note}. PayPal will ask you to approve it. Should I continue?"


async def run_send_money(ctx: Ctx, s: dict) -> Reply:
    """Personal Payments v2, PayPal-UI pattern: create token -> user approves on PayPal -> return."""
    p: Payee = s["payee"]
    amt: Money = s["amount"]
    ref = "pv-" + uuid.uuid4().hex[:12]
    body = {"receiver": {"id": p.id, "type": p.type}, "amount": amt.as_api(), "payment_type": s.get("payment_type", "PERSONAL"),
            "intent": "CAPTURE", "external_reference_id": ref}
    if s.get("note"):
        body["note_to_receiver"] = s["note"][:200]
    tok = await ctx.client.request("POST", "/v2/personal-payments/tokens", op="v2.tokens.create", json=body,
                                   user_token=_user(ctx), idempotency_key=s["_idempotency_key"])
    link = next(l["href"] for l in tok["links"] if l["rel"] == "initiate_payment")
    url = f"{link}&return_url={ctx.config.public_base_url}/paypal/return?sid={ctx.session.id}"   # one parameter pair allowed
    ctx.session.pending_token = {"id": tok["id"], "payee": p.spoken(), "amount": str(amt)}
    return Reply(f"Opening PayPal so you can approve {amt} to {p.spoken()}. I'll confirm when it's done.",
                 {"title": f"Approve {amt} to {p.spoken()}", "lines": [f"Token {tok['id']}"], "link": url},
                 actions=[{"type": "open_url", "url": url, "purpose": "approve_payment"}],
                 remember={"last_payment_token": tok["id"]})


async def run_fee_check(ctx: Ctx, s: dict) -> Reply:
    """Personal Payments v1 funding options: fees and arrival for each way to pay, without paying."""
    p: Payee = s["payee"]
    amt: Money = s["amount"]
    body = {"amount": amt.as_api("currency"), "payee": {"id": p.id, "type": p.type},
            "payment_type": s.get("payment_type", "PERSONAL"), "fee": {"payer": "PAYER"}}
    if p.name:
        body["payee"]["name"] = p.name
    if p.country_code:
        body["payee"]["country_code"] = p.country_code
    r = await ctx.client.request("POST", "/v1/payments/personal-payments/funding-options", op="personal-payments.funding-options",
                                 json=body, user_token=_user(ctx))
    fo = r["funding_options"]
    lines, spoken = [], []
    for o in fo["options"]:
        src = o["sources"][0]
        kind = {"HOLDING": "PayPal balance", "BANK_ACCOUNT": "bank account", "PAYMENT_CARD": "card", "CREDIT": "PayPal Credit"}.get(src["instrument_type"], src["instrument_type"].lower())
        if src.get("payment_card", {}).get("last_4"):
            kind += f" ending {src['payment_card']['last_4']}"
        fee = o["fee"]["amount"]
        spoken.append(f"free from your {kind}" if Decimal(fee['value']) == 0 else f"{money(fee)} by {kind}")
        lines.append(f"{kind} — fee {money(fee)}, arrives {when(o.get('estimated_funds_arrival'), ctx.now)}")
    speech = f"Sending {amt} to {p.spoken()} is " + join_and(spoken).replace(" and ", ", or ", 1) + "."
    if fo.get("regulatory_information", {}).get("remittance_transfer_rule", {}).get("action") == "DISPLAY_TRANSACTION_DISCLOSURES":
        speech += " This is an international transfer, so PayPal requires disclosures; they're on screen."
        lines.append("Remittance transfer disclosures required (shown before any payment).")
    return Reply(speech, {"title": f"Ways to send {amt}", "lines": lines})


async def run_payment_status(ctx: Ctx, s: dict) -> Reply:
    tok_id = ctx.session.context.get("last_payment_token")
    if not tok_id:
        return Reply("I don't have a recent payment from this session to check. PayPal shows your full activity in the app.",
                     {"title": "No recent payment"})
    t = await ctx.client.request("GET", f"/v2/personal-payments/tokens/{tok_id}", op="v2.tokens.get", user_token=_user(ctx))
    who = (t.get("receiver", {}).get("name", {}).get("person_name", {}).get("full_name")) or t.get("receiver", {}).get("id", "")
    if t.get("status") == "COMPLETED":
        return Reply(f"Your {money(t['amount'])} to {who} went through.", {"title": "Payment complete", "lines": [t["id"]]})
    return Reply(f"Your {money(t['amount'])} to {who} hasn't been approved yet. It's waiting for you in PayPal.",
                 {"title": "Waiting for approval", "lines": [t["id"], t.get("status", "")]})


COUNTRIES = {"mexico": "MX", "canada": "CA", "the uk": "GB", "uk": "GB", "england": "GB", "britain": "GB", "united kingdom": "GB",
             "france": "FR", "germany": "DE", "spain": "ES", "italy": "IT", "brazil": "BR", "india": "IN", "japan": "JP",
             "australia": "AU", "philippines": "PH", "nigeria": "NG", "ireland": "IE", "cuba": "CU", "us": "US",
             "the us": "US", "america": "US", "the united states": "US", "united states": "US", "singapore": "SG", "malaysia": "MY"}


async def run_capabilities(ctx: Ctx, s: dict) -> Reply:
    cc = s["country"]
    r = await ctx.client.request("POST", "/v1/payments/personal-payments/assess-capabilities", op="personal-payments.assess-capabilities",
                                 json={"payer_country_code": ctx.config.home_country, "payee_country_codes": [cc]}, user_token=_user(ctx))
    pc = next((x for x in r.get("payee_countries", []) if x.get("country_code") == cc), None)
    name = next((k.title() for k, v in COUNTRIES.items() if v == cc and not k.startswith("the ")), cc)
    if pc and pc.get("payment_types"):
        kinds = join_and([t.lower() for t in pc["payment_types"]])
        return Reply(f"Yes. You can send {kinds} payments to {name}" + (f" in {join_and(pc['currency_codes'][:3])}." if pc.get("currency_codes") else "."),
                     {"title": f"Sending to {name}", "lines": [f"Types: {kinds}", f"Currencies: {', '.join(pc.get('currency_codes', []))}"]})
    return Reply(f"PayPal doesn't support sending personal payments to {name} from your account.", {"title": f"Sending to {name}", "lines": ["Not supported"]})


# =================================================================== OWNER (the app's own PayPal business account)

async def run_balance(ctx: Ctx, s: dict) -> Reply:
    r = await ctx.client.request("GET", "/v1/reporting/balances", op="balances.get")
    bals = r.get("balances", [])
    if not bals:
        return Reply("PayPal didn't return a balance for this account.", {"title": "Balance"})
    prim = next((b for b in bals if b.get("primary")), bals[0])
    speech = f"You have {money(prim['available_balance'])} available"
    if Decimal(prim.get("withheld_balance", {}).get("value", "0")) > 0:
        speech += f", plus {money(prim['withheld_balance'])} on hold"
    speech += "."
    others = [money(b["available_balance"]) for b in bals if b is not prim and Decimal(b["available_balance"]["value"]) > 0]
    if others:
        speech += f" Also {join_and(others)}."
    lines = [f"{b['currency']}: {money(b['available_balance'])} available, {money(b.get('total_balance'))} total" for b in bals]
    lines.append(f"Updated {when(r.get('last_refresh_time'), ctx.now)} (PayPal can lag up to 3 hours)")
    return Reply(speech, {"title": "Balance", "lines": lines})


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


async def search_transactions(ctx: Ctx, rng: DateRange, **filters) -> list[dict]:
    """Transaction Search allows at most 31 days per call; split longer ranges."""
    out, start = [], rng.start
    while start < rng.end:
        end = min(start + timedelta(days=31), rng.end)
        r = await ctx.client.request("GET", "/v1/reporting/transactions", op="search.get",
                                     params={"start_date": _rfc3339(start), "end_date": _rfc3339(end),
                                             "fields": "transaction_info,payer_info", "page_size": 100, **filters})
        out.extend(r.get("transaction_details", []))
        start = end + timedelta(seconds=1)
    seen, uniq = set(), []
    for t in out:
        tid = t["transaction_info"]["transaction_id"]
        if tid not in seen:
            seen.add(tid); uniq.append(t)
    return sorted(uniq, key=lambda t: t["transaction_info"]["transaction_initiation_date"], reverse=True)


def _who(t: dict) -> str:
    pi = t.get("payer_info", {})
    return pi.get("payer_name", {}).get("alternate_full_name") or pi.get("email_address") or t["transaction_info"].get("transaction_subject", "someone")


def _item(t: dict) -> dict:
    ti = t["transaction_info"]
    return {"kind": "transaction", "id": ti["transaction_id"], "who": _who(t), "amount": ti["transaction_amount"],
            "date": ti["transaction_initiation_date"], "subject": ti.get("transaction_subject", ""),
            "code": ti.get("transaction_event_code", ""), "email": t.get("payer_info", {}).get("email_address", "")}


async def run_activity(ctx: Ctx, s: dict) -> Reply:
    rng: DateRange = s.get("range") or DateRange(ctx.now - timedelta(days=7), ctx.now, "the last 7 days")
    txns = await search_transactions(ctx, rng)
    if not txns:
        return Reply(f"No PayPal activity {_label(rng)}. New transactions can take up to 3 hours to appear.", {"title": "Activity"})
    ins = sum(Decimal(t["transaction_info"]["transaction_amount"]["value"]) for t in txns if Decimal(t["transaction_info"]["transaction_amount"]["value"]) > 0)
    outs = -sum(Decimal(t["transaction_info"]["transaction_amount"]["value"]) for t in txns if Decimal(t["transaction_info"]["transaction_amount"]["value"]) < 0)
    focus = s.get("direction")
    items = [_item(t) for t in txns]
    if focus == "in":
        items = [i for i in items if Decimal(i["amount"]["value"]) > 0]
    elif focus == "out":
        items = [i for i in items if Decimal(i["amount"]["value"]) < 0]
    head = {"in": f"You received {money({'value': ins})} {_label(rng)}", "out": f"You spent {money({'value': outs})} {_label(rng)}"}.get(
        focus, f"{_label(rng).capitalize()}: {money({'value': ins})} in, {money({'value': outs})} out")
    lines = [f"{money(i['amount'], signed=True)} {'from' if Decimal(i['amount']['value']) > 0 else 'to'} {i['who']}, {when(i['date'], ctx.now)}" for i in items]
    speech, rest = listing(head + f", across {plural(len(items), 'transaction')}.", lines)
    return Reply(speech, {"title": f"Activity {_label(rng)}", "lines": lines}, items=items, more=rest)


def _label(rng: DateRange) -> str:
    return rng.label if rng.label in ("today", "yesterday") or rng.label.startswith(("this", "last", "the")) else f"in {rng.label}"


async def run_find_payment(ctx: Ctx, s: dict) -> Reply:
    name = s["who"].lower()
    rng = s.get("range") or DateRange(ctx.now - timedelta(days=31), ctx.now, "the last 31 days")
    hits = [_item(t) for t in await search_transactions(ctx, rng)]
    hits = [h for h in hits if name in h["who"].lower() or name in h["email"].lower()]
    if not hits:
        return Reply(f"I don't see anything with {s['who'].title()} {_label(rng)}.", {"title": "No match"})
    h = hits[0]
    v = Decimal(h["amount"]["value"])
    verb = "paid you" if v > 0 else "got"
    lead = f"{h['who']} {verb} {money(h['amount'])} {when(h['date'], ctx.now)}" + (f" for {h['subject']}" if h["subject"] and v > 0 else "") + "."
    if v < 0:
        lead = f"You sent {h['who']} {money(h['amount'])} {when(h['date'], ctx.now)}."
    if len(hits) > 1:
        lead += f" {plural(len(hits) - 1, 'more transaction')} with them {_label(rng)}."
    return Reply(lead, {"title": f"Payments with {s['who'].title()}", "lines": [f"{money(x['amount'], True)} {when(x['date'], ctx.now)} {x['subject']}" for x in hits]}, items=hits)


def _inv_item(i: dict) -> dict:
    bi = i.get("primary_recipients", [{}])[0].get("billing_info", {})
    nm = bi.get("name", {})
    who = " ".join(filter(None, [nm.get("given_name"), nm.get("surname")])) or bi.get("email_address", "someone")
    return {"kind": "invoice", "id": i["id"], "who": who, "email": bi.get("email_address", ""), "status": i["status"],
            "amount": i.get("amount"), "due": i.get("due_amount"), "number": i.get("detail", {}).get("invoice_number"),
            "date": i.get("detail", {}).get("invoice_date"), "due_date": i.get("detail", {}).get("payment_term", {}).get("due_date")}


async def run_invoices(ctx: Ctx, s: dict) -> Reply:
    want_open = s.get("filter") != "all"
    if want_open:
        r = await ctx.client.request("POST", "/v2/invoicing/search-invoices", op="invoices.search-invoices",
                                     json={"status": ["SENT", "UNPAID", "PARTIALLY_PAID"]}, params={"page_size": 20})
    else:
        r = await ctx.client.request("GET", "/v2/invoicing/invoices", op="invoices.list", params={"page_size": 20, "total_required": "true"})
    items = [_inv_item(i) for i in r.get("items", [])]
    if not items:
        return Reply("No unpaid invoices. Everyone's paid up." if want_open else "You don't have any invoices yet.", {"title": "Invoices"})
    total = sum(Decimal((i["due"] or i["amount"] or {"value": "0"})["value"]) for i in items)
    settled = ("PAID", "MARKED_AS_PAID", "CANCELLED")
    lines = [f"{i['who']}, {money(i['amount'] if i['status'] in settled else i['due'] or i['amount'])}, {i['status'].replace('_', ' ').lower()}" + (f", due {when(i['due_date'], ctx.now)}" if i.get("due_date") and i["status"] not in settled else "") for i in items]
    intro = (f"{plural(len(items), 'unpaid invoice')} totaling {money({'value': total})}." if want_open
             else f"{plural(len(items), 'invoice')}.")
    speech, rest = listing(intro, lines)
    return Reply(speech, {"title": "Unpaid invoices" if want_open else "Invoices", "lines": lines}, items=items, more=rest,
                 suggestions=["Remind the first one"])


def confirm_invoice(ctx: Ctx, s: dict) -> str:
    p: Payee = s["payee"]
    return f"Send {p.spoken()}{' at ' + p.id if p.name else ''} an invoice for {s['amount']}" + (f" for {s['note']}" if s.get("note") else "") + ". Send it?"


async def run_invoice_create(ctx: Ctx, s: dict) -> Reply:
    p: Payee = s["payee"]
    amt: Money = s["amount"]
    num = await ctx.client.request("POST", "/v2/invoicing/generate-next-invoice-number", op="invoicing.generate-next-invoice-number")
    given, _, sur = (p.name or "").partition(" ")
    body = {"detail": {"invoice_number": num.get("invoice_number"), "currency_code": amt.currency,
                       "invoice_date": ctx.now.strftime("%Y-%m-%d"), "payment_term": {"term_type": "NET_15"}},
            "primary_recipients": [{"billing_info": {"email_address": p.id, **({"name": {"given_name": given, **({"surname": sur} if sur else {})}} if given else {})}}],
            "items": [{"name": (s.get("note") or "Services")[:200], "quantity": "1", "unit_amount": amt.as_api(), "unit_of_measure": "QUANTITY"}]}
    inv = await ctx.client.request("POST", "/v2/invoicing/invoices", op="invoices.create", json=body,
                                   idempotency_key=s["_idempotency_key"] + "-create")
    iid = inv.get("id") or inv.get("href", "").rstrip("/").split("/")[-1]
    await ctx.client.request("POST", f"/v2/invoicing/invoices/{iid}/send", op="invoices.send",
                             json={"send_to_invoicer": True}, idempotency_key=s["_idempotency_key"] + "-send")
    return Reply(f"Sent. {p.spoken()} has an invoice for {amt}" + (f" for {s['note']}" if s.get('note') else "") + ".",
                 {"title": f"Invoice {num.get('invoice_number', '')} sent", "lines": [iid, p.id, str(amt)]},
                 remember={"last_invoice_id": iid})


def _pick(ctx: Ctx, s: dict, kind: str) -> dict | None:
    ref = s.get("ref")
    return ref if isinstance(ref, dict) and ref.get("kind") == kind else None


async def run_invoice_remind(ctx: Ctx, s: dict) -> Reply:
    inv = _pick(ctx, s, "invoice")
    await ctx.client.request("POST", f"/v2/invoicing/invoices/{inv['id']}/remind", op="invoices.remind",
                             json={"send_to_invoicer": True})
    return Reply(f"Reminder sent to {inv['who']} for {money(inv['due'] or inv['amount'])}.", {"title": "Reminder sent", "lines": [inv["id"]]})


def confirm_invoice_cancel(ctx: Ctx, s: dict) -> str:
    inv = s["ref"]
    return f"Cancel {inv['who']}'s invoice for {money(inv['due'] or inv['amount'])}? They'll be told it's cancelled."


async def run_invoice_cancel(ctx: Ctx, s: dict) -> Reply:
    inv = _pick(ctx, s, "invoice")
    await ctx.client.request("POST", f"/v2/invoicing/invoices/{inv['id']}/cancel", op="invoices.cancel",
                             json={"send_to_invoicer": True, "send_to_recipient": True}, idempotency_key=s["_idempotency_key"])
    return Reply(f"Cancelled {inv['who']}'s invoice.", {"title": "Invoice cancelled", "lines": [inv["id"]]})


async def run_payment_link(ctx: Ctx, s: dict) -> Reply:
    amt: Money = s["amount"]
    body = {"integration_mode": "LINK", "type": "BUY_NOW", "reusable": "MULTIPLE",
            "line_items": [{"name": (s.get("note") or "Payment")[:127], "unit_amount": amt.as_api()}]}
    r = await ctx.client.request("POST", "/v1/checkout/payment-resources", op="payment-resources.post", json=body,
                                 idempotency_key=s["_idempotency_key"])
    return Reply(f"Your {amt} payment link for {s.get('note') or 'this item'} is ready. It's on screen to copy or share.",
                 {"title": "Payment link", "lines": [r.get("payment_link", "")], "link": r.get("payment_link")},
                 actions=[{"type": "copy", "text": r.get("payment_link", "")}])


def confirm_refund(ctx: Ctx, s: dict) -> str:
    t = s["ref"]
    amt = s.get("amount")
    part = f"{amt} of " if amt else "all "
    return f"Refund {part}{money(t['amount'])} to {t['who']} from {when(t['date'], ctx.now)}? This can't be undone."


async def run_refund(ctx: Ctx, s: dict) -> Reply:
    t = _pick(ctx, s, "transaction")
    body = {"amount": s["amount"].as_api()} if s.get("amount") else {}
    r = await ctx.client.request("POST", f"/v2/payments/captures/{t['id']}/refund", op="captures.refund", json=body,
                                 idempotency_key=s["_idempotency_key"])
    amt = r.get("amount") or t["amount"]
    return Reply(f"Refunded {money(amt)} to {t['who']}." + ("" if r.get("status") == "COMPLETED" else " It's pending at PayPal."),
                 {"title": "Refund", "lines": [r.get("id", ""), r.get("status", "")]},
                 remember={"last_refund": {"id": r.get("id"), "who": t["who"]}}, suggestions=["Did the refund go through"])


async def run_disputes(ctx: Ctx, s: dict) -> Reply:
    r = await ctx.client.request("GET", "/v1/customer/disputes", op="disputes.list", params={"page_size": 10})
    open_ = [d for d in r.get("items", []) if d.get("status") not in ("RESOLVED", "CLOSED")]
    if not open_:
        return Reply("No open disputes.", {"title": "Disputes"})
    items = [{"kind": "dispute", "id": d["dispute_id"], "amount": d.get("dispute_amount"), "reason": d.get("reason", ""),
              "status": d.get("status", ""), "due": d.get("seller_response_due_date")} for d in open_]
    reason = lambda x: x.replace("_", " ").lower().replace("merchandise or service ", "item ")
    lines = [f"{money(i['amount'])}, {reason(i['reason'])}" + (f", respond by {when(i['due'], ctx.now)}" if i["due"] else "") for i in items]
    speech, rest = listing(f"{plural(len(items), 'open dispute')}.", lines)
    return Reply(speech, {"title": "Open disputes", "lines": lines}, items=items, more=rest)


def confirm_dispute_accept(ctx: Ctx, s: dict) -> str:
    d = s["ref"]
    return f"Accept the {money(d['amount'])} claim? The buyer gets refunded and you lose the dispute. This can't be undone."


async def run_dispute_accept(ctx: Ctx, s: dict) -> Reply:
    d = _pick(ctx, s, "dispute")
    await ctx.client.request("POST", f"/v1/customer/disputes/{d['id']}/accept-claim", op="disputes.accept-claim",
                             json={"note": "Accepted by voice in PayPal Voice."}, idempotency_key=s["_idempotency_key"])
    return Reply(f"Claim accepted. The buyer will get {money(d['amount'])} back.", {"title": "Claim accepted", "lines": [d["id"]]})


async def run_subscriptions(ctx: Ctx, s: dict) -> Reply:
    r = await ctx.client.request("GET", "/v1/billing/subscriptions", op="subscriptions.list", params={"page_size": 20})
    subs = [x for x in r.get("subscriptions", []) if x.get("status") in ("ACTIVE", "SUSPENDED")]
    if not subs:
        return Reply("No active subscriptions.", {"title": "Subscriptions"})
    items = []
    for x in subs:
        nm = x.get("subscriber", {}).get("name", {})
        items.append({"kind": "subscription", "id": x["id"], "status": x.get("status"), "who": " ".join(filter(None, [nm.get("given_name"), nm.get("surname")])) or x.get("subscriber", {}).get("email_address", ""),
                      "amount": x.get("billing_info", {}).get("last_payment", {}).get("amount"),
                      "next": x.get("billing_info", {}).get("next_billing_time")})
    lines = [f"{i['who']}, {money(i['amount'])}" + (", paused" if i["status"] == "SUSPENDED" else f", next bill {when(i['next'], ctx.now)}" if i["next"] else "") for i in items]
    active = sum(1 for i in items if i["status"] == "ACTIVE")
    intro = f"{plural(active, 'active subscription')}" + (f" and {len(items) - active} paused" if active != len(items) else "") + "."
    speech, rest = listing(intro, lines)
    return Reply(speech, {"title": "Subscriptions", "lines": lines}, items=items, more=rest)


def confirm_sub_cancel(ctx: Ctx, s: dict) -> str:
    return f"Cancel {s['ref']['who']}'s subscription? Billing stops and it can't be restarted."


async def run_sub_cancel(ctx: Ctx, s: dict) -> Reply:
    sub = _pick(ctx, s, "subscription")
    await ctx.client.request("POST", f"/v1/billing/subscriptions/{sub['id']}/cancel", op="subscriptions.cancel",
                             json={"reason": "Cancelled by the merchant by voice."}, idempotency_key=s["_idempotency_key"])
    return Reply(f"Cancelled {sub['who']}'s subscription.", {"title": "Subscription cancelled", "lines": [sub["id"]]})


def confirm_payout(ctx: Ctx, s: dict) -> str:
    p: Payee = s["payee"]
    return f"Pay out {s['amount']} to {p.spoken()}{' at ' + p.id if p.name else ''} from your business balance? Payouts can't be pulled back once claimed."


async def run_payout(ctx: Ctx, s: dict) -> Reply:
    p: Payee = s["payee"]
    amt: Money = s["amount"]
    body = {"sender_batch_header": {"sender_batch_id": s["_idempotency_key"][:30], "email_subject": "You have a payment",
                                    "email_message": (s.get("note") or "Payment")[:200]},
            "items": [{"recipient_type": "EMAIL" if p.type == "EMAIL" else "PHONE", "receiver": p.id,
                       "amount": amt.as_api("currency"), "note": (s.get("note") or "Thank you")[:200]}]}
    r = await ctx.client.request("POST", "/v1/payments/payouts", op="payouts.post", json=body, idempotency_key=s["_idempotency_key"])
    bh = r.get("batch_header", {})
    return Reply(f"Payout of {amt} to {p.spoken()} is {bh.get('batch_status', 'submitted').lower()}.",
                 {"title": "Payout", "lines": [bh.get("payout_batch_id", ""), bh.get("batch_status", "")]},
                 remember={"last_payout": {"id": bh.get("payout_batch_id"), "who": p.spoken(), "amount": str(amt)}},
                 suggestions=["Did the payout go through"])


# ------------------------------------------------------------------- details, status and lifecycle

_INV_STATUS = {"SENT": "sent and unpaid", "UNPAID": "unpaid", "PARTIALLY_PAID": "partly paid", "PAID": "paid",
               "MARKED_AS_PAID": "marked as paid", "CANCELLED": "cancelled", "DRAFT": "a draft", "SCHEDULED": "scheduled"}


async def run_invoice_details(ctx: Ctx, s: dict) -> Reply:
    inv = _pick(ctx, s, "invoice")
    r = await ctx.client.request("GET", f"/v2/invoicing/invoices/{inv['id']}", op="invoices.get")
    i = _inv_item(r)
    owed = Decimal((i["due"] or {"value": "0"})["value"]) > 0
    status = _INV_STATUS.get(i["status"], i["status"].replace("_", " ").lower())
    speech = f"Invoice {i['number']} to {i['who']} for {money(i['amount'])} is {status}."
    if owed and i.get("due_date"):
        speech += f" It's due {when(i['due_date'], ctx.now)}."
    lines = [f"Invoice {i['number']}", i["who"], f"{money(i['amount'])}, {status}"] + ([f"Due {i['due_date']}"] if i.get("due_date") else [])
    return Reply(speech, {"title": "Invoice", "lines": lines}, items=[i],
                 suggestions=["Remind them", "Mark it as paid"] if owed else [])


async def run_invoice_mark_paid(ctx: Ctx, s: dict) -> Reply:
    inv = _pick(ctx, s, "invoice")
    due = inv["due"] or inv["amount"]
    r = await ctx.client.request("POST", f"/v2/invoicing/invoices/{inv['id']}/payments", op="invoices.payments",
                                 json={"method": "OTHER", "payment_date": ctx.now.strftime("%Y-%m-%d"), "amount": due,
                                       "note": "Recorded by voice in PayPal Voice."}, idempotency_key=s["_idempotency_key"])
    return Reply(f"Done. {inv['who']}'s invoice is marked paid for {money(due)}.",
                 {"title": "Payment recorded", "lines": [inv["id"], r.get("payment_id", "")]})


_REASON = {"MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "the item wasn't received", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "the item wasn't as described",
           "UNAUTHORISED": "the payment wasn't authorized", "CREDIT_NOT_PROCESSED": "a refund wasn't processed",
           "DUPLICATE_TRANSACTION": "they were charged twice", "INCORRECT_AMOUNT": "the amount was wrong"}


async def run_dispute_details(ctx: Ctx, s: dict) -> Reply:
    d = _pick(ctx, s, "dispute")
    r = await ctx.client.request("GET", f"/v1/customer/disputes/{d['id']}", op="disputes.get")
    txn = (r.get("disputed_transactions") or [{}])[0]
    buyer = txn.get("buyer", {}).get("name", "The buyer")
    reason = _REASON.get(r.get("reason", ""), r.get("reason", "something").replace("_", " ").lower())
    speech = f"{buyer} says {reason}, and is disputing {money(r.get('dispute_amount'))}."
    due = r.get("seller_response_due_date")
    if r.get("status") == "WAITING_FOR_SELLER_RESPONSE":
        speech += " PayPal is waiting for your response" + (f", due {when(due, ctx.now)}." if due else ".")
    else:
        speech += f" Status: {r.get('status', 'unknown').replace('_', ' ').lower()}."
    item = {"kind": "dispute", "id": r["dispute_id"], "amount": r.get("dispute_amount"), "reason": r.get("reason", ""),
            "status": r.get("status", ""), "due": due, "who": buyer}
    return Reply(speech, {"title": "Dispute " + r["dispute_id"], "lines": [buyer, money(r.get("dispute_amount")), reason,
                                                                         r.get("status", "").replace("_", " ").lower()]},
                 items=[item], suggestions=["Accept the claim"])


async def run_subscription_details(ctx: Ctx, s: dict) -> Reply:
    sub = _pick(ctx, s, "subscription")
    r = await ctx.client.request("GET", f"/v1/billing/subscriptions/{sub['id']}", op="subscriptions.get")
    bi = r.get("billing_info", {})
    amt = bi.get("last_payment", {}).get("amount")
    status = {"ACTIVE": "active", "SUSPENDED": "paused", "CANCELLED": "cancelled", "EXPIRED": "expired"}.get(r.get("status"), "pending")
    speech = f"{sub['who']}'s subscription is {status} at {money(amt)} a cycle."
    if r.get("status") == "ACTIVE" and bi.get("next_billing_time"):
        speech += f" Next bill {when(bi['next_billing_time'], ctx.now)}."
    return Reply(speech, {"title": "Subscription", "lines": [sub["who"], r["id"], f"{money(amt)}, {status}", r.get("plan_id", "")]},
                 items=[{**sub, "status": r.get("status")}],
                 suggestions=["Pause it"] if r.get("status") == "ACTIVE" else ["Resume it"] if r.get("status") == "SUSPENDED" else [])


async def run_sub_pause(ctx: Ctx, s: dict) -> Reply:
    sub = _pick(ctx, s, "subscription")
    if sub.get("status") == "SUSPENDED":
        return Reply(f"{sub['who']}'s subscription is already paused.", {"title": "Already paused"})
    await ctx.client.request("POST", f"/v1/billing/subscriptions/{sub['id']}/suspend", op="subscriptions.suspend",
                             json={"reason": "Paused by the merchant by voice."}, idempotency_key=s["_idempotency_key"])
    return Reply(f"Paused {sub['who']}'s subscription. Billing stops until you resume it.",
                 {"title": "Subscription paused", "lines": [sub["id"]]}, items=[{**sub, "status": "SUSPENDED"}], suggestions=["Resume it"])


async def run_sub_resume(ctx: Ctx, s: dict) -> Reply:
    sub = _pick(ctx, s, "subscription")
    if sub.get("status") == "ACTIVE":
        return Reply(f"{sub['who']}'s subscription is already active.", {"title": "Already active"})
    await ctx.client.request("POST", f"/v1/billing/subscriptions/{sub['id']}/activate", op="subscriptions.activate",
                             json={"reason": "Resumed by the merchant by voice."}, idempotency_key=s["_idempotency_key"])
    return Reply(f"Resumed {sub['who']}'s subscription. Billing is back on.",
                 {"title": "Subscription resumed", "lines": [sub["id"]]}, items=[{**sub, "status": "ACTIVE"}])


async def run_payout_status(ctx: Ctx, s: dict) -> Reply:
    last = ctx.session.context.get("last_payout")
    if not last or not last.get("id"):
        return Reply("I don't have a payout from this session to check. Make one first, like: pay out 30 dollars to jordan@example.com.",
                     {"title": "No recent payout"})
    r = await ctx.client.request("GET", f"/v1/payments/payouts/{last['id']}", op="payouts.get")
    st = r.get("batch_header", {}).get("batch_status", "PENDING")
    word = {"SUCCESS": "went through", "PENDING": "is still pending", "PROCESSING": "is processing",
            "DENIED": "was denied", "CANCELED": "was cancelled"}.get(st, "is " + st.lower())
    return Reply(f"Your {last['amount']} payout to {last['who']} {word}.", {"title": "Payout status", "lines": [last["id"], st]})


async def run_refund_status(ctx: Ctx, s: dict) -> Reply:
    last = ctx.session.context.get("last_refund")
    if not last or not last.get("id"):
        return Reply("I don't have a refund from this session to check. Refund a payment first, like: refund sam.",
                     {"title": "No recent refund"})
    r = await ctx.client.request("GET", f"/v2/payments/refunds/{last['id']}", op="refunds.get")
    st = r.get("status", "PENDING")
    word = {"COMPLETED": "went through", "PENDING": "is still pending", "FAILED": "failed", "CANCELLED": "was cancelled"}.get(st, "is " + st.lower())
    return Reply(f"The {money(r.get('amount'))} refund to {last['who']} {word}.", {"title": "Refund status", "lines": [last["id"], st]})


async def run_plans(ctx: Ctx, s: dict) -> Reply:
    r = await ctx.client.request("GET", "/v1/billing/plans", op="plans.list", params={"page_size": 20})
    plans = r.get("plans", [])
    if not plans:
        return Reply("You don't have any subscription plans yet.", {"title": "Plans"})
    lines = [f"{p.get('name', p['id'])}, {p.get('status', '').lower()}" for p in plans]
    speech, rest = listing(f"You offer {plural(len(plans), 'plan')}.", lines)
    return Reply(speech, {"title": "Subscription plans", "lines": lines}, more=rest)


async def run_products(ctx: Ctx, s: dict) -> Reply:
    r = await ctx.client.request("GET", "/v1/catalogs/products", op="products.list", params={"page_size": 20})
    prods = r.get("products", [])
    if not prods:
        return Reply("Your catalog is empty.", {"title": "Products"})
    lines = [p.get("name", p["id"]) for p in prods]
    speech, rest = listing(f"{plural(len(prods), 'product')} in your catalog.", lines)
    return Reply(speech, {"title": "Catalog products", "lines": lines}, more=rest)


async def run_exchange(ctx: Ctx, s: dict) -> Reply:
    amt: Money = s.get("amount") or Money(Decimal(1), "USD")
    target = s["currency"]
    r = await ctx.client.request("POST", "/v2/pricing/quote-exchange-rates", op="quote_exchange_rates",
                                 json={"quote_items": [{"base_currency": amt.currency, "base_amount": f"{amt.value:.2f}", "quote_currency": target}]})
    q = (r.get("exchange_rate_quotes") or [{}])[0]
    return Reply(f"{amt} is about {money(q.get('quote_amount'))} at PayPal's rate of {q.get('exchange_rate', '?')}.",
                 {"title": "Exchange rate", "lines": [f"{amt} → {money(q.get('quote_amount'))}", f"Rate {q.get('exchange_rate')}"]})


# =================================================================== table

def _cap(**kw) -> Capability:
    return Capability(**kw)


MONEY = Slot("amount", "money", True, "How much?")
PAYEE = Slot("payee", "payee", True, "Who should get it? Say a name, email or phone number.")
NOTE = Slot("note", "text", False)

CAPABILITIES: list[Capability] = [
    # ---------------- personal
    _cap(name="whoami", summary="Tell you which PayPal account you're signed in with", examples=["who am i signed in as", "which paypal account is this"],
         patterns=[r"\bwho am i\b", r"\b(which|what) (paypal )?account\b", r"\bam i (signed|logged) in\b", r"\bmy (paypal )?(profile|email address)\b"],
         mode=PERSONAL, availability=LIMITED, tier="T0", ops=["userinfo.get"], run=run_whoami),
    _cap(name="send_money", summary="Send money to a person (you approve it in PayPal)",
         examples=["send 20 dollars to sam", "pay maya 12.50 for dinner", "send forty bucks to sam@example.com", "pay jordan 60 for the print as goods and services"],
         patterns=[r"^(?:can you |could you |i want to |i'd like to |i need to |go ahead and )?(send|pay|give|transfer|tip|zelle)\b",
                   r"\b(send|pay) (\w+ ){0,3}(\$|\d)"],
         exclude=[r"\b(invoice|bill|request|refund|payout|pay ?out|link|fee|fees|cost|charge|subscription)\b", r"\bpay (my|the) (bill|invoice)\b",
                  r"\bwho (did i )?pa(y|id)\b", r"\b(did|has) \w+ (pay|paid|send|sent) (me|us)\b",
                  r"\b(pay|paying|give|send) (attention|respects?|tribute|homage|heed|a visit|off|up|my regards|thanks|love)\b"],
         slots=[PAYEE, MONEY, NOTE, Slot("payment_type", "payment_type", False)],
         mode=PERSONAL, availability=LIMITED, tier="T2", ops=["v2.tokens.create"], run=run_send_money, confirm=confirm_send, money_slot="amount",
         error_hint="Personal Payments is a limited-release API; PayPal must approve the app (p2papiapproval@paypal.com)."),
    _cap(name="fee_check", summary="Check fees and arrival time before sending", examples=["how much does it cost to send 50 dollars to sam", "what's the fee to send 100 pounds to maya"],
         patterns=[r"\b(fee|fees|cost|charge)\b.*\bsend", r"\bsend\b.*\b(fee|fees|cost)\b", r"\bhow much (does it|would it|will it) cost\b"],
         slots=[PAYEE, MONEY], mode=PERSONAL, availability=LIMITED, tier="T0", ops=["personal-payments.funding-options"], run=run_fee_check, priority=2),
    _cap(name="payment_status", summary="Check whether your last payment went through", examples=["did my payment go through", "did sam get the money"],
         patterns=[r"\b(did|has) (my|the|that) (payment|money|transfer)\b", r"\b(go|gone|went) through\b", r"\bdid \w+ get (the|my) (money|payment)\b", r"\bpayment status\b"],
         mode=PERSONAL, availability=LIMITED, tier="T0", ops=["v2.tokens.get"], run=run_payment_status, priority=1),
    _cap(name="send_capabilities", summary="Check whether you can send money to a country", examples=["can i send money to mexico", "can i pay someone in the uk"],
         patterns=[r"\bcan i (send|pay)\b.*\b(to|in) (" + "|".join(sorted(COUNTRIES, key=len, reverse=True)) + r")\b"],
         slots=[Slot("country", "country", True, "Which country?")], mode=PERSONAL, availability=LIMITED, tier="T0",
         ops=["personal-payments.assess-capabilities"], run=run_capabilities, priority=3),
    # ---------------- owner
    _cap(name="balance", summary="Read your PayPal balance", examples=["what's my balance", "how much money do i have", "paypal balance"],
         patterns=[r"\bbalance\b", r"\bhow much (money )?(do i have|have i got|is in|is there)\b", r"\bwhat'?s in my (paypal|account)\b"],
         exclude=[r"\bsend\b"], mode=OWNER, availability=GA, tier="T0", ops=["balances.get"], run=run_balance,
         error_hint="Turn on Transaction Search for this app in the PayPal developer dashboard; it can take up to 9 hours."),
    _cap(name="activity", summary="Summarize money in and out for a period", examples=["what came in this week", "how much did i make last month", "recent activity", "what did i spend yesterday"],
         patterns=[r"\b(recent|latest|last few) (activity|transactions|payments)\b", r"\b(activity|transactions)\b",
                   r"\bhow much (did i|have i) (make|made|earn|earned|get|got|receive|received|sell|sold|spend|spent)\b",
                   r"\bwhat (came|come|has come) in\b", r"\bwho paid me\b", r"\bwhat did i (spend|get|receive|make)\b", r"\bmoney (in|out)\b"],
         slots=[Slot("range", "date_range", False)], mode=OWNER, availability=GA, tier="T0", ops=["search.get"], run=run_activity,
         error_hint="Turn on Transaction Search for this app in the PayPal developer dashboard; it can take up to 9 hours."),
    _cap(name="find_payment", summary="Find payments from or to a person", examples=["did jordan pay me", "has priya paid", "when did sam pay me"],
         patterns=[r"\b(did|has|have) (?!i\b|you\b|my\b|the\b)([a-z]+) (ever )?(pay|paid|send|sent)( me| us)?\b", r"\bwhen did (?!i\b)([a-z]+) pay\b"],
         slots=[Slot("who", "who", True, "Who are you asking about?"), Slot("range", "date_range", False)],
         mode=OWNER, availability=GA, tier="T0", ops=["search.get"], run=run_find_payment, priority=4),
    _cap(name="invoices", summary="List unpaid invoices", examples=["who owes me money", "any unpaid invoices", "show my invoices"],
         patterns=[r"\binvoices\b", r"\bwho owes me\b", r"\b(unpaid|open|outstanding|overdue) (invoice|bill)s?\b", r"\binvoice status\b"],
         exclude=[r"\b(send|create|make|new|remind|cancel)\b.*\binvoice\b"], slots=[Slot("filter", "invoice_filter", False)],
         mode=OWNER, availability=GA, tier="T0", ops=["invoices.search-invoices", "invoices.list"], run=run_invoices),
    _cap(name="invoice_create", summary="Send someone an invoice", examples=["bill sam 40 dollars for framing", "send an invoice to jordan@example.com for 85 dollars", "request 25 from maya for prints"],
         patterns=[r"^(?:please )?(bill|invoice|charge)\b", r"\b(send|create|make|start|draft)( an?)?( new)? invoice\b", r"\brequest (\$?\d|money|payment)", r"\brequest .* from\b"],
         exclude=[r"\b(remind|cancel)\b"], slots=[PAYEE, MONEY, Slot("note", "text", False)],
         mode=OWNER, availability=GA, tier="T2", ops=["invoicing.generate-next-invoice-number", "invoices.create", "invoices.send"],
         run=run_invoice_create, confirm=confirm_invoice, priority=3),
    _cap(name="invoice_remind", summary="Remind someone about an unpaid invoice", examples=["remind jordan about the invoice", "send a reminder", "remind the first one"],
         patterns=[r"\bremind\b", r"\breminder\b"], slots=[Slot("ref", "ref", True, "Which invoice? Say the person's name.")], ref_kind="invoice",
         mode=OWNER, availability=GA, tier="T1", ops=["invoices.remind"], run=run_invoice_remind,
         confirm=lambda c, s: f"Remind {s['ref']['who']} about {money(s['ref']['due'] or s['ref']['amount'])}?", priority=5),
    _cap(name="invoice_cancel", summary="Cancel a sent invoice", examples=["cancel priya's invoice", "cancel the invoice to jordan"],
         patterns=[r"\bcancel\b.*\binvoice\b", r"\binvoice\b.*\bcancel\b"], slots=[Slot("ref", "ref", True, "Which invoice? Say the person's name.")],
         ref_kind="invoice", mode=OWNER, availability=GA, tier="T2", ops=["invoices.cancel"], run=run_invoice_cancel,
         confirm=confirm_invoice_cancel, priority=6),
    _cap(name="payment_link", summary="Make a payment link to share", examples=["make a payment link for 25 dollars for a small print", "create a buy now link for 120"],
         patterns=[r"\b(payment|pay|checkout|buy now|purchase) (link|button)\b", r"\blink (to|for) (pay|buy)"],
         slots=[MONEY, Slot("note", "text", False, "What's it for?")], mode=OWNER, availability=GA, tier="T1",
         ops=["payment-resources.post"], run=run_payment_link,
         confirm=lambda c, s: f"Create a {s['amount']} payment link" + (f" for {s['note']}" if s.get('note') else "") + "?", priority=4),
    _cap(name="refund", summary="Refund a payment you received", examples=["refund sam", "refund the last payment", "refund 10 dollars to jordan"],
         patterns=[r"\brefund\b", r"\b(give|send) (\w+ )?(their|his|her|the) money back\b"],
         exclude=[r"\b(my|a) refund\b.*\b(status|where)\b"], slots=[Slot("ref", "ref", True, "Which payment? Say who paid you."), Slot("amount", "money", False)],
         ref_kind="transaction", mode=OWNER, availability=GA, tier="T2", ops=["search.get", "captures.refund"], run=run_refund,
         confirm=confirm_refund, money_slot="amount", priority=5),
    _cap(name="disputes", summary="List open disputes and claims", examples=["any disputes", "do i have any claims", "open cases"],
         patterns=[r"\b(dispute|disputes|claim|claims|chargeback|chargebacks|open cases)\b"], exclude=[r"\baccept\b"],
         mode=OWNER, availability=GA, tier="T0", ops=["disputes.list"], run=run_disputes),
    _cap(name="dispute_accept", summary="Accept a dispute claim (refunds the buyer)", examples=["accept the claim", "accept the first dispute"],
         patterns=[r"\baccept (the |that |this )?(first |last )?(claim|dispute)\b"], slots=[Slot("ref", "ref", True, "Which dispute?")],
         ref_kind="dispute", mode=OWNER, availability=GA, tier="T2", ops=["disputes.accept-claim"], run=run_dispute_accept,
         confirm=confirm_dispute_accept, priority=5),
    _cap(name="subscriptions", summary="List active subscriptions", examples=["show my subscriptions", "who's subscribed"],
         patterns=[r"\bsubscriptions?\b", r"\bsubscribers?\b", r"\bsubscribed\b"], exclude=[r"\bcancel\b"],
         mode=OWNER, availability=GA, tier="T0", ops=["subscriptions.list"], run=run_subscriptions),
    _cap(name="subscription_cancel", summary="Cancel a subscriber's subscription", examples=["cancel maya's subscription"],
         patterns=[r"\bcancel\b.*\bsubscription\b"], slots=[Slot("ref", "ref", True, "Whose subscription?")], ref_kind="subscription",
         mode=OWNER, availability=GA, tier="T2", ops=["subscriptions.cancel"], run=run_sub_cancel, confirm=confirm_sub_cancel, priority=6),
    _cap(name="payout", summary="Pay a contractor or vendor from your business balance", examples=["pay out 50 dollars to sam@example.com", "send a payout to jordan for 30"],
         patterns=[r"\bpay ?out\b", r"\bpayouts?\b", r"\bpay (my |the )?(contractor|vendor|artist|freelancer|supplier)\b"],
         slots=[PAYEE, MONEY, NOTE], mode=OWNER, availability=APPROVAL, tier="T2", ops=["payouts.post"], run=run_payout,
         confirm=confirm_payout, money_slot="amount", priority=6,
         error_hint="Payouts must be enabled for your business account; request access in the PayPal developer dashboard."),
    _cap(name="exchange_rate", summary="Quote a currency conversion", examples=["how much is 100 dollars in euros", "exchange rate to pounds"],
         patterns=[r"\bexchange rate\b", r"\bconvert\b", r"\bhow much is .* in (euros?|pounds?|pesos?|canadian|yen)\b"],
         slots=[Slot("amount", "money", False), Slot("currency", "currency", True, "Into which currency?")],
         mode=OWNER, availability=GA, tier="T0", ops=["quote_exchange_rates"], run=run_exchange, priority=4),
    _cap(name="invoice_details", summary="Read one invoice's status and due date", examples=["jordan's invoice details", "what's the status of priya's invoice"],
         patterns=[r"\b(details?|status|look up|pull up|open|show|read|tell me about|what about|what'?s (up|going on) with)\b.*\binvoice\b", r"\binvoice\b.*\b(details?|due)\b", r"\b[a-z]+'s invoice$"],
         exclude=[r"\b(remind|reminder|cancel|mark|record|send|create|make|new|draft|bill)\b", r"\binvoice status\b"],
         slots=[Slot("ref", "ref", True, "Which invoice? Say the person's name.")], ref_kind="invoice",
         mode=OWNER, availability=GA, tier="T0", ops=["invoices.get"], run=run_invoice_details, priority=5),
    _cap(name="invoice_mark_paid", summary="Record a payment you got outside PayPal", examples=["mark jordan's invoice as paid", "record a payment for priya's invoice"],
         patterns=[r"\bmark\b.*\b(paid|invoice)\b", r"\b(record|log) (a |the )?payment\b", r"\b[a-z]+ paid (me )?(in |by |with )?(cash|check|cheque)\b"],
         slots=[Slot("ref", "ref", True, "Which invoice? Say the person's name.")], ref_kind="invoice",
         mode=OWNER, availability=GA, tier="T1", ops=["invoices.payments"], run=run_invoice_mark_paid,
         confirm=lambda c, s: f"Mark {s['ref']['who']}'s invoice for {money(s['ref']['due'] or s['ref']['amount'])} as paid outside PayPal?", priority=7),
    _cap(name="dispute_details", summary="Explain what a dispute is about", examples=["what's the dispute about", "dispute details"],
         patterns=[r"\b(details?|tell me (more )?about|what'?s|what is|why|explain|more (info|information) (on|about))\b.*\b(dispute|claim|case|chargeback)\b",
                   r"\b(dispute|claim|case|chargeback) details\b"],
         exclude=[r"\baccept\b"], slots=[Slot("ref", "ref", True, "Which dispute?")], ref_kind="dispute",
         mode=OWNER, availability=GA, tier="T0", ops=["disputes.get"], run=run_dispute_details, priority=3),
    _cap(name="subscription_details", summary="Read one subscription's status and next bill", examples=["when does maya's subscription renew", "maya's subscription details"],
         patterns=[r"\b(when|details?|status|tell me about|next (bill|billing|charge|payment))\b.*\bsubscription\b",
                   r"\bsubscription\b.*\b(renew|renews|details?|status|next bill)\b", r"\b[a-z]+'s subscription$"],
         exclude=[r"\b(cancel|pause|suspend|hold|resume|reactivate|restart|unpause|stop)\b"],
         slots=[Slot("ref", "ref", True, "Whose subscription?")], ref_kind="subscription",
         mode=OWNER, availability=GA, tier="T0", ops=["subscriptions.get"], run=run_subscription_details, priority=4),
    _cap(name="subscription_pause", summary="Pause a subscriber's billing", examples=["pause maya's subscription"],
         patterns=[r"\b(pause|suspend|hold|freeze)\b.*\bsubscription\b", r"^pause (it|that|them)$"],
         slots=[Slot("ref", "ref", True, "Whose subscription?")], ref_kind="subscription",
         mode=OWNER, availability=GA, tier="T1", ops=["subscriptions.suspend"], run=run_sub_pause,
         confirm=lambda c, s: f"Pause {s['ref']['who']}'s subscription? They won't be billed until you resume it.", priority=6),
    _cap(name="subscription_resume", summary="Resume a paused subscription", examples=["resume maya's subscription"],
         patterns=[r"\b(resume|reactivate|restart|re-start|unpause)\b.*\bsubscription\b", r"^(resume|unpause|reactivate) (it|that|them)$"],
         slots=[Slot("ref", "ref", True, "Whose subscription?")], ref_kind="subscription",
         mode=OWNER, availability=GA, tier="T1", ops=["subscriptions.activate"], run=run_sub_resume,
         confirm=lambda c, s: f"Resume {s['ref']['who']}'s subscription? Billing starts again.", priority=6),
    _cap(name="payout_status", summary="Check whether your last payout went through", examples=["did the payout go through", "payout status"],
         patterns=[r"\b(did|has) (the|my|that) pay ?out\b", r"\bpay ?out\b.*\b(status|go through|went through|arrived?|done|complete)\b", r"\bpay ?out status\b"],
         mode=OWNER, availability=APPROVAL, tier="T0", ops=["payouts.get"], run=run_payout_status, priority=8),
    _cap(name="refund_status", summary="Check whether your last refund went through", examples=["did the refund go through", "refund status"],
         patterns=[r"\b(did|has) (the|my|that) refund\b", r"\brefund\b.*\b(status|go through|went through|arrived?|complete)\b", r"\bwhere('?s| is) (my|the) refund\b"],
         mode=OWNER, availability=GA, tier="T0", ops=["refunds.get"], run=run_refund_status, priority=8),
    _cap(name="plans", summary="List the subscription plans you offer", examples=["what plans do i offer", "list my subscription plans"],
         patterns=[r"\bplans\b", r"\bwhat (plans|subscriptions) do i (offer|sell)\b"],
         mode=OWNER, availability=GA, tier="T0", ops=["plans.list"], run=run_plans, priority=2),
    _cap(name="products", summary="List the products in your catalog", examples=["list my products", "what's in my catalog"],
         patterns=[r"\b(products|catalog|catalogue)\b", r"\bwhat do i sell\b"],
         mode=OWNER, availability=GA, tier="T0", ops=["products.list"], run=run_products, priority=2),
    # ---------------- no API: honest handoffs
    _cap(name="rewards", summary="Rewards and cashback (in the PayPal app)", examples=["how much cashback do i have", "check my rewards"],
         patterns=[r"\b(rewards?|cash ?back|points|offers?|deals?)\b"], mode=ANY, availability=NONE, tier="T0", ops=[],
         handoff="PayPal doesn't offer an API for rewards or cashback, so I can't read them. They're under Rewards in the PayPal app.",
         handoff_url="https://www.paypal.com/rewards"),
    _cap(name="savings", summary="PayPal Savings (in the PayPal app)", examples=["what's in my savings"], patterns=[r"\bsavings?\b"],
         mode=ANY, availability=NONE, tier="T0", ops=[], priority=1,
         handoff="There's no PayPal API for Savings, so I can't see it. Check Savings in the PayPal app.", handoff_url="https://www.paypal.com/myaccount/summary"),
    _cap(name="bank_transfer", summary="Move money to your bank (in the PayPal app)", examples=["transfer 100 to my bank", "cash out"],
         patterns=[r"\b(transfer|move|withdraw|cash out)\b.*\bbank\b", r"\bwithdraw\b", r"\bcash out\b"], mode=ANY, availability=NONE, tier="T0", ops=[], priority=7,
         handoff="PayPal has no API for transfers to your bank. Use Transfer Money in the PayPal app.", handoff_url="https://www.paypal.com/myaccount/money"),
    _cap(name="card_controls", summary="Lock or unlock a PayPal card (in the PayPal app)", examples=["lock my debit card"],
         patterns=[r"\b(lock|freeze|unlock|unfreeze)\b.*\bcard\b"], mode=ANY, availability=NONE, tier="T0", ops=[], priority=7,
         handoff="Card controls aren't available through PayPal's API. Use Wallet in the PayPal app.", handoff_url="https://www.paypal.com/myaccount/money"),
    _cap(name="crypto", summary="Crypto (in the PayPal app)", examples=["what's my bitcoin worth"], patterns=[r"\b(crypto|bitcoin|ethereum|pyusd|litecoin)\b"],
         mode=ANY, availability=NONE, tier="T0", ops=[], priority=7,
         handoff="PayPal's APIs don't cover crypto. Check Crypto in the PayPal app.", handoff_url="https://www.paypal.com/myaccount/crypto"),
]

BY_NAME = {c.name: c for c in CAPABILITIES}

# Every PayPal REST endpoint the voice layer can reach: op -> (method, path template, plain-language label).
ENDPOINTS: dict[str, tuple[str, str, str]] = {
    "userinfo.get": ("GET", "/v1/identity/openidconnect/userinfo", "Signed-in user profile"),
    "v2.tokens.create": ("POST", "/v2/personal-payments/tokens", "Start a personal payment"),
    "v2.tokens.get": ("GET", "/v2/personal-payments/tokens/{id}", "Personal payment status"),
    "personal-payments.funding-options": ("POST", "/v1/payments/personal-payments/funding-options", "Fees and arrival time"),
    "personal-payments.assess-capabilities": ("POST", "/v1/payments/personal-payments/assess-capabilities", "Can I send to this country"),
    "balances.get": ("GET", "/v1/reporting/balances", "Account balance"),
    "search.get": ("GET", "/v1/reporting/transactions", "Transaction search"),
    "invoices.search-invoices": ("POST", "/v2/invoicing/search-invoices", "Search invoices"),
    "invoices.list": ("GET", "/v2/invoicing/invoices", "List invoices"),
    "invoices.get": ("GET", "/v2/invoicing/invoices/{id}", "Invoice details"),
    "invoicing.generate-next-invoice-number": ("POST", "/v2/invoicing/generate-next-invoice-number", "Next invoice number"),
    "invoices.create": ("POST", "/v2/invoicing/invoices", "Create invoice"),
    "invoices.send": ("POST", "/v2/invoicing/invoices/{id}/send", "Send invoice"),
    "invoices.remind": ("POST", "/v2/invoicing/invoices/{id}/remind", "Send invoice reminder"),
    "invoices.cancel": ("POST", "/v2/invoicing/invoices/{id}/cancel", "Cancel invoice"),
    "invoices.payments": ("POST", "/v2/invoicing/invoices/{id}/payments", "Record invoice payment"),
    "payment-resources.post": ("POST", "/v1/checkout/payment-resources", "Create payment link"),
    "captures.refund": ("POST", "/v2/payments/captures/{id}/refund", "Refund a payment"),
    "refunds.get": ("GET", "/v2/payments/refunds/{id}", "Refund status"),
    "disputes.list": ("GET", "/v1/customer/disputes", "List disputes"),
    "disputes.get": ("GET", "/v1/customer/disputes/{id}", "Dispute details"),
    "disputes.accept-claim": ("POST", "/v1/customer/disputes/{id}/accept-claim", "Accept dispute claim"),
    "subscriptions.list": ("GET", "/v1/billing/subscriptions", "List subscriptions"),
    "subscriptions.get": ("GET", "/v1/billing/subscriptions/{id}", "Subscription details"),
    "subscriptions.suspend": ("POST", "/v1/billing/subscriptions/{id}/suspend", "Pause subscription"),
    "subscriptions.activate": ("POST", "/v1/billing/subscriptions/{id}/activate", "Resume subscription"),
    "subscriptions.cancel": ("POST", "/v1/billing/subscriptions/{id}/cancel", "Cancel subscription"),
    "plans.list": ("GET", "/v1/billing/plans", "List subscription plans"),
    "products.list": ("GET", "/v1/catalogs/products", "List catalog products"),
    "payouts.post": ("POST", "/v1/payments/payouts", "Create payout"),
    "payouts.get": ("GET", "/v1/payments/payouts/{id}", "Payout status"),
    "quote_exchange_rates": ("POST", "/v2/pricing/quote-exchange-rates", "Currency conversion quote"),
}
