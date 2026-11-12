"""API-to-Voice: turn PayPal data and errors into short spoken replies plus a display card.

Rules: answer first; one or two sentences; at most ~25 words by default; lists say the count,
read the top three, and offer "more". Numbers stay as digits ("$40.50"); the text-to-speech
layer reads them aloud.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .paypal_client import PayPalError

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$", "MXN": "MX$", "JPY": "¥"}


@dataclass
class Reply:
    speech: str
    display: dict = field(default_factory=dict)        # {"title":..., "lines":[...], "items":[...], "link":...}
    actions: list[dict] = field(default_factory=list)  # e.g. {"type":"open_url","url":...}
    items: list[dict] = field(default_factory=list)    # referenceable results ("the first one")
    remember: dict = field(default_factory=dict)       # context ids (last_invoice_id, ...)
    more: list[str] = field(default_factory=list)      # spoken continuation for "more"
    suggestions: list[str] = field(default_factory=list)


def money(m: dict | None, signed: bool = False) -> str:
    if not m:
        return "an unknown amount"
    cur = m.get("currency_code") or m.get("currency") or "USD"
    v = Decimal(str(m.get("value", "0")))
    sign = "-" if signed and v < 0 else ""
    v = abs(v)
    num = f"{v:,.0f}" if v == v.to_integral_value() else f"{v:,.2f}"
    sym = SYMBOLS.get(cur)
    return f"{sign}{sym}{num}" if sym else f"{sign}{num} {cur}"


def when(ts: str | None, now: datetime | None = None) -> str:
    if not ts:
        return ""
    now = now or datetime.now(timezone.utc)
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00").replace("+0000", "+00:00"))
    except ValueError:
        try:
            d = datetime.strptime(ts[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return ts
    days = (now.date() - d.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days == -1:
        return "tomorrow"
    if 1 < days < 7:
        return d.strftime("%A")
    return d.strftime("%b %-d")


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def join_and(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def listing(intro: str, lines: list[str], per_turn: int = 3) -> tuple[str, list[str]]:
    """Speak the first `per_turn` lines; return (speech, remaining chunks for 'more')."""
    head = lines[:per_turn]
    rest = [". ".join(lines[i:i + per_turn]) + "." for i in range(per_turn, len(lines), per_turn)]
    speech = intro + (" " + ". ".join(head) + "." if head else "")
    if rest:
        speech += " Say more for the rest."
    return speech, rest


# ------------------------------------------------------------------ errors
ERROR_SPEECH = {
    "AUTHENTICATION_FAILURE": "I can't sign in to PayPal with this app's credentials. Check the client ID and secret.",
    "invalid_client": "I can't sign in to PayPal with this app's credentials. Check the client ID and secret.",
    "invalid_grant": "That PayPal sign-in didn't work. Please sign in again.",
    "NOT_AUTHORIZED": "This app isn't allowed to do that yet.",
    "PERMISSION_DENIED": "This app isn't allowed to do that yet.",
    "RESOURCE_NOT_FOUND": "I couldn't find that in PayPal.",
    "RATE_LIMIT_REACHED": "PayPal is busy right now. Try again in a minute.",
    "NETWORK_ERROR": "I can't reach PayPal right now.",
    "INTERNAL_SERVER_ERROR": "PayPal had a problem on its side. Nothing was changed; try again shortly.",
    "INVALID_REQUEST": "PayPal rejected that request. I've logged the details for the developer.",
}
ISSUE_SPEECH = {
    "REFUND_AMOUNT_EXCEEDED": "That's more than the original payment.",
    "CAPTURE_FULLY_REFUNDED": "That payment was already fully refunded.",
    "INVALID_INVOICE_STATUS": "That invoice can't do that in its current state.",
    "INSTRUMENT_DECLINED": "The payment method was declined. Try a different one in PayPal.",
    "INSUFFICIENT_FUNDS": "There isn't enough money available for that.",
    "PAYEE_ACCOUNT_RESTRICTED": "The recipient's account can't receive payments right now.",
    "PAYEE_SAME_AS_PAYER": "You can't send money to yourself.",
    "INVALID_DATE_RANGE": "PayPal only searches 31 days at a time.",
}


def error_reply(e: PayPalError, hint: str = "") -> Reply:
    speech = next((ISSUE_SPEECH[i] for i in e.issues if i in ISSUE_SPEECH), None) \
        or ERROR_SPEECH.get(e.name) \
        or (f"PayPal said: {e.message.rstrip('.')}." if e.message else "PayPal returned an error.")
    if hint and e.name in ("NOT_AUTHORIZED", "PERMISSION_DENIED"):
        speech += " " + hint
    lines = [f"{e.status} {e.name}", e.message or ""]
    if e.issues:
        lines.append("Issues: " + ", ".join(e.issues))
    if e.debug_id:
        lines.append(f"PayPal debug id: {e.debug_id}")
    return Reply(speech, {"title": "PayPal error", "lines": [x for x in lines if x], "error": True})


def as_json_safe(x: Any) -> Any:
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, dict):
        return {k: as_json_safe(v) for k, v in x.items()}
    if isinstance(x, list):
        return [as_json_safe(v) for v in x]
    return x
