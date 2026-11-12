# PayPal Voice

Run your PayPal account by talking to it. You say "who owes me money" or "refund Sam"; PayPal Voice works out
what you mean, asks for anything missing, reads risky actions back for confirmation, calls the PayPal REST API,
and speaks the answer. **35 voice commands reach 32 PayPal REST endpoints.**

Built for the PayPal hackathon.

> **Status and disclaimer.** This is a prototype. It runs end to end against a built-in PayPal simulator and is
> wired for the PayPal Sandbox, but it has not yet been run against PayPal's real servers. It is an independent
> project, not affiliated with or endorsed by PayPal.

## What you get

- **Voice in, voice out.** Press the mic (or Space) and talk; replies are spoken back. An optional hands-free mode
  listens for "Hey PayPal".
- **A live Voice → API → Voice trace.** Every turn shows what was heard, the command it matched, the exact PayPal
  REST calls (method, path, status, time) and the spoken reply.
- **An endpoint panel.** All 32 endpoints are listed and light up as they are called. Tapping one runs its command.
- **A guided demo.** One button speaks a 47-phrase script and calls every endpoint.

## Setup

**You need:** macOS or Linux, Python 3.10 or newer, and Chrome, Edge or Safari (Firefox works by typing only).
No PayPal account is needed for the simulator.

**Quick start (macOS):**

```bash
git clone https://github.com/infinatsolutions/paypal-voice.git
cd paypal-voice
./start.sh
```

`start.sh` finds or installs Python, creates a private `.venv`, installs the packages, creates `.env`, runs the
tests, starts the server and opens **http://localhost:8787**. Options: `--sandbox`, `--chat` (terminal instead of
browser), `--demo` (scripted conversation), `--port N`, `--skip-tests`, `--reset`, `--help`.

