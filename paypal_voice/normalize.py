"""Turn a raw speech-recognition transcript into normalized text plus typed entities.

Recognizers return text like "send forty two fifty to sam at gmail dot com" or
"what did I spend last month". Everything downstream (intent matching, slot filling)
works on the output of `normalize()`, never on the raw transcript.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

UNITS = {
    "zero": 0, "oh": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
        "seventy": 70, "eighty": 80, "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1000, "grand": 1000, "k": 1000, "million": 1_000_000}
NUMBER_WORDS = set(UNITS) | set(TENS) | set(SCALES) | {"and"}
CURRENCY_WORDS = {
    "dollar": "USD", "dollars": "USD", "bucks": "USD", "buck": "USD", "usd": "USD",
    "euro": "EUR", "euros": "EUR", "eur": "EUR", "pound": "GBP", "pounds": "GBP", "quid": "GBP",
    "gbp": "GBP", "peso": "MXN", "pesos": "MXN", "yen": "JPY", "canadian dollars": "CAD",
    "aussie dollars": "AUD",
}
CENT_WORDS = {"cent", "cents", "penny", "pennies"}
FILLERS = re.compile(r"\b(uh+|um+|erm|hmm+|like,|you know|please|pls|kindly)\b")
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


@dataclass
class Money:
    value: Decimal
    currency: str = "USD"

    def as_api(self, key: str = "currency_code") -> dict:
        return {key: self.currency, "value": f"{self.value:.2f}" if self.currency != "JPY" else f"{self.value:.0f}"}

    def __str__(self) -> str:  # display form
        symbol = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$", "MXN": "MX$", "JPY": "¥"}.get(self.currency)
        whole = self.value == self.value.to_integral_value()
        num = f"{self.value:,.0f}" if whole or self.currency == "JPY" else f"{self.value:,.2f}"
        return f"{symbol}{num}" if symbol else f"{num} {self.currency}"


@dataclass
class DateRange:
    start: datetime
    end: datetime
    label: str


@dataclass
class Parsed:
    raw: str
    text: str                      # normalized, lower-case, numbers as digits
    amounts: list[Money] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    numbers: list[Decimal] = field(default_factory=list)
    date_range: DateRange | None = None
    ids: list[str] = field(default_factory=list)   # PayPal-style IDs the user read out

    def amount_or_bare(self, default_currency: str = "USD") -> Money | None:
        """A currency amount if one was said; else the first bare number (for 'send 40 to Sam')."""
        if self.amounts:
            return self.amounts[0]
        if self.numbers:
            return Money(self.numbers[0], default_currency)
        return None


# ---------------------------------------------------------------- number words

def words_to_number(tokens: list[str]) -> Decimal | None:
    """'two hundred and forty five' -> 245. Returns None if tokens are not a number."""
    total, current, seen = 0, 0, False
    for t in tokens:
        if t == "and":
            continue
        if t in UNITS:
            current += UNITS[t]
        elif t in TENS:
            current += TENS[t]
        elif t == "hundred":
            current = (current or 1) * 100
        elif t in ("thousand", "grand", "k", "million"):
            total += (current or 1) * SCALES[t]
            current = 0
        else:
            return None
        seen = True
    return Decimal(total + current) if seen else None


def _replace_number_words(text: str) -> str:
    """Replace runs of number words with digits; handle 'twelve fifty' as 12.50 before a currency word."""
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in NUMBER_WORDS and tokens[i] not in ("and", "a", "an", "oh", "k"):
            j = i
            while j < len(tokens) and tokens[j] in NUMBER_WORDS:
                j += 1
            run = tokens[i:j]
            while run and run[-1] == "and":   # 'forty and' -> keep 'and' outside the number
                run.pop()
                j -= 1
            n = words_to_number(run)
            if n is None:
                out.append(tokens[i]); i += 1; continue
            # "twelve fifty dollars" -> 12.50 ; "two twenty five" -> 2.25 (price idiom, two groups)
            split = _price_idiom(run)
            out.append(split if split else str(n))
            i = j
        elif tokens[i] in ("a", "an") and i + 1 < len(tokens) and tokens[i + 1] in ("hundred", "thousand", "grand"):
            j = i + 1
            while j < len(tokens) and tokens[j] in NUMBER_WORDS:
                j += 1
            n = words_to_number(["one"] + tokens[i + 1:j])
            out.append(str(n)); i = j
        else:
            out.append(tokens[i]); i += 1
    return " ".join(out)


def _price_idiom(run: list[str]) -> str | None:
    """'twelve fifty' -> '12.50', 'two twenty five' -> '2.25'. Only for a short unit/teen + tens pattern."""
    if any(t in SCALES or t == "and" for t in run):
        return None
    if len(run) == 2 and run[0] in UNITS and run[1] in TENS and UNITS[run[0]] >= 1:
        return f"{UNITS[run[0]]}.{TENS[run[1]]:02d}"
    if len(run) == 3 and run[0] in UNITS and run[1] in TENS and run[2] in UNITS and 1 <= UNITS[run[2]] <= 9:
        return f"{UNITS[run[0]]}.{TENS[run[1]] + UNITS[run[2]]:02d}"
    return None


# ---------------------------------------------------------------- entities

EMAIL_SPOKEN = re.compile(r"\b([a-z0-9._%+-]+(?: dot [a-z0-9_-]+)*) at ([a-z0-9-]+(?: dot [a-z0-9-]+)+)\b")
EMAIL = re.compile(r"\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,}\d(?![\w.])")
PAYPAL_ID = re.compile(r"\b(?:inv2(?:-[a-z0-9]{4}){4}|pp-[a-z0-9]{10,}|pp-d-[a-z0-9-]+|i-[a-z0-9]{10,}|[a-z0-9]{17})\b")


def _spoken_emails(text: str) -> str:
    def fix(m: re.Match) -> str:
        local = m.group(1).replace(" dot ", ".")
        domain = m.group(2).replace(" dot ", ".")
        return f"{local.replace(' ', '')}@{domain.replace(' ', '')}"
    return EMAIL_SPOKEN.sub(fix, text)


def _amounts(text: str) -> list[Money]:
    return [m for _, _, m in _amount_spans(text)]


def _amount_spans(text: str) -> list[tuple[int, int, Money]]:
    found: list[tuple[int, int, Money]] = []
    # $12.50 / $1,200 / 12.50 dollars / 40 bucks / 5 dollars and 20 cents / 25 euros
    for m in re.finditer(r"([$€£¥])\s?(\d[\d,]*(?:\.\d{1,2})?)", text):
        cur = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}[m.group(1)]
        found.append((m.start(), m.end(), Money(_dec(m.group(2)), cur)))
    cur_words = "|".join(sorted((re.escape(w) for w in CURRENCY_WORDS), key=len, reverse=True))
    for m in re.finditer(rf"(?<![$€£¥\d.,])(\d[\d,]*(?:\.\d{{1,2}})?)\s?({cur_words})\b(?:\s(?:and\s)?(\d{{1,2}})\s(?:cent|cents))?", text):
        value = _dec(m.group(1))
        if m.group(3):
            value += Decimal(m.group(3)) / 100
        found.append((m.start(), m.end(), Money(value, CURRENCY_WORDS[m.group(2)])))
    for m in re.finditer(r"(?<![$\d.])(\d{1,2})\s(?:cent|cents)\b", text):
        if not any(abs(pos - m.start()) < 25 for pos, _, _ in found):
            found.append((m.start(), m.end(), Money(Decimal(m.group(1)) / 100, "USD")))
    found.sort(key=lambda x: x[0])
    return found


def _dec(s: str) -> Decimal:
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return Decimal(0)


def _phones(text: str) -> list[str]:
    out = []
    for m in PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 10 <= len(digits) <= 15:
            if len(digits) == 10:
                digits = "1" + digits          # assume North American number; E.164 needs the country code
            out.append("+" + digits)
    return out


def _date_range(text: str, now: datetime) -> DateRange | None:
    today = now.astimezone(timezone.utc).date()
    start_of = lambda d: datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    end_of = lambda d: datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
    if re.search(r"\btoday\b", text):
        return DateRange(start_of(today), now, "today")
    if re.search(r"\byesterday\b", text):
        y = today - timedelta(days=1)
        return DateRange(start_of(y), end_of(y), "yesterday")
    m = re.search(r"\b(?:last|past) (\d{1,3}) days?\b", text)
    if m:
        return DateRange(start_of(today - timedelta(days=int(m.group(1)))), now, f"the last {m.group(1)} days")
    if re.search(r"\bthis week\b", text):
        return DateRange(start_of(today - timedelta(days=today.weekday())), now, "this week")
    if re.search(r"\blast week\b", text):
        s = today - timedelta(days=today.weekday() + 7)
        return DateRange(start_of(s), end_of(s + timedelta(days=6)), "last week")
    if re.search(r"\bthis month\b", text):
        return DateRange(start_of(today.replace(day=1)), now, "this month")
    if re.search(r"\blast month\b", text):
        last_end = today.replace(day=1) - timedelta(days=1)
        return DateRange(start_of(last_end.replace(day=1)), end_of(last_end), "last month")
    m = re.search(r"\b(?:in|during|for) (" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b", text)
    if m:
        month = MONTHS[m.group(1)]
        year = today.year if month <= today.month else today.year - 1
        last_day = calendar.monthrange(year, month)[1]
        end = min(end_of(date(year, month, last_day)), now)
        return DateRange(start_of(date(year, month, 1)), end, calendar.month_name[month])
    return None


# ---------------------------------------------------------------- public

def normalize(raw: str, now: datetime | None = None) -> Parsed:
    now = now or datetime.now(timezone.utc)
    t = raw.strip().lower()
    t = t.replace("’", "'").replace("‘", "'")
    t = re.sub(r"\bpay ?pal'?s?\b", "paypal", t)
    t = re.sub(r"\bpay pal\b", "paypal", t)
    t = FILLERS.sub(" ", t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)             # 1,200 -> 1200
    t = re.sub(r"[!?;:\"]", " ", t)
    t = re.sub(r"(?<![\w$€£¥])[.,](?!\d)|(?<=[a-z])[.,](?=\s|$)", " ", t)  # sentence punctuation, keep 12.50 / emails
    t = re.sub(r"\s+", " ", t).strip()
    t = _replace_number_words(t)
    t = _spoken_emails(t)
    t = re.sub(r"\b(\d+) point (\d{1,2})\b", r"\1.\2", t)
    p = Parsed(raw=raw, text=t)
    p.emails = EMAIL.findall(t)
    no_email = EMAIL.sub(" ", t)
    spans = _amount_spans(no_email)
    p.amounts = [m for _, _, m in spans]
    masked = no_email
    for a, b, _ in sorted(spans, key=lambda x: -x[0]):
        masked = masked[:a] + " ~ " + masked[b:]
    p.phones = _phones(masked)
    no_phone = PHONE.sub(" ", masked) if p.phones else masked
    p.numbers = [_dec(x) for x in re.findall(r"(?<![\w.$])\d+(?:\.\d+)?(?![\w.])", no_phone)]
    p.date_range = _date_range(t, now)
    p.ids = [i.upper() for i in PAYPAL_ID.findall(t) if any(c.isdigit() for c in i) and any(c.isalpha() for c in i)]
    return p
