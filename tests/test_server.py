import os

os.environ["AUDIT_PATH"] = ""
os.environ["PAYPAL_ENV"] = "mock"

from fastapi.testclient import TestClient  # noqa: E402

from paypal_voice.server import app  # noqa: E402

c = TestClient(app)


def test_turn_round_trip_and_confirmation():
    sid = "web-session-0001"
    r = c.post("/voice/turn", json={"session_id": sid, "text": "refund sam"}).json()
    assert r["expecting"] == "confirm" and r["capability"] == "refund"
    r = c.post("/voice/turn", json={"session_id": sid, "text": "yes"}).json()
    assert r["speech"] == "Refunded $40 to Sam Rivera."
    v = c.get("/audit/verify").json()
    assert v["entries"] >= 1 and v["chain_valid"]


def test_n_best_alternatives_are_used():
    r = c.post("/voice/turn", json={"session_id": "web-session-0002", "text": "what's my pal ants",
                                    "alternatives": ["what's my balance"]}).json()
    assert r["capability"] == "balance"


def test_capabilities_report_status():
    caps = {x["name"]: x for x in c.get("/capabilities").json()}
    assert caps["balance"]["status"] == "ready"
    assert caps["rewards"]["status"] == "handoff"
    assert caps["send_money"]["availability"] == "LIMITED_RELEASE"


def test_input_validation():
    assert c.post("/voice/turn", json={"session_id": "x", "text": "hi"}).status_code == 422
    assert c.get("/auth/callback").status_code == 400


def test_app_page_served_uncached():
    r = c.get("/")
    assert r.status_code == 200 and "PayPal Voice" in r.text and "/voice/turn" in r.text
    assert "no-store" in r.headers["cache-control"]


def test_stale_service_worker_kill_switch():
    r = c.get("/sw.js")
    assert r.status_code == 200 and "unregister" in r.text and "caches.delete" in r.text


def test_mock_session_is_signed_in_for_personal_capabilities():
    r = c.post("/voice/turn", json={"session_id": "console-abc123", "text": "who am i signed in as"}).json()
    assert r["state"] != "error" and "sign in" not in r["speech"].lower()


def test_at_least_25_endpoints_each_reachable_by_a_voice_command():
    eps = c.get("/endpoints").json()
    assert len(eps) >= 25
    assert all(e["capability"] and e["say"] and e["method"] in ("GET", "POST") and e["path"].startswith("/v") for e in eps)
    assert len({(e["method"], e["path"]) for e in eps}) == len(eps)


def test_turn_reports_the_paypal_calls_it_made():
    r = c.post("/voice/turn", json={"session_id": "trace-0001", "text": "What's my balance?"}).json()
    assert [(a["op"], a["method"], a["status"]) for a in r["api"]] == [("balances.get", "GET", 200)]
    assert r["heard"] == "what's my balance"


def test_unknown_request_suggests_close_commands():
    r = c.post("/voice/turn", json={"session_id": "trace-0002", "text": "purple monkey invoice dishwasher"}).json()
    assert r["state"] == "idle" and r["suggestions"] and not r["api"]


def test_guided_demo_calls_every_endpoint_without_errors():
    """The whole demo script, spoken in order in one session, reaches every registered PayPal endpoint."""
    c.post("/demo/reset")
    sid, seen = "demo-coverage-01", set()
    steps = c.get("/demo").json()
    assert sum(1 for s in steps if "say" in s) >= 40
    for step in steps:
        if "say" not in step:
            continue
        r = c.post("/voice/turn", json={"session_id": sid, "text": step["say"]}).json()
        assert r["state"] != "error", (step, r["speech"])
        assert "not sure" not in r["speech"], (step, r["speech"])
        assert all(a["status"] < 400 for a in r["api"]), (step, r["api"])
        seen.update(a["op"] for a in r["api"])
        if any(a.get("simulated") for a in r["actions"]):
            done = c.post("/voice/approve-sim", json={"session_id": sid}).json()
            assert done["speech"].startswith("Done."), done
    eps = {e["op"] for e in c.get("/endpoints").json()}
    assert seen == eps, sorted(eps - seen)
    c.post("/demo/reset")


def test_speech_recognizer_style_transcripts():
    """Browsers capitalize, punctuate and write $ amounts; all of that must still route."""
    cases = [("What's my balance?", "balance"), ("Send Sam $20 for coffee.", "send_money"), ("Bill Maya $45 for a framed print", "invoice_create"),
             ("Who owes me money?", "invoices"), ("Pause Maya's subscription.", "subscription_pause"), ("How much is $100 in euros?", "exchange_rate"),
             ("Did Jordan pay me?", "find_payment"), ("Hey PayPal, any disputes?", "disputes"), ("What do I sell?", "products"),
             ("Refund the last payment.", "refund"), ("Create an invoice for Sam for 60 dollars", "invoice_create")]
    for i, (text, want) in enumerate(cases):
        r = c.post("/voice/turn", json={"session_id": f"asr-style-{i:03d}", "text": text}).json()
        assert r["capability"] == want and r["state"] != "error", (text, r)
    r = c.post("/voice/turn", json={"session_id": "asr-style-last", "text": "Refund the last payment."}).json()
    assert "Sam Rivera" in r["speech"]            # newest payment, not the oldest