**By hand (any platform):**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest                              # 239 tests against the simulator
uvicorn paypal_voice.server:app --port 8787   # then open http://localhost:8787
```

**Connect your PayPal Sandbox:**

1. In the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard), open **Apps & Credentials**
   (Sandbox) and create an app.
2. Run `./start.sh --sandbox` and paste the client ID and secret when asked (or put them in `.env`; see
   `.env.example`). Never commit `.env`.
3. In the app's settings, turn on **Transaction Search** (balance and activity). PayPal can take up to 9 hours to
   enable it.
4. Features that need PayPal's approval stay off until you turn them on in `.env`: Personal Payments
   (`PERSONAL_PAYMENTS_ENABLED`), Log in with PayPal (`LOGIN_WITH_PAYPAL_ENABLED`) and Payouts (`PAYOUTS_ENABLED`).

## How we used PayPal

PayPal Voice is a voice layer over the PayPal REST API. Each voice command maps to one or more real endpoints:

| Say | What happens | PayPal endpoint(s) |
| --- | --- | --- |
| "who am i signed in as" | Tell you which PayPal account you're signed in with | `GET /v1/identity/openidconnect/userinfo` |
| "send 20 dollars to sam" | Send money to a person (you approve it in PayPal) | `POST /v2/personal-payments/tokens` |
| "how much does it cost to send 50 dollars to sam" | Check fees and arrival time before sending | `POST /v1/payments/personal-payments/funding-options` |
| "did my payment go through" | Check whether your last payment went through | `GET /v2/personal-payments/tokens/{id}` |
| "can i send money to mexico" | Check whether you can send money to a country | `POST /v1/payments/personal-payments/assess-capabilities` |
| "what's my balance" | Read your PayPal balance | `GET /v1/reporting/balances` |
| "what came in this week" | Summarize money in and out for a period | `GET /v1/reporting/transactions` |
| "did jordan pay me" | Find payments from or to a person | `GET /v1/reporting/transactions` |
| "who owes me money" | List unpaid invoices | `POST /v2/invoicing/search-invoices`<br>`GET /v2/invoicing/invoices` |
| "bill sam 40 dollars for framing" | Send someone an invoice | `POST /v2/invoicing/generate-next-invoice-number`<br>`POST /v2/invoicing/invoices`<br>`POST /v2/invoicing/invoices/{id}/send` |
| "remind jordan about the invoice" | Remind someone about an unpaid invoice | `POST /v2/invoicing/invoices/{id}/remind` |
| "cancel priya's invoice" | Cancel a sent invoice | `POST /v2/invoicing/invoices/{id}/cancel` |
| "make a payment link for 25 dollars for a small print" | Make a payment link to share | `POST /v1/checkout/payment-resources` |
| "refund sam" | Refund a payment you received | `GET /v1/reporting/transactions`<br>`POST /v2/payments/captures/{id}/refund` |
| "any disputes" | List open disputes and claims | `GET /v1/customer/disputes` |
| "accept the claim" | Accept a dispute claim (refunds the buyer) | `POST /v1/customer/disputes/{id}/accept-claim` |
| "show my subscriptions" | List active subscriptions | `GET /v1/billing/subscriptions` |
| "cancel maya's subscription" | Cancel a subscriber's subscription | `POST /v1/billing/subscriptions/{id}/cancel` |
| "pay out 50 dollars to sam@example.com" | Pay a contractor or vendor from your business balance | `POST /v1/payments/payouts` |
| "how much is 100 dollars in euros" | Quote a currency conversion | `POST /v2/pricing/quote-exchange-rates` |
| "jordan's invoice details" | Read one invoice's status and due date | `GET /v2/invoicing/invoices/{id}` |
| "mark jordan's invoice as paid" | Record a payment you got outside PayPal | `POST /v2/invoicing/invoices/{id}/payments` |
| "what's the dispute about" | Explain what a dispute is about | `GET /v1/customer/disputes/{id}` |
| "when does maya's subscription renew" | Read one subscription's status and next bill | `GET /v1/billing/subscriptions/{id}` |
| "pause maya's subscription" | Pause a subscriber's billing | `POST /v1/billing/subscriptions/{id}/suspend` |
| "resume maya's subscription" | Resume a paused subscription | `POST /v1/billing/subscriptions/{id}/activate` |
| "did the payout go through" | Check whether your last payout went through | `GET /v1/payments/payouts/{id}` |
| "did the refund go through" | Check whether your last refund went through | `GET /v2/payments/refunds/{id}` |
| "what plans do i offer" | List the subscription plans you offer | `GET /v1/billing/plans` |
| "list my products" | List the products in your catalog | `GET /v1/catalogs/products` |

Five more commands (rewards, savings, bank transfers, card controls, crypto) have no public PayPal API. For
those the app says so in one sentence and offers a link to PayPal, instead of pretending.

**PayPal products used**

- **OAuth 2.0** client credentials for the app, and the authorization-code flow with **Log in with PayPal**
  (OpenID Connect) for signed-in users.
- **Personal Payments API** (limited release) for sending money to people, fee checks and country checks. Payments
  finish on PayPal's own approval screen, so PayPal handles sign-in and regulatory disclosures.
- **Transaction Search, Invoicing, Payments (refunds), Payment Links, Disputes, Subscriptions, Catalog Products,
  Payouts** and the currency-exchange quote endpoint, acting on the app owner's business account.
- **PayPal's official OpenAPI specifications** (`specs/`). Every request the app builds is validated against
  them, in the simulator and in tests, so a malformed call fails here the way it would at PayPal.
- **PayPal Sandbox** as the target environment (`./start.sh --sandbox`).

**What the research found** (details and sources in `research/FINDINGS.md`; all 167 operations are catalogued in
`research/API_CATALOG.md`):

- Personal Payments and Log in with PayPal need PayPal's approval for each app.
- Balance, activity, invoices, refunds, disputes, subscriptions and payouts act on the API caller's own business
  account. Reading another person's balance or history needs PayPal partner status.
- Payouts must be enabled per account.

**The simulator.** `paypal_voice/mock_paypal.py` is a stateful stand-in for PayPal with sample balances,
transactions, invoices, a dispute and a subscription. It lets the whole app run with no account, and it is what
the demo and the tests use. Approving a personal payment is a stand-in button there, because PayPal's approval
screen only exists at PayPal.

## How we used AI

- **To build it.** The project was built with [Claude](https://claude.com) (Anthropic), directed by the author.
  Claude read PayPal's API documentation and OpenAPI specifications, produced the research notes in `research/`,
  and wrote the engine, the PayPal simulator, the demo app, the launcher and the tests. It also ran the test suite
  and drove the demo app in a headless browser to check it.
- **In the running app.** Speech recognition and spoken replies use the browser's Web Speech API
  (`SpeechRecognition` and `SpeechSynthesis`). In Chrome, recognition is done by Google's speech service.
- **Not in the running app, on purpose.** Understanding a command is done by deterministic rules
  (`normalize.py`, `nlu.py`, `registry.py`), not by a language model. The same words always produce the same
  action, every path is covered by tests, and no model can invent a payee or an amount.

## Other tools and technologies

| Area | What |
| --- | --- |
| Languages | Python 3.10+, JavaScript, HTML5, CSS3, Bash |
| Backend | FastAPI, Uvicorn, httpx, Pydantic |
| Contract checks | jsonschema, referencing, PayPal's OpenAPI specs |
| Front end | One self-contained HTML page, no framework; Web Speech API, Fetch API, Clipboard API |
| Tests | pytest (239 tests); Playwright with headless Chrome was used during development |
| Storage | No database. In-memory sessions, a hash-chained (SHA-256) JSONL audit log, `contacts.json`, `.env` |
| Setup | `start.sh` (uses Homebrew or uv only if Python has to be installed) |

## How a turn works

```
"bill maya forty five dollars for a framed print"
  1 normalize     -> "bill maya 45 dollars for a framed print"; amount $45; name "maya"
  2 classify      -> invoice_create (from the capability registry)
  3 fill slots    -> payee = Maya Chen <maya@example.com> (contacts); note = "framed print"
  4 gate          -> account mode, PayPal approval status, spending limits
  5 confirm (T2)  -> "Send Maya Chen at maya@example.com an invoice for $45 for framed print. Send it?"
  6 "yes"         -> next turn, within 90 s
  7 execute       -> generate-next-invoice-number -> invoices.create -> invoices.send (idempotency keys)
  8 speak         -> "Sent. Maya Chen has an invoice for $45 for framed print."
  9 audit         -> hash-chained log entry with PayPal debug ids
