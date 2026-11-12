"""In-process PayPal sandbox simulator (httpx MockTransport).

Shapes follow PayPal's official specs and documented samples. Every request body is checked
against the official contract (contracts.validate_request); a violation returns a PayPal-style
400 INVALID_REQUEST, so tests fail the same way the real sandbox would.

Behaviours marked MOCK-ONLY are reasonable stand-ins where PayPal does not document the exact
response (for example the error for a >31 day transaction search).
"""
from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from .contracts import validate_request

NOW = datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S+0000")


def _money(v, c="USD"):
    return {"currency_code": c, "value": f"{Decimal(str(v)):.2f}"}


def _txn(tid, days_ago, amount, code, name, email, subject, fee=None, status="S"):
    d = NOW - timedelta(days=days_ago, hours=3)
    return {
        "transaction_info": {
            "transaction_id": tid, "transaction_event_code": code,
            "transaction_initiation_date": _iso(d), "transaction_updated_date": _iso(d),
            "transaction_amount": _money(amount), "transaction_status": status,
            **({"fee_amount": _money(fee)} if fee is not None else {}),
            "transaction_subject": subject,
        },
        "payer_info": {"email_address": email, "payer_name": {"alternate_full_name": name}},
    }


class MockPayPal:
    def __init__(self, now: datetime = NOW):
        self.now = now
        self.requests: list[dict] = []
        self.idem: dict[str, tuple[int, dict]] = {}
        self.user = {"user_id": "https://www.paypal.com/webapps/auth/identity/user/MOCKUSER", "sub": "MOCKUSER",
                     "name": "Taylor Brooks", "given_name": "Taylor", "family_name": "Brooks",
                     "email": "taylor@example.com", "email_verified": True, "account_type": "PERSONAL",
                     "verified_account": True}
        self.balances = [{"currency": "USD", "primary": True, "total_balance": _money("1342.18"),
                          "available_balance": _money("1250.18"), "withheld_balance": _money("92.00")}]
        self.transactions = [
            _txn("1AB23456CD789012E", 0, "40.00", "T0006", "Sam Rivera", "sam@example.com", "Custom frame", fee="-1.70"),
            _txn("2BC34567DE890123F", 1, "-25.00", "T0000", "Maya Chen", "maya@example.com", "Dinner split"),
            _txn("3CD45678EF901234G", 2, "120.00", "T0006", "Lake Erie Gallery", "orders@lakeerie.example", "Large print 24x36", fee="-4.38"),
            _txn("4DE56789FG012345H", 4, "-12.99", "T0000", "Printify", "billing@printify.example", "Sample print"),
            _txn("5EF67890GH123456I", 6, "-40.00", "T1107", "Sam Rivera", "sam@example.com", "Refund: Custom frame"),
            _txn("6FG78901HI234567J", 9, "65.00", "T0006", "Jordan Lee", "jordan@example.com", "Memorabilia shadow box", fee="-2.57"),
            _txn("7GH89012IJ345678K", 15, "-300.00", "T0400", "Bank transfer", "", "Withdrawal to bank"),
            _txn("8HI90123JK456789L", 21, "210.00", "T0006", "Ohio Craft Fair", "pay@ohiocraft.example", "Booth sales", fee="-7.59"),
            _txn("9IJ01234KL567890M", 38, "55.00", "T0006", "Priya Nair", "priya@example.com", "Small print set", fee="-2.21"),
        ]
        self.captures = {"1AB23456CD789012E": {"id": "1AB23456CD789012E", "status": "COMPLETED", "amount": _money("40.00")}}
        self.invoices: dict[str, dict] = {}
        for i, (num, status, who, email, amt, days) in enumerate([
            ("0041", "SENT", "Jordan Lee", "jordan@example.com", "85.00", 3),
            ("0040", "PAID", "Lake Erie Gallery", "orders@lakeerie.example", "120.00", 6),
            ("0039", "SENT", "Priya Nair", "priya@example.com", "55.00", 20),
        ]):
            iid = f"INV2-MOCK-000{i}-AAAA-BBBB"
            self.invoices[iid] = self._invoice(iid, num, status, who, email, amt, self.now - timedelta(days=days))
        self.disputes = [{"dispute_id": "PP-D-27803", "create_time": _iso(self.now - timedelta(days=2)),
                          "update_time": _iso(self.now - timedelta(days=1)), "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                          "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_state": "REQUIRED_ACTION",
                          "dispute_amount": _money("65.00"), "dispute_life_cycle_stage": "INQUIRY",
                          "disputed_transactions": [{"seller_transaction_id": "6FG78901HI234567J", "gross_amount": _money("65.00"),
                                                     "buyer": {"name": "Jordan Lee"}, "items": [{"item_name": "Memorabilia shadow box"}]}],
                          "seller_response_due_date": _iso(self.now + timedelta(days=8))}]
        self.subscriptions = [{"id": "I-MOCK1234567A", "status": "ACTIVE", "plan_id": "P-MONTHLYPRINT",
                               "subscriber": {"name": {"given_name": "Maya", "surname": "Chen"}, "email_address": "maya@example.com"},
                               "billing_info": {"last_payment": {"amount": _money("19.00")}, "next_billing_time": _iso(self.now + timedelta(days=12))}}]
        self.plans = [{"id": "P-MONTHLYPRINT", "product_id": "PROD-PRINTCLUB", "name": "Monthly Print Club", "status": "ACTIVE",
                       "description": "One new small print every month", "create_time": _iso(self.now - timedelta(days=120))},
                      {"id": "P-FRAMECARE", "product_id": "PROD-FRAMECARE", "name": "Frame Care Plan", "status": "INACTIVE",
                       "description": "Yearly frame cleaning and repair", "create_time": _iso(self.now - timedelta(days=60))}]
        self.products = [{"id": "PROD-PRINTCLUB", "name": "Print Club membership", "description": "Monthly art print subscription"},
                         {"id": "PROD-SMALLPRINT", "name": "Small format print", "description": "8x10 archival print"},
                         {"id": "PROD-LARGEPRINT", "name": "Large format print", "description": "24x36 archival print"},
                         {"id": "PROD-SHADOWBOX", "name": "Memorabilia shadow box", "description": "Custom framed memorabilia"}]
        self.refunds: dict[str, dict] = {}
        self.payouts: dict[str, dict] = {}
        self.pp_tokens: dict[str, dict] = {}
        self.pp_payments: dict[str, dict] = {}
        self.funding_options: dict[str, dict] = {}
        self.known_paypal_emails = {"sam@example.com", "maya@example.com", "jordan@example.com", "priya@example.com"}

    def _invoice(self, iid, num, status, who, email, amt, when):
        given, _, sur = who.partition(" ")
        return {"id": iid, "status": status,
                "detail": {"invoice_number": num, "currency_code": "USD", "invoice_date": when.strftime("%Y-%m-%d"),
                           "payment_term": {"due_date": (when + timedelta(days=14)).strftime("%Y-%m-%d")}},
                "primary_recipients": [{"billing_info": {"email_address": email, "name": {"given_name": given, "surname": sur}}}],
                "amount": _money(amt), "due_amount": _money("0.00" if status == "PAID" else amt),
                "links": [{"href": f"https://www.sandbox.paypal.com/invoice/p/#{iid}", "rel": "payer-view", "method": "GET"}]}

    # ------------------------------------------------------------------ transport
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        body = None
        if req.content and req.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(req.content)
        self.requests.append({"method": method, "path": path, "params": dict(req.url.params), "body": body,
                              "request_id": req.headers.get("paypal-request-id")})
        if path == "/v1/oauth2/token":
            return self._token(req)
        if not req.headers.get("authorization", "").startswith("Bearer "):
            return self._err(401, "AUTHENTICATION_FAILURE", "Authentication failed due to invalid authentication credentials")
        rid = req.headers.get("paypal-request-id")
        if method == "POST" and rid and rid in self.idem:
            status, payload = self.idem[rid]
            return httpx.Response(status, json=payload, headers={"paypal-debug-id": "mockdbg-replay"})
        problems = validate_request(method, path, body) if method in ("POST", "PUT", "PATCH") else []
        if problems:
            self.requests[-1]["status"] = 400
            self.requests[-1]["contract_problems"] = problems
            return self._err(400, "INVALID_REQUEST", "Request is not well-formed, syntactically incorrect, or violates schema.",
                             [{"issue": "CONTRACT_VIOLATION", "description": p} for p in problems])
        status, payload = self._route(method, path, req, body)
        self.requests[-1]["status"] = status
        if method == "POST" and rid and status < 500:
            self.idem[rid] = (status, payload)
        return httpx.Response(status, json=payload, headers={"paypal-debug-id": "mockdbg-" + uuid.uuid4().hex[:10]})

    def _err(self, status, name, message, details=None):
        return httpx.Response(status, json={"name": name, "message": message, "debug_id": "mockdbg-err",
                                            **({"details": details} if details else {})})

    def _token(self, req):
        form = dict(x.split("=", 1) for x in req.content.decode().split("&") if "=" in x)
        if not req.headers.get("authorization", "").startswith("Basic "):
            return self._err(401, "invalid_client", "Client Authentication failed")
        if form.get("grant_type") == "authorization_code" and form.get("code") != "good-code":
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid authorization code"})
        return httpx.Response(200, json={"access_token": "A21AAmock" + uuid.uuid4().hex[:8], "token_type": "Bearer",
                                         "expires_in": 32400, "scope": "openid https://uri.paypal.com/services/wallet/sendmoney",
                                         **({"refresh_token": "R23mock"} if form.get("grant_type") != "client_credentials" else {})})

    # ------------------------------------------------------------------ routes
    def _route(self, method, path, req, body):  # noqa: C901 - a router is a long switch by nature
        q = dict(req.url.params)
        if method == "GET" and path == "/v1/identity/openidconnect/userinfo":
            return 200, self.user
        if method == "GET" and path == "/v1/reporting/balances":
            return 200, {"balances": deepcopy(self.balances), "account_id": "MOCKMERCHANT1", "as_of_time": _iso(self.now),
                         "last_refresh_time": _iso(self.now - timedelta(hours=1))}
        if method == "GET" and path == "/v1/reporting/transactions":
            try:
                s = datetime.fromisoformat(q["start_date"].replace("Z", "+00:00"))
                e = datetime.fromisoformat(q["end_date"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                return 400, {"name": "INVALID_REQUEST", "message": "start_date and end_date are required (RFC 3339 with seconds)."}
            if e - s > timedelta(days=31, seconds=1):     # documented limit; error wording MOCK-ONLY
                return 400, {"name": "INVALID_REQUEST", "message": "Date range is greater than 31 days",
                             "details": [{"issue": "INVALID_DATE_RANGE"}]}
            items = []
            for t in self.transactions:
                d = datetime.strptime(t["transaction_info"]["transaction_initiation_date"], "%Y-%m-%dT%H:%M:%S+0000").replace(tzinfo=timezone.utc)
                if s <= d <= e and (not q.get("transaction_id") or q["transaction_id"] == t["transaction_info"]["transaction_id"]):
                    items.append(deepcopy(t))
            return 200, {"transaction_details": items, "account_number": "MOCKMERCHANT1", "start_date": q["start_date"],
                         "end_date": q["end_date"], "last_refreshed_datetime": _iso(self.now - timedelta(hours=1)),
                         "page": 1, "total_items": len(items), "total_pages": 1}
        if method == "GET" and path == "/v2/invoicing/invoices":
            items = sorted(self.invoices.values(), key=lambda i: i["detail"]["invoice_date"], reverse=True)
            return 200, {"items": deepcopy(items), "total_items": len(items), "total_pages": 1}
        if method == "POST" and path == "/v2/invoicing/search-invoices":
            items = [i for i in self.invoices.values()
                     if not body.get("status") or i["status"] in body["status"]]
            if body.get("recipient_email"):
                items = [i for i in items if i["primary_recipients"][0]["billing_info"]["email_address"] == body["recipient_email"]]
            return 200, {"items": deepcopy(items), "total_items": len(items), "total_pages": 1}
        if method == "POST" and path == "/v2/invoicing/generate-next-invoice-number":
            return 200, {"invoice_number": f"{len(self.invoices) + 40:04d}"}
        if method == "POST" and path == "/v2/invoicing/invoices":
            iid = f"INV2-MOCK-{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}-CCCC"
            rec = body.get("primary_recipients", [{}])[0].get("billing_info", {})
            total = sum(Decimal(it["unit_amount"]["value"]) * Decimal(it.get("quantity", "1")) for it in body.get("items", []))
            inv = self._invoice(iid, body["detail"].get("invoice_number", "0050"), "DRAFT",
                                " ".join(filter(None, [rec.get("name", {}).get("given_name"), rec.get("name", {}).get("surname")])) or rec.get("email_address", ""),
                                rec.get("email_address", ""), total, self.now)
            inv["items"] = body.get("items", [])
            self.invoices[iid] = inv
            return 201, deepcopy(inv)
        m = re.match(r"^/v2/invoicing/invoices/([^/]+)/payments$", path)
        if m and method == "POST":
            inv = self.invoices.get(m.group(1))
            if not inv:
                return 404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}
            if inv["status"] in ("PAID", "MARKED_AS_PAID", "CANCELLED", "DRAFT"):
                return 422, {"name": "UNPROCESSABLE_ENTITY", "message": "Payment cannot be recorded for this invoice status.",
                             "details": [{"issue": "INVALID_INVOICE_STATUS"}]}
            inv["status"], inv["due_amount"] = "MARKED_AS_PAID", _money("0.00")
            return 200, {"payment_id": "EXTR-" + uuid.uuid4().hex[:17].upper()}
        m = re.match(r"^/v2/invoicing/invoices/([^/]+)(?:/(send|remind|cancel))?$", path)
        if m:
            inv = self.invoices.get(m.group(1))
            if not inv:
                return 404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}
            action = m.group(2)
            if method == "GET" and not action:
                return 200, deepcopy(inv)
            if action == "send":
                if inv["status"] != "DRAFT":
                    return 422, {"name": "UNPROCESSABLE_ENTITY", "message": "Invoice is not in draft state",
                                 "details": [{"issue": "INVALID_INVOICE_STATUS"}]}
                inv["status"] = "SENT"
                return 200, {"href": inv["links"][0]["href"], "rel": "payer-view", "method": "GET"}
            if action == "remind":
                return 204, {}
            if action == "cancel":
                if inv["status"] == "PAID":
                    return 422, {"name": "UNPROCESSABLE_ENTITY", "message": "Cannot cancel a paid invoice"}
                inv["status"] = "CANCELLED"
                return 204, {}
        if method == "POST" and path == "/v1/checkout/payment-resources":
            rid = "PLB-" + uuid.uuid4().hex[:12].upper()
            return 201, {"id": rid, "payment_link": f"https://www.sandbox.paypal.com/ncp/payment/{rid}", "status": "ACTIVE", **body}
        m = re.match(r"^/v2/payments/captures/([^/]+)/refund$", path)
        if m and method == "POST":
            cap = self.captures.get(m.group(1))
            if not cap:
                return 404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}
            amt = (body or {}).get("amount") or cap["amount"]
            if Decimal(amt["value"]) > Decimal(cap["amount"]["value"]):
                return 422, {"name": "UNPROCESSABLE_ENTITY", "message": "The requested action could not be performed.",
                             "details": [{"issue": "REFUND_AMOUNT_EXCEEDED"}]}
            ref = {"id": "R-" + uuid.uuid4().hex[:15].upper(), "status": "COMPLETED", "amount": amt,
                   "create_time": _iso(self.now), "update_time": _iso(self.now)}
            self.refunds[ref["id"]] = ref
            return 201, deepcopy(ref)
        m = re.match(r"^/v2/payments/refunds/([^/]+)$", path)
        if m and method == "GET":
            ref = self.refunds.get(m.group(1))
            return (200, deepcopy(ref)) if ref else (404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."})
        m = re.match(r"^/v1/customer/disputes/([^/]+)$", path)
        if m and method == "GET":
            d = next((x for x in self.disputes if x["dispute_id"] == m.group(1)), None)
            return (200, deepcopy(d)) if d else (404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."})
        if method == "GET" and path == "/v1/billing/plans":
            return 200, {"plans": deepcopy(self.plans), "total_items": len(self.plans), "total_pages": 1}
        if method == "GET" and path == "/v1/catalogs/products":
            return 200, {"products": deepcopy(self.products), "total_items": len(self.products), "total_pages": 1}
        m = re.match(r"^/v1/billing/subscriptions/([^/]+)(?:/(suspend|activate))?$", path)
        if m and (method == "GET" and not m.group(2) or method == "POST" and m.group(2)):
            sub = next((x for x in self.subscriptions if x["id"] == m.group(1)), None)
            if not sub:
                return 404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}
            if method == "GET":
                return 200, deepcopy(sub)
            want, need = ("SUSPENDED", "ACTIVE") if m.group(2) == "suspend" else ("ACTIVE", "SUSPENDED")
            if sub["status"] != need:
                return 422, {"name": "UNPROCESSABLE_ENTITY", "message": "The requested action could not be performed.",
                             "details": [{"issue": "SUBSCRIPTION_STATUS_INVALID"}]}
            sub["status"] = want
            return 204, {}
        m = re.match(r"^/v1/payments/payouts/([^/]+)$", path)
        if m and method == "GET":
            po = self.payouts.get(m.group(1))
            if not po:
                return 404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}
            po["batch_header"]["batch_status"] = "SUCCESS"        # payouts settle shortly after creation
            return 200, deepcopy(po)
        if method == "GET" and path == "/v1/customer/disputes":
            return 200, {"items": deepcopy(self.disputes)}
        m = re.match(r"^/v1/customer/disputes/([^/]+)/accept-claim$", path)
        if m and method == "POST":
            d = next((x for x in self.disputes if x["dispute_id"] == m.group(1)), None)
            if not d:
                return 404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}
            d["status"], d["dispute_state"] = "RESOLVED", "RESOLVED"
            return 200, {"links": [{"href": f"/v1/customer/disputes/{d['dispute_id']}", "rel": "self", "method": "GET"}]}
        if method == "GET" and path == "/v1/billing/subscriptions":
            return 200, {"subscriptions": deepcopy(self.subscriptions)}
        m = re.match(r"^/v1/billing/subscriptions/([^/]+)/cancel$", path)
        if m and method == "POST":
            sub = next((s for s in self.subscriptions if s["id"] == m.group(1)), None)
            if not sub:
                return 404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}
            sub["status"] = "CANCELLED"
            return 204, {}
        if method == "POST" and path == "/v1/payments/payouts":
            po = {"batch_header": {"payout_batch_id": "PB" + uuid.uuid4().hex[:12].upper(), "batch_status": "PENDING",
                                   "sender_batch_header": body["sender_batch_header"]}}
            self.payouts[po["batch_header"]["payout_batch_id"]] = po
            return 201, deepcopy(po)
        if method == "POST" and path == "/v2/pricing/quote-exchange-rates":   # response shape MOCK-ONLY
            rates = {("USD", "EUR"): "0.9180", ("USD", "GBP"): "0.7810", ("USD", "MXN"): "18.4100", ("USD", "CAD"): "1.3750"}
            out = []
            for it in body["quote_items"]:
                r = Decimal(rates.get((it["base_currency"], it["quote_currency"]), "1.0000"))
                out.append({"base_amount": {"currency_code": it["base_currency"], "value": it.get("base_amount", "1.00")},
                            "quote_amount": {"currency_code": it["quote_currency"],
                                             "value": f"{Decimal(it.get('base_amount', '1')) * r:.2f}"},
                            "exchange_rate": str(r)})
            return 200, {"exchange_rate_quotes": out}
        # ------------------------------ Personal Payments (limited release)
        if method == "POST" and path == "/v1/payments/personal-payments/assess-capabilities":
            payee_cc = body.get("payee_country_codes") or []
            blocked = {"CU", "IR", "KP", "SY"}
            return 200, {"payer_country_code": body["payer_country_code"], "supported_entities": ["PAYPAL"],
                         "payment_types": ["PERSONAL", "PURCHASE"], "currency_codes": ["USD", "EUR", "GBP", "CAD", "MXN"],
                         "payee_countries": [{"country_code": c, "supported_entities": [] if c in blocked else ["PAYPAL"],
                                              "payment_types": [] if c in blocked else ["PERSONAL", "PURCHASE"],
                                              "currency_codes": [] if c in blocked else ["USD"]} for c in payee_cc]}
        if method == "POST" and path == "/v1/payments/personal-payments/funding-options":
            payee = body["payee"]
            if payee["type"] == "EMAIL" and payee["id"].lower() == self.user["email"]:
                return 422, {"name": "UNPROCESSABLE_ENTITY", "message": "Sender and receiver cannot be the same.",
                             "details": [{"issue": "PAYEE_SAME_AS_PAYER"}]}          # MOCK-ONLY issue name
            foreign = payee.get("country_code") not in (None, "US")
            if payee["type"] == "EMAIL" and payee["id"] not in self.known_paypal_emails and not payee.get("name") and Decimal(body["amount"]["value"]) > 15:
                return 422, {"name": "TRANSACTION_DECLINED_ADDITIONAL_PAYEE_INFO_REQUIRED",
                             "message": "Additional payee information (name and country code) is required."}
            value = Decimal(body["amount"]["value"])
            card_fee = (value * Decimal("0.029") + Decimal("0.30")).quantize(Decimal("0.01"))
            opts = []
            for oid, src, fee in [("FO-BAL-" + uuid.uuid4().hex[:8], {"instrument_type": "HOLDING", "holding": {"amount": _money_v1(value)}}, Decimal(0)),
                                  ("FO-CARD-" + uuid.uuid4().hex[:8], {"instrument_type": "PAYMENT_CARD", "payment_card": {"id": "CC-MOCK", "network": "VISA", "last_4": "4417", "amount": _money_v1(value + card_fee)}}, card_fee)]:
                opt = {"id": oid, "funding_mode": "INSTANT", "fee": {"payer": (body.get("fee") or {}).get("payer", "PAYER"), "amount": _money_v1(fee)},
                       "sources": [src], "estimated_funds_arrival": _iso(self.now)}
                self.funding_options[oid] = {"option": opt, "request": body}
                opts.append(opt)
            fo = {"payee": {**payee, "is_paypal_account": payee["id"] in self.known_paypal_emails},
                  "options": opts, "payment_type": body["payment_type"]}
            if foreign and value > 15:
                fo["regulatory_information"] = {"remittance_transfer_rule": {"action": "DISPLAY_TRANSACTION_DISCLOSURES"}}
            return 200, {"funding_options": fo}
        if method == "POST" and path == "/v1/payments/personal-payments":
            ref = self.funding_options.pop(body["funding_option_id"], None)
            if not ref:
                return 422, {"name": "UNPROCESSABLE_ENTITY", "message": "Funding option is invalid or expired.",
                             "details": [{"issue": "INVALID_FUNDING_OPTION"}]}       # MOCK-ONLY issue name
            pid = uuid.uuid4().hex[:17].upper()
            r = ref["request"]
            paid = Decimal(r["amount"]["value"]) + Decimal(ref["option"]["fee"]["amount"]["value"])
            pay = {"payment_id": pid, "create_time": _iso(self.now), "payee": r["payee"], "payment_amount": r["amount"],
                   "total_fee": ref["option"]["fee"]["amount"], "total_amount_paid": _money_v1(paid),
                   "total_amount_received": r["amount"], "payment_type": r["payment_type"],
                   "note_to_payee": body.get("note_to_payee", ""), "estimated_funds_arrival": _iso(self.now)}
            self.pp_payments[pid] = pay
            return 201, {"personal_payment": pay}
        m = re.match(r"^/v1/payments/personal-payments/([A-Z0-9]+)$", path)
        if m and method == "GET":
            p = self.pp_payments.get(m.group(1))
            return (200, p) if p else (404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."})
        if method == "POST" and path == "/v2/personal-payments/tokens":
            tid = "PP-" + uuid.uuid4().hex[:18].upper()
            tok = {"id": tid, "status": "CREATED",
                   "sender": {"account_id": "MOCKSENDER", "name": {"person_name": {"full_name": self.user["name"]}}, "country_code": "US", "account_type": "PERSONAL"},
                   "receiver": {**body["receiver"], "name": {"person_name": {"full_name": "Sam Rivera" if body["receiver"]["id"] == "sam@example.com" else ""}}},
                   "amount": body["amount"], "payment_type": body["payment_type"], "intent": "CAPTURE",
                   "external_reference_id": body.get("external_reference_id", ""), "note_to_receiver": body.get("note_to_receiver", ""),
                   "links": [{"href": f"https://api-m.sandbox.paypal.com/v2/personal-payments/tokens/{tid}", "rel": "self", "method": "GET"},
                             {"href": f"https://api-m.sandbox.paypal.com/myaccount/transfer/send/external/pisp?token_id={tid}", "rel": "initiate_payment", "method": "GET"}]}
            self.pp_tokens[tid] = tok
            return 201, deepcopy(tok)
        m = re.match(r"^/v2/personal-payments/tokens/(PP-[A-Z0-9]+)$", path)
        if m and method == "GET":
            t = self.pp_tokens.get(m.group(1))
            if not t:
                return 404, {"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}
            return 200, deepcopy(t)
        return 404, {"name": "RESOURCE_NOT_FOUND", "message": f"MOCK: no route for {method} {path}"}

    # test helper: simulate the user approving a PayPal-UI personal payment
    def approve_token(self, token_id: str) -> str:
        t = self.pp_tokens[token_id]
        t["status"] = "COMPLETED"
        t["id_payment"] = uuid.uuid4().hex[:17].upper()
        t["create_time"] = _iso(self.now)
        t["amounts_breakdown"] = {"sender": {"amount_sent": t["amount"], "total_amount_sent": t["amount"]},
                                  "receiver": {"amount_sent": t["amount"], "amount_received": t["amount"]}}
        return t["id_payment"]


def _money_v1(v, c="USD"):
    return {"currency": c, "value": f"{Decimal(str(v)):.2f}"}
