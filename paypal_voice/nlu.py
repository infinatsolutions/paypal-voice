"""Voice-to-API, step 2: decide what the user meant (intent) and pull out the details (slots)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .normalize import CURRENCY_WORDS, Money, Parsed
from .registry import CAPABILITIES, COUNTRIES, Capability, Payee

YES = re.compile(r"^(yes|yeah|yep|yup|ya|sure|ok|okay|correct|right|confirm|confirmed|do it|go ahead|send it|that'?s right|"
                 r"that is right|affirmative|please do|yes please|absolutely|definitely|of course|go for it|sounds good|"
                 r"refund it|cancel it|accept it|bill them|pay (it|them)|make it|create it|remind them|send the invoice)\b")
NO = re.compile(r"^(no|nope|nah|don'?t|do not|negative|not now|wrong|wait|hold on)\b")
CANCEL = re.compile(r"^(cancel|stop|never ?mind|forget (it|that)|abort|quit|exit)( (it|that|this|everything))?$")
REPEAT = re.compile(r"^(repeat( that)?|say (that|it) again|again|what did you say|come again|pardon|sorry what)$")
MORE = re.compile(r"^(more|next|keep going|go on|continue|the rest|tell me more|what else|and)$")
HELP = re.compile(r"\b(help|what can (you|i) (do|say|ask)|what do you do|how does this work)\b")
SIGN_IN = re.compile(r"\b(sign|log) ?in\b|\bconnect (my )?paypal\b")
ORDINALS = {"first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1, "third": 2, "3rd": 2, "three": 2,
            "fourth": 3, "4th": 3, "fifth": 4, "5th": 4, "last": -1, "latest": 0, "most recent": 0}
ORDINAL = re.compile(r"\b(?:the )?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last|latest|most recent)\b(?: one)?|\bnumber (\d)\b|^(one|two|three)$")
CHANGE = re.compile(r"\b(change|make (it|that)|actually|instead|switch)\b")

STOP_NAMES = {"me", "my", "the", "a", "an", "it", "them", "him", "her", "us", "you", "paypal", "money", "cash", "dollars",
              "bucks", "dollar", "invoice", "bill", "payment", "link", "back", "for", "to", "from", "please", "now", "today",
              "tomorrow", "yesterday", "all", "this", "that", "out", "someone", "somebody", "everyone", "and", "as",
              "goods", "services", "personal", "friend", "with", "at", "on", "in", "up", "about", "again", "last", "first",
              "is", "was", "be", "get", "got", "ask", "request", "remind", "refund", "cancel", "accept", "who", "what",
              "how", "much", "any", "every", "next", "new", "one", "of", "their", "his", "her", "our", "your",
              "send", "pay", "give", "transfer", "bill", "charge", "buy", "make", "create", "check", "see", "know",
              "invoices", "someone's", "people", "person", "contractor", "vendor", "do", "does", "would", "will", "can",
              "could", "should", "cost", "fee", "fees", "there", "here", "my", "i", "we", "they"}
RELATIONS = {"mom", "mum", "mother", "dad", "father", "brother", "sister", "son", "daughter", "wife", "husband", "partner",
             "boss", "landlord", "roommate", "grandma", "grandpa", "aunt", "uncle", "cousin", "friend", "babysitter"}


@dataclass
class Understanding:
    capability: Capability | None
    candidates: list[str] = field(default_factory=list)


def classify(p: Parsed) -> Understanding:
    hits = [c for c in CAPABILITIES if c.matches(p.text)]
    if not hits:
        return Understanding(None)
    hits.sort(key=lambda c: -c.priority)
    return Understanding(hits[0], [c.name for c in hits])


# ---------------------------------------------------------------- slot extraction

def extract_payee(p: Parsed, contacts) -> tuple[Payee | None, str | None]:
    """Returns (resolved payee, unresolved spoken name)."""
    name = spoken_name(p.text)
    if name and any(e.startswith(name.split()[-1]) for e in p.emails):
        name = None                                   # "to newfriend@x.com": the email's local part is not a name
    if p.emails:
        c = contacts.by_id(p.emails[0])
        return Payee(p.emails[0], "EMAIL", c.get("name", "") if c else (name or "").title()), None
    if p.phones:
        c = contacts.by_id(p.phones[0])
        return Payee(p.phones[0], "PHONE", c.get("name", "") if c else (name or "").title()), None
    if name:
        c = contacts.find(name)
        if c:
            return Payee(c["id"], c["type"], c.get("name") or name.title(), c.get("country_code", "")), None
        return None, name
    return None, None


NAME_PATTERNS = [
    r"\b(?:send|pay|give|transfer|tip|bill|invoice|charge|remind|refund|payout to|pay out to)\s+(?:back\s+)?(?:to\s+)?((?:my\s+)?[a-z][a-z'-]+(?:\s[a-z][a-z'-]+)?)",
    r"\bto\s+((?:my\s+)?[a-z][a-z'-]+(?:\s[a-z][a-z'-]+)?)",
    r"\binvoice for\s+((?:my\s+)?[a-z][a-z'-]+)",
    r"\bfrom\s+((?:my\s+)?[a-z][a-z'-]+(?:\s[a-z][a-z'-]+)?)",
    r"\b([a-z]+)'s\b",
]


_NOT_POSSESSIVE = {"what", "it", "that", "who", "let", "there", "here", "how", "where", "when", "he", "she", "everyone", "today", "paypal"}


def spoken_name(text: str) -> str | None:
    for m in re.finditer(r"\b([a-z]+)'s\b", text):       # "jordan's invoice" names the person outright
        if m.group(1) not in _NOT_POSSESSIVE and m.group(1) not in STOP_NAMES and m.group(1) not in RELATIONS:
            return m.group(1)
    for pat in NAME_PATTERNS:
        for m in re.finditer(pat, text):
            words = m.group(1).replace("'s", "").split()
            if words and words[0] == "my":
                if len(words) > 1 and words[1] in RELATIONS:
                    return "my " + words[1]
                continue
            words = [w for w in words if w not in STOP_NAMES and not w[0].isdigit()]
            # keep at most two name words, stop at the first stop word
            kept = []
            for w in m.group(1).replace("'s", "").split():
                if w in STOP_NAMES or w[0].isdigit():
                    break
                kept.append(w)
            if kept and kept[0] not in RELATIONS and len(kept[0]) > 1:
                return " ".join(kept[:2])
    return None


NOTE_RX = re.compile(r"\b(?:for|note|memo|saying|that says|reason|because)\s+(.+)$")
AMOUNT_RX = re.compile(r"^(?:[$€£¥]\s?)?\d[\d.,]*\s?(?:" + "|".join(sorted(CURRENCY_WORDS, key=len, reverse=True)) + r")?\b\s*")


def extract_note(p: Parsed) -> str | None:
    t = re.sub(r"\bas (a )?(goods and services|goods or services|personal( payment)?|friends and family|purchase)\b", "", p.text)
    t = re.sub(r"\b(?:to|from)\s+\S+@\S+", "", t).strip()
    t = re.sub(r"\binvoice for\s+(?:my\s+)?[a-z]+\b", "invoice", t)      # "an invoice for sam for 60 dollars": sam is the payee
    note = None
    for m in NOTE_RX.finditer(t):
        rest = m.group(1).strip()
        stripped = AMOUNT_RX.sub("", rest, count=1)
        if stripped != rest:                          # "for 25 dollars for a small print" -> try the next "for"
            stripped = re.sub(r"^(for|note|memo)\s+", "", stripped).strip(" .,!?")
            if not stripped or NOTE_RX.match("for " + stripped) is None:
                continue
        note = stripped
        break
    if not note:
        return None
    note = re.sub(r"^(the|a|an)\s+", "", note) if len(note.split()) > 1 else note
    if re.fullmatch(r"[$€£¥]?\s?\d[\d.,]*\s?(" + "|".join(CURRENCY_WORDS) + r")?", note):
        return None                                     # "for 85 dollars" is an amount, not a note
    note = re.sub(r"\s*(?:to|from)\s+\w+$", "", note)
    return note or None


def extract_payment_type(p: Parsed) -> str:
    return "PURCHASE" if re.search(r"\b(goods and services|goods or services|goods|as a purchase|for services|for a service)\b", p.text) else "PERSONAL"


def extract_direction(p: Parsed) -> str | None:
    if re.search(r"\b(spend|spent|paid out|money out|did i (pay|send))\b", p.text):
        return "out"
    if re.search(r"\b(make|made|earn|earned|receive|received|came in|come in|money in|paid me|sell|sold|get|got)\b", p.text):
        return "in"
    return None


def extract_country(p: Parsed) -> str | None:
    for k in sorted(COUNTRIES, key=len, reverse=True):
        if re.search(rf"\b{k}\b", p.text):
            return COUNTRIES[k]
    return None


def extract_currency(p: Parsed) -> str | None:
    m = re.search(r"\b(?:in|into|to)\s+(" + "|".join(sorted(CURRENCY_WORDS, key=len, reverse=True)) + r")\b", p.text)
    return CURRENCY_WORDS[m.group(1)] if m else None


def extract_ordinal(p: Parsed) -> int | None:
    m = ORDINAL.search(p.text)
    if not m:
        return None
    if m.group(2):
        return int(m.group(2)) - 1
    word = m.group(1) or m.group(3)
    return ORDINALS.get(word)


def extract_who(p: Parsed) -> str | None:
    m = re.search(r"\b(?:did|has|have|when did)\s+(?!i\b|you\b|my\b|the\b)([a-z]+)\b", p.text)
    return m.group(1) if m else spoken_name(p.text)


def extract_money(p: Parsed, allow_bare: bool) -> Money | None:
    return p.amount_or_bare() if allow_bare else (p.amounts[0] if p.amounts else None)