```

| Module | Role |
| --- | --- |
| `normalize.py` | Speech text to clean text and entities: number words ("twelve fifty" = $12.50), spoken emails, phones, currencies, dates, PayPal IDs |
| `nlu.py` | Intent matching and slot extraction (payee, note, goods vs personal, direction, country, currency, "the second one") |
| `registry.py` | **The capability table**: patterns, slots, account mode, availability, risk tier, PayPal operations, confirmation text, and the run + speak function |
| `engine.py` | Dialog: slot questions, confirmations, choices, limits, gating, sign-in, PayPal return, audit |
| `paypal_client.py` | OAuth (app and user tokens), `PayPal-Request-Id` idempotency, retries on 429/5xx, typed errors with debug ids |
| `speak.py` | API-to-Voice: short spoken replies, list paging ("say more"), plain-English error messages |
| `contracts.py` | Validates request bodies against PayPal's OpenAPI specs (`specs/`) |
| `mock_paypal.py` | Stateful PayPal simulator used by tests and `PAYPAL_ENV=mock` |
| `server.py` | FastAPI endpoints |

## Safety model

| Tier | Examples | Rule |
| --- | --- | --- |
| T0 read | balance, activity, invoices, disputes, fees, statuses, plans, products | Runs immediately |
| T1 reversible | invoice reminder, payment link, mark invoice paid, pause or resume a subscription | Read back, spoken yes |
| T2 money or irreversible | send money, invoice, refund, payout, accept claim, cancel | Full read-back; yes must come in a **later** turn within 90 s; optional on-screen second factor (`SECOND_FACTOR=true`); per-action and daily limits |

Sending personal money always finishes on PayPal's own approval screen (PayPal UI pattern), so PayPal does the
authentication and regulatory disclosures. Nothing is executed from the first utterance, a stale question, or a
wake word. Data from PayPal (merchant names, notes) is only ever read out, never treated as a command.

## HTTP API

| Endpoint | Purpose |
| --- | --- |
| `GET /` | The demo app |
| `POST /voice/turn` `{session_id, text, alternatives[]}` | One spoken turn. Returns `speech`, `display`, `actions`, `expecting`, `state`, `api` (the PayPal calls made) and `suggestions` |
| `POST /voice/second-factor` `{session_id, token}` | Completes a money-moving action after an on-screen tap |
| `GET /paypal/return?sid=` | Where PayPal sends the user back after approving a payment |
| `GET /auth/callback` | Log in with PayPal callback |
| `GET /capabilities`, `GET /endpoints` | What the app can do now, and every PayPal endpoint reachable by voice |
| `GET /demo`, `POST /demo/reset`, `POST /voice/approve-sim` | Guided-demo script and simulator-only helpers |
| `GET /audit/verify` | Checks the audit log's hash chain |
| `GET /docs` | Interactive API reference |

## Tests

```bash
python -m pytest
```

239 tests run against the simulator. They cover number, email, phone and date parsing; more than 120 phrasings;
full conversations; the safety rules; request contracts; and one test that speaks the whole guided demo and
requires every endpoint to be called without an error. Real microphone input and audio output are not covered by
automated tests.

## Before production

- Run the suite against the PayPal Sandbox and get PayPal's approvals for Personal Payments, Log in with PayPal
  and Payouts.
- Tie `session_id` to a signed-in device or user; today the client chooses it.
- Store user tokens encrypted and persist sessions; both are in memory today.
- Add a webhook listener for spoken alerts (payment received, invoice paid, dispute opened).

## Add a voice command

Add one `Capability(...)` row in `paypal_voice/registry.py` with its phrases, the details it needs, its risk
tier, the PayPal operations it uses and a `run` function that returns a `Reply`. Add the operations to
`ENDPOINTS`, a phrase to `tools/demo.txt`, and run `python tools/build_catalog.py` to update coverage.

## License

MIT; see [LICENSE](LICENSE). The PayPal OpenAPI specifications in `specs/` are © PayPal under the Apache
License 2.0; see [specs/README.md](specs/README.md) and [NOTICE](NOTICE). PayPal is a trademark of PayPal, Inc.
