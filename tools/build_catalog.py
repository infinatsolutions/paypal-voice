"""Build research/catalog.json and research/API_CATALOG.md: every PayPal REST operation found,
who can call it, how risky it is by voice, and which voice capability covers it."""
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paypal_voice.registry import CAPABILITIES  # noqa: E402

T2_WORDS = re.compile(r"refund|capture|payout|accept-claim|accept-offer|cancel|delete|void|personal-payments\.post|personal-payments\.pay|"
                      r"tokens\.create|send|adjudicate|escalate|authorize|reauthorize|suspend|deactivate|make-offer|subscriptions\.create")
COVER = defaultdict(list)
for c in CAPABILITIES:
    for op in c.ops:
        COVER[op].append(c.name)

GITHUB_AVAIL = {
    "reporting_transactions_v1": ("GA_OWN_ACCOUNT", "Own account; third parties need the PayPal partner network"),
    "customer_partner_referrals_v2": ("PARTNER_ONLY", "Platforms onboarding sellers"),
    "payments_payouts_batch_v1": ("NEEDS_APPROVAL", "Business account with Payouts enabled"),
    "vault_payment_tokens_v3": ("GA_OWN_ACCOUNT", "Merchant vaulting for its customers"),
    "payment-experience_web_experience_profiles_v1": ("LEGACY", "Profiles for the v1 Payments API"),
}


SITE_TITLE = {"transaction-search": "Transaction Search", "invoicing": "Invoices", "orders": "Orders", "subscriptions": "Subscriptions",
              "payment-links-buttons": "Payment Links and Buttons", "pricing": "Currency Exchange", "identity": "Identity",
              "usage-based-billing": "Usage Based Billing"}


def tier(method, op):
    if method == "GET":
        return "T0"
    return "T2" if T2_WORDS.search(op) else "T1"


def main():
    rows = []
    for f in sorted((ROOT / "specs").glob("*.json")):
        spec = json.loads(f.read_text())
        avail, note = GITHUB_AVAIL.get(f.stem, ("GA_OWN_ACCOUNT", "Acts on the API caller's own business account"))
        for path, item in spec["paths"].items():
            for m, o in item.items():
                if m not in ("get", "post", "put", "patch", "delete"):
                    continue
                scopes = sorted({s.replace("https://uri.paypal.com/services/", "").replace("https://uri.paypal.com/", "")
                                 for sec in o.get("security", spec.get("security", [])) for v in sec.values() for s in v})
                rows.append({"api": spec["info"]["title"], "version": f"GitHub v{spec['info']['version']}", "source": "github-spec", "method": m.upper(),
                             "path": path, "operation": o.get("operationId", ""), "summary": o.get("summary", ""), "scopes": scopes,
                             "availability": avail, "who": note, "voice_tier": tier(m.upper(), o.get("operationId", "")),
                             "voice": COVER.get(o.get("operationId", ""), [])})
    extras = json.loads((ROOT / "research/site_extras.json").read_text())
    for api, d in extras["apis"].items():
        title = SITE_TITLE[api.split("/")[0]]
        ver = re.search(r"site (v[\d.]+)", api).group(1)
        for m, p, op, summ, scope, params in d["ops"]:
            rows.append({"api": title, "version": f"site {ver}", "source": "developer-site", "method": m, "path": p, "operation": op, "summary": summ,
                         "scopes": [scope] if scope else [], "availability": d["availability"].replace("LIMITED_RELEASE", "LIMITED_RELEASE"),
                         "who": "Signed-in PayPal user (Log in with PayPal)" if "identity" in api else "Acts on the API caller's own business account",
                         "voice_tier": tier(m, op), "voice": COVER.get(op, [])})
    pp = json.loads((ROOT / "research/personal_payments.json").read_text())
    for o in pp["v1"]["ops"]:
        rows.append({"api": "Personal Payments v1 (limited release)", "version": "site v1.19", "source": "developer-site", "method": o["m"], "path": o["p"],
                     "operation": o["op"], "summary": o.get("note", ""), "scopes": [x.strip() for x in o.get("scope", "").split("+") if x.strip()],
                     "availability": "LIMITED_RELEASE", "who": "Signed-in personal PayPal user; app approved by PayPal",
                     "voice_tier": tier(o["m"], o["op"]), "voice": COVER.get(o["op"], [])})
    for o in pp["v2"]["ops"]:
        rows.append({"api": "Personal Payments v2 (limited release)", "version": "guide only", "source": "developer-site-guide", "method": o["m"], "path": o["p"],
                     "operation": o["op"], "summary": o["pattern"] + " pattern", "scopes": [], "availability": "LIMITED_RELEASE",
                     "who": "Signed-in personal PayPal user; app approved by PayPal (p2papiapproval@paypal.com)",
                     "voice_tier": tier(o["m"], o["op"]), "voice": COVER.get(o["op"], [])})
    (ROOT / "research/catalog.json").write_text(json.dumps(rows, indent=1))

    by_api = defaultdict(list)
    for r in rows:
        by_api[r["api"]].append(r)
    out = ["# PayPal REST API catalog for PayPal Voice", "",
           f"{len(rows)} operations across {len(by_api)} APIs, generated by `tools/build_catalog.py` from PayPal's official "
           "OpenAPI specs (GitHub snapshot in `specs/`) plus operations published only on developer.paypal.com "
           "(`research/site_extras.json`, `research/personal_payments.json`). Usage-Based Billing (75 merchant metering "
           "operations) is excluded as out of scope.", "",
           "Voice tier: T0 read, runs immediately; T1 reversible write, spoken yes; T2 money or irreversible, read-back plus yes "
           "in a later turn (plus a second factor if enabled).", "",
           "| API | Versions read | Ops | Voice-covered | Availability | Who it acts for |", "| --- | --- | --- | --- | --- | --- |"]
    for api, rs in sorted(by_api.items(), key=lambda kv: (kv[1][0]["availability"] != "LIMITED_RELEASE", kv[0])):
        vers = " / ".join(sorted({r["version"] for r in rs}))
        out.append(f"| {api} | {vers} | {len(rs)} | {sum(1 for r in rs if r['voice'])} | {rs[0]['availability']} | {rs[0]['who']} |")
    out += ["", f"Total voice-covered operations: {sum(1 for r in rows if r['voice'])} of {len(rows)}.", ""]
    for api, rs in sorted(by_api.items(), key=lambda kv: (kv[1][0]["availability"] != "LIMITED_RELEASE", kv[0])):
        out += [f"## {api}", "", "| Method | Path | Operation | Tier | Voice capability | Scopes |", "| --- | --- | --- | --- | --- | --- |"]
        for r in rs:
            out.append(f"| {r['method']} | `{r['path']}` | {r['operation']} | {r['voice_tier']} | {', '.join(r['voice']) or '—'} | {', '.join(r['scopes']) or '—'} |")
        out.append("")
    (ROOT / "research/API_CATALOG.md").write_text("\n".join(out))
    print(f"{len(rows)} operations, {len(by_api)} APIs, {sum(1 for r in rows if r['voice'])} voice-covered")


if __name__ == "__main__":
    main()
