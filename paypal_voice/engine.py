"""The voice backbone: one call per spoken turn.

    text -> normalize -> (dialog frame | classify) -> fill slots -> gate (mode, availability, limits)
         -> confirm (by risk tier) -> execute PayPal operations -> speak result (API-to-Voice)

Everything that moves money or can't be undone (tier T2) needs a read-back and an explicit yes in a
later turn, and optionally a second factor from the UI. Nothing is executed on a wake word, a stale
frame, or the app's own speech.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import nlu
from .normalize import DateRange, Money, Parsed, normalize
from .paypal_client import TRACE, PayPalClient, PayPalError, Token
from .registry import (ANY, APPROVAL, BY_NAME, CAPABILITIES, GA, LIMITED, NONE, OWNER, PERSONAL, Capability, Ctx,
                       Payee, Slot, search_transactions)
from .speak import Reply, error_reply, join_and, money, when

_STOP = {w[:5] for w in "a an the my me i to of for in on is it do does did what whats how much many can you please show tell give say and or with from about any some this that have has".split()}
CONFIRM_TTL = 90          # seconds a pending confirmation stays valid
FRAME_TTL = 120           # seconds a pending slot question stays valid


@dataclass
class Config:
    env: str = "mock"                       # mock | sandbox | live
    account_mode: str = "BOTH"              # OWNER | PERSONAL | BOTH
    public_base_url: str = "http://localhost:8000"
    home_country: str = "US"
    personal_payments_enabled: bool = True  # PayPal approved this app for Personal Payments (limited release)
    login_enabled: bool = True              # PayPal approved Log in with PayPal for this app
    payouts_enabled: bool = False           # Payouts enabled on the business account
    second_factor: bool = False             # T2 also needs a tap/passkey from the UI
    max_per_action: Decimal = Decimal("500")
    daily_limit: Decimal = Decimal("1000")
    audit_path: str = "audit.jsonl"
    contacts_path: str = "contacts.json"


# ------------------------------------------------------------------ session, contacts, audit

@dataclass
class Pending:
    capability: str
    slots: dict
    awaiting: str                     # slot:<name> | confirm | second_factor | choice:<slot>
    asked_at: float
    asked_turn: int
    choices: list[dict] = field(default_factory=list)
    attempts: int = 0
    factor_token: str = ""


@dataclass
class Session:
    id: str
    turn: int = 0
    pending: Pending | None = None
    user_token: Token | None = None
    profile: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    last_items: list[dict] = field(default_factory=list)
    more: list[str] = field(default_factory=list)
    last_reply: dict = field(default_factory=dict)
    spent_today: Decimal = Decimal("0")
    spent_day: str = ""
    pending_token: dict | None = None
    oauth_state: str = ""


class Contacts:
    def __init__(self, path: str | None = None, seed: dict | None = None):
        self.path = Path(path) if path else None
        self.data: dict[str, dict] = {}
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text())
        if seed:
            for k, v in seed.items():
                self.data.setdefault(k, v)

    def find(self, spoken: str) -> dict | None:
        key = spoken.lower().strip()
        if key in self.data:
            return self.data[key]
        for k, v in self.data.items():
            if key == v.get("name", "").lower() or key == k.split()[0] or key in v.get("aliases", []):
                return v
        return None

    def by_id(self, ident: str) -> dict | None:
        return next((v for v in self.data.values() if v["id"].lower() == ident.lower()), None)

    def add(self, spoken: str, payee: Payee) -> None:
        self.data[spoken.lower()] = {"id": payee.id, "type": payee.type, "name": payee.name or spoken.title()}
        if self.path:
            self.path.write_text(json.dumps(self.data, indent=2))


class Audit:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.prev = "0" * 64
        self.memory: list[dict] = []
        if self.path and self.path.exists():
            lines = self.path.read_text().strip().splitlines()
            if lines:
                self.prev = json.loads(lines[-1])["hash"]

    def write(self, **entry) -> dict:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry, "prev": self.prev}
        entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()
        self.prev = entry["hash"]
        self.memory.append(entry)
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        return entry

    @staticmethod
    def verify(lines: list[dict]) -> bool:
        prev = "0" * 64
        for e in lines:
            body = {k: v for k, v in e.items() if k != "hash"}
            if e["prev"] != prev or hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest() != e["hash"]:
                return False
            prev = e["hash"]
        return True


@dataclass
class TurnResult:
    speech: str
    display: dict = field(default_factory=dict)
    actions: list[dict] = field(default_factory=list)
    expecting: str = ""                 # what the next utterance should answer ("", "confirm", "slot:amount", ...)
    capability: str = ""
    state: str = "idle"                 # idle | asking | confirming | done | error | handoff
    debug: dict = field(default_factory=dict)
    api: list[dict] = field(default_factory=list)          # PayPal REST calls made for this turn (op, method, path, status, ms)
    suggestions: list[str] = field(default_factory=list)   # things the user could say next
    heard: str = ""                                        # the normalized text the engine acted on

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ engine

class VoiceEngine:
    def __init__(self, client: PayPalClient, config: Config | None = None, contacts: Contacts | None = None,
                 now: callable = None):
        self.client = client
        self.config = config or Config()
        self.contacts = contacts or Contacts()
        self.audit = Audit(self.config.audit_path)
        self.sessions: dict[str, Session] = {}
        self._now = now or (lambda: datetime.now(timezone.utc))

    def session(self, sid: str) -> Session:
        if sid not in self.sessions:
            self.sessions[sid] = Session(sid)
        return self.sessions[sid]

    # ---------------------------------------------------------------- entry point
    async def handle_turn(self, sid: str, text: str, alternatives: list[str] | None = None) -> TurnResult:
        s = self.session(sid)
        s.turn += 1
        now = self._now()
        p = normalize(text, now)
        if alternatives and not nlu.classify(p).capability and not s.pending:
            for alt in alternatives:                       # n-best rescoring: first alternative that means something
                ap = normalize(alt, now)
                if nlu.classify(ap).capability:
                    p = ap
                    break
        token = TRACE.set([])
        try:
            result = await self._turn(s, p, now)
            result.api = list(TRACE.get() or [])
        finally:
            TRACE.reset(token)
        result.heard = p.text
        if not result.suggestions:
            result.suggestions = list(result.debug.get("suggestions") or [])
        s.last_reply = result.to_dict()
        return result

    async def _traced(self, coro) -> TurnResult:
        token = TRACE.set([])
        try:
            result = await coro
            result.api = list(TRACE.get() or [])
            return result
        finally:
            TRACE.reset(token)

    def _closest(self, text: str) -> list[str]:
        """Example phrases that share the most words with something we couldn't classify."""
        words = {w[:5] for w in re.findall(r"[a-z]+", text)} - _STOP
        scored = []
        for c in CAPABILITIES:
            vocab = {w[:5] for w in re.findall(r"[a-z]+", " ".join(c.examples) + " " + c.summary.lower() + " " + c.name.replace("_", " "))} - _STOP
            score = len(words & vocab)
            if score and not self._gate_quiet(c):
                scored.append((score, c.examples[0]))
        scored.sort(key=lambda x: -x[0])
        return [ex for _, ex in scored[:3]]

    def _gate_quiet(self, cap: Capability) -> bool:
        c = self.config
        return (cap.mode == OWNER and c.account_mode == "PERSONAL") or (cap.mode == PERSONAL and c.account_mode == "OWNER")

    async def _turn(self, s: Session, p: Parsed, now: datetime) -> TurnResult:
        t = p.text
        if not t:
            return TurnResult("I didn't catch that.", state="idle")
        if nlu.CANCEL.match(t):
            had = s.pending is not None
            s.pending = None
            return TurnResult("Cancelled. Nothing was sent." if had else "Okay.", state="idle")
        if nlu.REPEAT.match(t) and s.last_reply:
            return TurnResult(**{**s.last_reply, "debug": {"repeat": True}})
        if nlu.MORE.match(t) and not s.pending:
            if s.more:
                chunk = s.more.pop(0)
                return TurnResult(chunk + (" Say more for the rest." if s.more else ""), state="done")
            return TurnResult("That's everything.", state="done")
        if nlu.HELP.search(t) and not s.pending:
            return self._help(s)
        if nlu.SIGN_IN.search(t) and not s.pending:
            return self._sign_in(s)

        if s.pending:
            r = await self._continue(s, p, now)
            if r is not None:
                return r
        u = nlu.classify(p)
        if not u.capability:
            near = self._closest(t)
            if near:
                return TurnResult("I'm not sure what you meant. You could say: " + ", or ".join(f'"{x}"' for x in near[:2]) + ".",
                                  {"title": "Did you mean", "lines": near}, state="idle", debug={"text": t}, suggestions=near)
            return TurnResult("I'm not sure how to help with that. Say help to hear what I can do.", state="idle",
                              debug={"text": t}, suggestions=["help"])
        return await self._start(s, u.capability, p, now)

    # ---------------------------------------------------------------- new request
    async def _start(self, s: Session, cap: Capability, p: Parsed, now: datetime) -> TurnResult:
        s.pending = None
        gate = self._gate(s, cap)
        if gate:
            return gate
        slots = self._extract(cap, p, s)
        if cap.name == "activity":
            slots["direction"] = nlu.extract_direction(p)
        return await self._advance(s, cap, slots, now)

    def _gate(self, s: Session, cap: Capability) -> TurnResult | None:
        c = self.config
        if cap.availability == NONE:
            return TurnResult(cap.handoff, {"title": cap.summary, "link": cap.handoff_url},
                              [{"type": "open_url", "url": cap.handoff_url, "purpose": "handoff"}] if cap.handoff_url else [],
                              capability=cap.name, state="handoff")
        if cap.mode == OWNER and c.account_mode == "PERSONAL":
            return TurnResult("PayPal only shares balances and activity with the account owner's own app. Reading a personal "
                              "account's history needs the PayPal partner program, so I can't do that here.",
                              {"title": "Not available for personal accounts"}, capability=cap.name, state="handoff")
        if cap.mode == PERSONAL:
            if c.account_mode == "OWNER":
                return TurnResult("This app is set up for a business account, and sending personal payments needs PayPal's "
                                  "Personal Payments approval.", capability=cap.name, state="handoff")
            if cap.availability == LIMITED and cap.name != "whoami" and not c.personal_payments_enabled:
                return TurnResult("Sending personal payments needs PayPal to approve this app for its Personal Payments API. "
                                  "Until then, I can't send money.", {"title": "Needs PayPal approval",
                                  "lines": ["Request access: p2papiapproval@paypal.com"]}, capability=cap.name, state="handoff")
            if not s.user_token:
                r = self._sign_in(s)
                r.speech = "First, sign in with PayPal. " + r.speech
                r.capability = cap.name
                return r
        if cap.availability == APPROVAL and not c.payouts_enabled:
            return TurnResult(f"{cap.summary} needs PayPal to enable Payouts on your business account first.",
                              {"title": "Needs PayPal approval", "lines": [cap.error_hint]}, capability=cap.name, state="handoff")
        return None

    def _extract(self, cap: Capability, p: Parsed, s: Session) -> dict:
        slots: dict[str, Any] = {}
        for sl in cap.slots:
            v = self._extract_one(sl, cap, p, s, fresh=False)
            if v is not None:
                slots[sl.name] = v
        return slots

    def _extract_one(self, sl: Slot, cap: Capability, p: Parsed, s: Session, fresh: bool):
        k = sl.kind
        if k == "money":
            m = nlu.extract_money(p, allow_bare=True)
            if m is not None and m.value <= 0:
                return None
            return m
        if k == "payee":
            payee, unresolved = nlu.extract_payee(p, self.contacts)
            if payee:
                return payee
            if unresolved:
                s.context["unresolved_name"] = unresolved
            return None
        if k == "text":
            return nlu.extract_note(p) if not fresh else (p.raw.strip().rstrip(".") or None)
        if k == "payment_type":
            return nlu.extract_payment_type(p)
        if k == "date_range":
            return p.date_range
        if k == "country":
            return nlu.extract_country(p)
        if k == "currency":
            return nlu.extract_currency(p)
        if k == "who":
            return nlu.extract_who(p)
        if k == "invoice_filter":
            return "all" if re.search(r"\b(all|every)( of)?( my| the)? invoices?\b|\bshow my invoices\b|\binvoice (list|history)\b", p.text) else "open"
        if k == "ref":
            return ("__ref__", p)            # resolved asynchronously in _advance
        return None

    # ---------------------------------------------------------------- slot filling, confirmation, execution
    async def _advance(self, s: Session, cap: Capability, slots: dict, now: datetime) -> TurnResult:
        # resolve references ("the first one", "jordan's invoice")
        for sl in cap.slots:
            v = slots.get(sl.name)
            if isinstance(v, tuple) and v and v[0] == "__ref__":
                res = await self._resolve_ref(s, cap, v[1], now)
                if isinstance(res, TurnResult):
                    return res
                if res is None:
                    slots.pop(sl.name)
                else:
                    slots[sl.name] = res
        for sl in cap.slots:
            if sl.required and slots.get(sl.name) is None:
                return self._ask(s, cap, slots, sl)
        limit = self._limit_check(s, cap, slots)
        if limit:
            s.pending = None
            return limit
        needs_confirm = cap.tier in ("T1", "T2") and cap.confirm is not None
        if needs_confirm:
            prompt = cap.confirm(self._ctx(s, now), slots)
            s.pending = Pending(cap.name, slots, "confirm", time.time(), s.turn)
            return TurnResult(prompt, {"title": "Please confirm", "lines": [prompt], "tier": cap.tier},
                              expecting="confirm", capability=cap.name, state="confirming")
        return await self._execute(s, cap, slots, now)

    def _ask(self, s: Session, cap: Capability, slots: dict, sl: Slot) -> TurnResult:
        prompt = sl.prompt or f"What {sl.name}?"
        if sl.kind == "payee" and s.context.get("unresolved_name"):
            nm = s.context["unresolved_name"]
            prompt = f"What's {nm.title() if not nm.startswith('my ') else 'your ' + nm[3:]}'s email or phone number?"
        s.pending = Pending(cap.name, slots, f"slot:{sl.name}", time.time(), s.turn)
        return TurnResult(prompt, {"title": cap.summary, "lines": [prompt]}, expecting=f"slot:{sl.name}",
                          capability=cap.name, state="asking")

    def _limit_check(self, s: Session, cap: Capability, slots: dict) -> TurnResult | None:
        if not cap.money_slot:
            return None
        amt: Money | None = slots.get(cap.money_slot)
        if amt is None and cap.name == "refund" and isinstance(slots.get("ref"), dict):
            amt = Money(Decimal(slots["ref"]["amount"]["value"]).copy_abs(), slots["ref"]["amount"].get("currency_code", "USD"))
        if amt is None:
            return None
        day = self._now().strftime("%Y-%m-%d")
        if s.spent_day != day:
            s.spent_day, s.spent_today = day, Decimal("0")
        if amt.value > self.config.max_per_action:
            return TurnResult(f"{amt} is over your {money({'value': self.config.max_per_action})} limit for one voice payment. "
                              "Do this one in the PayPal app, or raise the limit in settings.", {"title": "Over the limit"},
                              capability=cap.name, state="done")
        if s.spent_today + amt.value > self.config.daily_limit:
            return TurnResult(f"That would pass your {money({'value': self.config.daily_limit})} daily voice limit.",
                              {"title": "Over the daily limit"}, capability=cap.name, state="done")
        return None

    async def _continue(self, s: Session, p: Parsed, now: datetime) -> TurnResult | None:
        pend = s.pending
        cap = BY_NAME[pend.capability]
        age = time.time() - pend.asked_at
        t = p.text
        if pend.awaiting == "confirm":
            if age > CONFIRM_TTL:
                s.pending = None
                return TurnResult("That confirmation expired, so I didn't do it. Ask again if you still want to.", state="idle")
            if pend.asked_turn >= s.turn:
                return None                                   # confirmation must come from a later turn
            if nlu.YES.match(t):
                if cap.tier == "T2" and self.config.second_factor:
                    pend.awaiting, pend.factor_token = "second_factor", uuid.uuid4().hex
                    return TurnResult("Tap Confirm on screen to finish.", {"title": "Confirm on screen"},
                                      [{"type": "second_factor", "token": pend.factor_token}], expecting="second_factor",
                                      capability=cap.name, state="confirming")
                s.pending = None
                return await self._execute(s, cap, pend.slots, now)
            if nlu.NO.match(t):
                s.pending = None
                return TurnResult("Okay, I won't." + (" Nothing was sent." if cap.tier == "T2" else ""), state="idle", capability=cap.name)
            if nlu.CHANGE.search(t) or (p.amounts and not nlu.classify(p).capability):
                changed = []
                for sl in cap.slots:
                    if sl.kind in ("money", "payee", "text"):
                        v = self._extract_one(sl, cap, p, s, fresh=False)
                        if v is not None and sl.kind != "text":
                            pend.slots[sl.name] = v
                            changed.append(sl.name)
                if changed:
                    return await self._advance(s, cap, pend.slots, now)
            if nlu.classify(p).capability:
                s.pending = None
                return None                                   # a new request replaces the unconfirmed one
            pend.attempts += 1
            if pend.attempts >= 2:
                s.pending = None
                return TurnResult("I'll leave that for now. Nothing was sent.", state="idle")
            return TurnResult("Please say yes or no.", expecting="confirm", capability=cap.name, state="confirming")
        if pend.awaiting == "second_factor":
            if nlu.NO.match(t):
                s.pending = None
                return TurnResult("Okay, cancelled. Nothing was sent.", state="idle")
            return TurnResult("Waiting for you to tap Confirm on screen.", expecting="second_factor", capability=cap.name, state="confirming")
        if pend.awaiting.startswith("slot:") or pend.awaiting.startswith("choice:"):
            name = pend.awaiting.split(":", 1)[1]
            sl = next(x for x in cap.slots if x.name == name)
            if age > FRAME_TTL:
                s.pending = None
                return None
            if pend.awaiting.startswith("choice:"):
                pick = self._pick_choice(p, pend.choices)
                if pick is not None:
                    pend.slots[name] = pick
                    s.pending = None
                    return await self._advance(s, cap, pend.slots, now)
            else:
                v = self._extract_one(sl, cap, p, s, fresh=True)
                if sl.kind == "payee" and v is None and s.context.get("unresolved_name") and (p.emails or p.phones):
                    v = None
                if sl.kind == "payee":
                    v = self._payee_answer(p, s)
                if sl.kind == "who" and v is None and t:
                    v = t.split()[-1]
                if isinstance(v, tuple) and v[0] == "__ref__":
                    res = await self._resolve_ref(s, cap, p, now)
                    if isinstance(res, TurnResult):
                        return res
                    v = res
                if v is not None:
                    pend.slots[name] = v
                    # the answer may also carry other missing details ("40 dollars to sam")
                    for other in cap.slots:
                        if other.name != name and pend.slots.get(other.name) is None and other.kind in ("money", "payee"):
                            ov = self._extract_one(other, cap, p, s, fresh=False)
                            if ov is not None:
                                pend.slots[other.name] = ov
                    s.pending = None
                    return await self._advance(s, cap, pend.slots, now)
            if nlu.classify(p).capability:
                s.pending = None
                return None
            pend.attempts += 1
            if pend.attempts >= 2:
                s.pending = None
                return TurnResult("Let's start over when you're ready.", state="idle")
            return TurnResult(f"Sorry, I still need that. {sl.prompt or ''}".strip(), expecting=pend.awaiting,
                              capability=cap.name, state="asking")
        return None

    def _payee_answer(self, p: Parsed, s: Session) -> Payee | None:
        name = s.context.get("unresolved_name")
        if p.emails or p.phones:
            ident, typ = (p.emails[0], "EMAIL") if p.emails else (p.phones[0], "PHONE")
            payee = Payee(ident, typ, (name or "").title() if name and not name.startswith("my ") else "")
            if name:
                self.contacts.add(name, payee)
                s.context.pop("unresolved_name", None)
            return payee
        payee, unresolved = nlu.extract_payee(p, self.contacts)
        if payee:
            return payee
        c = self.contacts.find(p.text)
        if c:
            return Payee(c["id"], c["type"], c.get("name", ""))
        if unresolved or (p.text and len(p.text.split()) <= 2 and p.text.isalpha()):
            s.context["unresolved_name"] = unresolved or p.text
        return None

    def _pick_choice(self, p: Parsed, choices: list[dict]) -> dict | None:
        idx = nlu.extract_ordinal(p)
        if idx is not None and -len(choices) <= idx < len(choices):
            return choices[idx]
        for c in choices:
            who = c.get("who", "").lower()
            if who and (who in p.text or any(w in p.text.split() for w in who.split())):
                return c
        return None

    async def _resolve_ref(self, s: Session, cap: Capability, p: Parsed, now: datetime):
        kind = cap.ref_kind
        pool = [i for i in s.last_items if i.get("kind") == kind]
        idx = nlu.extract_ordinal(p)
        newest = kind == "transaction" and re.search(r"\b(last|latest|most recent|newest) (payment|one|order|sale|transaction)\b", p.text)
        if newest:
            idx = None                                     # "the last payment" means the newest, not the end of the list
            pool = await self._fetch_pool(s, kind, now)
            if pool:
                return pool[0]
        if idx is not None and pool and -len(pool) <= idx < len(pool):
            return pool[idx]
        if not pool or nlu.spoken_name(p.text) or nlu.extract_who(p):
            pool = await self._fetch_pool(s, kind, now)
        if not pool:
            noun = {"invoice": "unpaid invoices", "transaction": "payments to you in the last 31 days",
                    "dispute": "open disputes", "subscription": "subscriptions"}[kind]
            s.pending = None
            return TurnResult(f"I don't see any {noun}.", state="done", capability=cap.name)
        name = nlu.spoken_name(p.text) or (nlu.extract_who(p) if kind != "transaction" else None)
        if name:
            hits = [i for i in pool if name.lower() in i.get("who", "").lower() or name.lower() in i.get("email", "").lower()]
            if len(hits) >= 1:
                return hits[0]
            return TurnResult(f"I don't see one for {name.title()}.", state="done", capability=cap.name)
        if idx is not None and -len(pool) <= idx < len(pool):
            return pool[idx]
        if len(pool) == 1:
            return pool[0]
        if re.search(r"\b(last|latest|most recent) (payment|one|order)\b", p.text):
            return pool[0]
        names = [f"{i.get('who') or i.get('reason', '').replace('_', ' ').lower()} {money(i.get('amount'))}" for i in pool[:3]]
        s.pending = Pending(cap.name, {}, f"choice:{next(x.name for x in cap.slots if x.kind == 'ref')}", time.time(), s.turn,
                            choices=pool[:5])
        s.last_items = pool
        return TurnResult(f"Which one: {join_and(names)}?", {"title": "Which one?", "lines": names},
                          expecting=s.pending.awaiting, capability=cap.name, state="asking")

    async def _fetch_pool(self, s: Session, kind: str, now: datetime) -> list[dict]:
        from .registry import _inv_item, _item
        if kind == "invoice":
            r = await self.client.request("POST", "/v2/invoicing/search-invoices", op="invoices.search-invoices",
                                          json={"status": ["SENT", "UNPAID", "PARTIALLY_PAID"]}, params={"page_size": 20})
            return [_inv_item(i) for i in r.get("items", [])]
        if kind == "transaction":
            ctx = self._ctx(s, now)
            txns = await search_transactions(ctx, DateRange(now - timedelta(days=31), now, "the last 31 days"))
            pool = [_item(t) for t in txns if Decimal(t["transaction_info"]["transaction_amount"]["value"]) > 0
                    and t["transaction_info"].get("transaction_event_code", "").startswith("T00")]
            return sorted(pool, key=lambda i: i["date"], reverse=True)      # newest first, so "the last payment" is pool[0]
        if kind == "dispute":
            r = await self.client.request("GET", "/v1/customer/disputes", op="disputes.list", params={"page_size": 10})
            return [{"kind": "dispute", "id": d["dispute_id"], "amount": d.get("dispute_amount"), "reason": d.get("reason", ""),
                     "who": "", "status": d.get("status")} for d in r.get("items", []) if d.get("status") not in ("RESOLVED", "CLOSED")]
        if kind == "subscription":
            r = await self.client.request("GET", "/v1/billing/subscriptions", op="subscriptions.list", params={"page_size": 20})
            out = []
            for x in r.get("subscriptions", []):
                if x.get("status") in ("ACTIVE", "SUSPENDED"):
                    nm = x.get("subscriber", {}).get("name", {})
                    out.append({"kind": "subscription", "id": x["id"], "status": x.get("status"),
                                "who": " ".join(filter(None, [nm.get("given_name"), nm.get("surname")])),
                                "amount": x.get("billing_info", {}).get("last_payment", {}).get("amount")})
            return out
        return []

    async def _execute(self, s: Session, cap: Capability, slots: dict, now: datetime) -> TurnResult:
        slots = dict(slots)
        slots.setdefault("_idempotency_key", f"pv-{s.id[:8]}-{cap.name}-{uuid.uuid4().hex[:12]}")
        calls_before = len(self.client.calls)
        try:
            reply: Reply = await cap.run(self._ctx(s, now), slots)
        except PayPalError as e:
            if e.name == "TRANSACTION_DECLINED_ADDITIONAL_PAYEE_INFO_REQUIRED" and "payee" in slots:
                return TurnResult("PayPal needs the recipient's full name and country for this transfer. "
                                  "Say it like: send 50 dollars to Jane Doe in the UK at jane@example.com.",
                                  {"title": "More details needed"}, capability=cap.name, state="asking")
            self._log(s, cap, slots, "error", calls_before, error=f"{e.status} {e.name}")
            r = error_reply(e, cap.error_hint)
            return TurnResult(r.speech, r.display, capability=cap.name, state="error")
        if cap.money_slot and slots.get(cap.money_slot) is not None and cap.tier == "T2":
            s.spent_today += slots[cap.money_slot].value
        if reply.items:
            s.last_items = reply.items
        s.more = list(reply.more)
        s.context.update(reply.remember)
        if cap.tier != "T0":
            self._log(s, cap, slots, "ok", calls_before)
        return TurnResult(reply.speech, reply.display, reply.actions, capability=cap.name, state="done",
                          debug={"suggestions": reply.suggestions})

    async def second_factor(self, sid: str, token: str) -> TurnResult:
        return await self._traced(self._second_factor(sid, token))

    async def _second_factor(self, sid: str, token: str) -> TurnResult:
        s = self.session(sid)
        pend = s.pending
        if not pend or pend.awaiting != "second_factor" or token != pend.factor_token:
            return TurnResult("There's nothing waiting for confirmation.", state="idle")
        if time.time() - pend.asked_at > CONFIRM_TTL + 60:
            s.pending = None
            return TurnResult("That confirmation expired, so I didn't do it.", state="idle")
        s.pending = None
        return await self._execute(s, BY_NAME[pend.capability], pend.slots, self._now())

    async def paypal_return(self, sid: str, params: dict) -> TurnResult:
        """PayPal UI redirect lands here after the user approves (or abandons) a personal payment."""
        s = self.session(sid)
        tok = s.pending_token or {}
        if params.get("status", "").lower() == "success" and tok:
            s.pending_token = None
            self.audit.write(session=s.id, capability="send_money", event="paypal_ui_return", status="success",
                             transaction_id=params.get("transactionId"), token=tok.get("id"))
            return TurnResult(f"Done. {tok['amount']} is on its way to {tok['payee']}.", {"title": "Sent", "lines": [params.get("transactionId", "")]},
                              capability="send_money", state="done")
        return TurnResult("The payment wasn't approved, so no money was sent.", {"title": "Not sent"}, capability="send_money", state="done")

    # ---------------------------------------------------------------- helpers
    def _ctx(self, s: Session, now: datetime) -> Ctx:
        return Ctx(self.client, s, self.config, now)

    def _log(self, s: Session, cap: Capability, slots: dict, outcome: str, calls_before: int, error: str = "") -> None:
        safe = {k: (str(v) if isinstance(v, (Money, Payee)) else (v.get("id") if isinstance(v, dict) else v))
                for k, v in slots.items() if not k.startswith("_") and not isinstance(v, (DateRange,))}
        calls = self.client.calls[calls_before:]
        self.audit.write(session=s.id, capability=cap.name, tier=cap.tier, slots=safe, outcome=outcome, error=error,
                         idempotency_key=slots.get("_idempotency_key"),
                         paypal=[{"op": c["op"], "status": c["status"], "debug_id": c["debug_id"]} for c in calls])

    def _help(self, s: Session) -> TurnResult:
        mode = self.config.account_mode
        usable = [c for c in CAPABILITIES if c.availability != NONE and (c.mode == ANY or mode == "BOTH" or c.mode == mode)]
        sample = [c.examples[0] for c in usable[:6]]
        lines = [f"{c.summary} — \"{c.examples[0]}\"" for c in usable]
        return TurnResult("You can say things like: " + "; ".join(f'"{x}"' for x in sample[:4]) + ". More are on screen.",
                          {"title": "Things you can say", "lines": lines}, state="done", suggestions=sample[:4])

    def _sign_in(self, s: Session) -> TurnResult:
        if not self.config.login_enabled:
            return TurnResult("Sign-in with PayPal isn't set up for this app yet.", state="handoff")
        s.oauth_state = uuid.uuid4().hex
        url = self.client.login_url(f"{self.config.public_base_url}/auth/callback", f"{s.id}.{s.oauth_state}",
                                    "openid profile email https://uri.paypal.com/services/wallet/sendmoney")
        return TurnResult("Opening PayPal so you can sign in.", {"title": "Sign in with PayPal", "link": url},
                          [{"type": "open_url", "url": url, "purpose": "sign_in"}], state="asking")

    async def complete_sign_in(self, state: str, code: str) -> TurnResult:
        sid, _, nonce = state.partition(".")
        s = self.session(sid)
        if not nonce or nonce != s.oauth_state:
            return TurnResult("That sign-in link didn't match this session. Please try again.", state="error")
        s.user_token = await self.client.exchange_code(code)
        s.oauth_state = ""
        return await self._execute(s, BY_NAME["whoami"], {}, self._now())
