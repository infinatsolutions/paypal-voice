# What PayPal's APIs let a voice app do (research, 2026-11-08)

Sources were read directly: PayPal's official OpenAPI specs on GitHub (13 APIs, commit dated 2026-04-07), the
OpenAPI schemas and guides on developer.paypal.com (17 current API references plus the limited-release Personal
Payments pages), and PayPal's developer docs on accounts and sandbox. Full operation list: `API_CATALOG.md`.

## 1. There is a "personal" REST API, and it is limited release

**Personal Payments** is PayPal's consumer peer-to-peer API: send money to a PayPal member or non-member by email,
phone (E.164) or account number, as `PERSONAL` (friends and family) or `PURCHASE` (goods and services).

- **Access:** you must be approved. Request it at `p2papiapproval@paypal.com`.
- **v1** (OpenAPI schema v1.19, 13 operations): capabilities by country, eligibility, funding options (fees, sources,
  arrival time), create/execute payment, default pay, payment tokens for a PayPal redirect, authorizations
  (capture, void), refunds, payment details. Scopes include `wallet/sendmoney`,
  `wallet/financial-instruments/view` and `consumer/personal-payments-tokens/readwrite`.
- **v2** (guide only, 4 endpoints): `POST /v2/personal-payments/tokens` and `GET .../tokens/{id}` for the
  **PayPal UI** pattern, plus `evaluate-funding-options` and `payments` for the **partner UI** pattern.
- **PayPal recommends the PayPal UI pattern for everyone except large enterprises.** You create a token, send the
  user to PayPal's `initiate_payment` link with `&return_url=`, PayPal signs them in, shows regulatory screens,
  and returns `status`, `transactionId` and `returnCode`. Partner UI is for enterprises with in-house compliance
  teams; PayPal warns non-compliant UIs can be fined or shut down.
- **Fees:** friends-and-family is free from balance or bank, with fees for cards and credit; purchases always charge
  the payee; `fee.payer` is `PAYER` or `PAYEE` (`PURCHASE` + `PAYER` is not allowed).
- **Regulation:** international transfers over $15 trigger the Remittance Transfer Rule (the app must display
  PayPal's disclosures); unregistered foreign payees need name and country
  (`TRANSACTION_DECLINED_ADDITIONAL_PAYEE_INFO_REQUIRED`); US senders at $3,000+ hit the travel rule.
- **Never** use it when sender and receiver are the same person.

**Identity (Log in with PayPal)** is the sign-in half: authorization-code flow, `GET /v1/identity/openidconnect/userinfo`.
PayPal's OpenID configuration lists the scope `https://uri.paypal.com/services/wallet/sendmoney` among those it supports.

## 2. Everything else acts on the app owner's own business account

The 17 generally available APIs (Orders, Payments, Invoicing, Subscriptions, Disputes, Payouts, Transaction Search,
Payment Links, Currency Exchange, Tracking, Webhooks, Vault, Catalog, ...) authenticate with the app's client
credentials and act on the business account that owns the app. Going live requires a PayPal Business account.

- **Transaction Search** (balance and activity): "To use the API on behalf of third parties, you must be part of the
  PayPal partner network." Data lags up to 3 hours, covers 3 years, and each search spans at most 31 days.
- **Payouts** needs a business account with Payouts enabled.
- So a voice app can read *its owner's* balance and activity, but **cannot read a signed-in consumer's balance or
  history** without PayPal partner status.

## 3. Not available through any public API

Rewards and cashback, PayPal Savings, transfers to a bank, card controls, crypto, Pay Later status, and a consumer's
own activity feed. The backbone answers these honestly and hands off to the PayPal app.

## 4. What this means for PayPal Voice

| Who is talking | What voice can do | What it needs from PayPal |
| --- | --- | --- |
| A personal PayPal user | Sign in; send money or pay for goods (approved on PayPal's screen); check fees, countries and payment status | Personal Payments approval + Log in with PayPal |
| The business that owns the app | Balance, activity, find payments, invoices (send, remind, cancel), payment links, refunds, disputes, subscriptions, payouts, exchange rates | Nothing beyond a REST app (Payouts must be enabled) |
| Anyone | Rewards, savings, bank transfers, cards, crypto | Not offered by PayPal; honest handoff |

## Sources

- [PayPal REST API specifications (GitHub)](https://github.com/paypal/paypal-rest-api-specifications)
- [Current resources](https://developer.paypal.com/api/rest/current-resources/)
- [Personal Payments v2 integration guide](https://developer.paypal.com/limited-release/personal-payments-v2)
- [Personal Payments v1 integration guide](https://developer.paypal.com/limited-release/personal-payments)
- [Personal Payments v1 API reference](https://developer.paypal.com/api/limited-release/personal-payments/v1)
- [Transaction fees](https://developer.paypal.com/limited-release/personal-payments/who-pays-fees)
- [Regulatory requirements](https://developer.paypal.com/limited-release/personal-payments/rtr)
- [Send money to phone or account number](https://developer.paypal.com/limited-release/personal-payments/fulfill-mobile)
- [Transaction Search v1](https://developer.paypal.com/api/transaction-search/v1)
- [Identity v1](https://developer.paypal.com/api/identity/v1)
- [PayPal OpenID configuration](https://www.paypalobjects.com/.well-known/openid-configuration)
- [Sandbox accounts](https://developer.paypal.com/api/rest/sandbox/accounts)
- [Configure sandbox and live accounts](https://developer.paypal.com/platforms/create-account)
