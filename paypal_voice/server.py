"""HTTP surface for the voice backbone (FastAPI).

Run:  uvicorn paypal_voice.server:app --reload
Any speech front end (browser, phone app, smart speaker) sends the recognized text to /voice/turn
and speaks back `speech`; `display` and `actions` drive the screen.
"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from .app_factory import build_engine
from .engine import Audit
from .paypal_client import PayPalError, Token
from .registry import CAPABILITIES, ENDPOINTS

BUILD = "hackathon-1"
engine, mock = build_engine()
app = FastAPI(title="PayPal Voice backbone", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://localhost:8080").split(","),
                   allow_methods=["GET", "POST"], allow_headers=["*"])


class TurnIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    text: str = Field(max_length=500)
    alternatives: list[str] = Field(default_factory=list, max_length=5)


class FactorIn(BaseModel):
    session_id: str
    token: str


@app.post("/voice/turn")
async def voice_turn(body: TurnIn) -> dict:
    if mock:  # simulator: every session is "signed in" so personal capabilities can be tried
        s = engine.session(body.session_id)
        if not s.user_token:
            s.user_token = Token("A21AAmock-user", time.time() + 3600)
    out = (await engine.handle_turn(body.session_id, body.text, body.alternatives)).to_dict()
    if mock:  # the simulator has no PayPal approval page, so the app shows a stand-in approve button
        for a in out["actions"]:
            if a.get("purpose") == "approve_payment":
                a["simulated"] = True
    return out


class SessionIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)


@app.post("/voice/approve-sim")
async def approve_sim(body: SessionIn) -> dict:
    """Simulator only: stands in for the user approving a personal payment on PayPal's own screen."""
    if not mock:
        raise HTTPException(404, "only available on the simulator")
    tok = engine.session(body.session_id).pending_token
    if not tok:
        return (await engine.paypal_return(body.session_id, {"status": "none"})).to_dict()
    txn = mock.approve_token(tok["id"])
    return (await engine.paypal_return(body.session_id, {"status": "success", "transactionId": txn})).to_dict()


@app.post("/demo/reset")
def demo_reset() -> dict:
    """Simulator only: put the fake PayPal account back to its starting state."""
    global engine, mock
    if not mock:
        raise HTTPException(404, "only available on the simulator")
    engine, mock = build_engine()
    return {"ok": True}


@app.get("/demo")
def demo_script() -> list[dict]:
    """The guided demo: sections and the phrases to say, from tools/demo.txt."""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "demo.txt")
    out: list[dict] = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8").read().splitlines():
            line = line.strip()
            if line.startswith("# ---"):
                out.append({"section": line.lstrip("#- ").strip().capitalize()})
            elif line and not line.startswith("#"):
                out.append({"say": line})
    return out


@app.get("/endpoints")
def endpoints() -> list[dict]:
    """Every PayPal REST endpoint reachable by voice, with the command that reaches it."""
    caps = {c["name"]: c for c in capabilities()}
    out = []
    for op, (method, path, label) in ENDPOINTS.items():
        owner = next((c for c in CAPABILITIES if op in c.ops), None)
        out.append({"op": op, "method": method, "path": path, "label": label, "capability": owner.name if owner else "",
                    "say": owner.examples[0] if owner else "", "tier": owner.tier if owner else "",
                    "status": caps[owner.name]["status"] if owner else "unused"})
    return out


@app.post("/voice/second-factor")
async def second_factor(body: FactorIn) -> dict:
    return (await engine.second_factor(body.session_id, body.token)).to_dict()


@app.get("/paypal/return")
async def paypal_return(sid: str, status: str = "", transactionId: str = "", returnCode: str = "") -> dict:
    return (await engine.paypal_return(sid, {"status": status, "transactionId": transactionId, "returnCode": returnCode})).to_dict()


@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(code: str = "", state: str = "") -> str:
    if not code or not state:
        raise HTTPException(400, "missing code or state")
    try:
        r = await engine.complete_sign_in(state, code)
    except PayPalError as e:
        return f"<p>Sign-in failed: {e.name}. Close this tab and try again.</p>"
    return f"<p>{r.speech}</p><p>You can close this tab and return to PayPal Voice.</p>"


@app.get("/capabilities")
def capabilities() -> list[dict]:
    c = engine.config
    def usable(cap):
        if cap.availability == "NOT_AVAILABLE":
            return "handoff"
        if cap.mode == "OWNER" and c.account_mode == "PERSONAL":
            return "unavailable_in_mode"
        if cap.mode == "PERSONAL" and (c.account_mode == "OWNER" or not c.personal_payments_enabled and cap.name != "whoami"):
            return "needs_approval" if cap.availability == "LIMITED_RELEASE" else "unavailable_in_mode"
        if cap.availability == "NEEDS_APPROVAL" and not c.payouts_enabled:
            return "needs_approval"
        return "ready"
    return [{"name": x.name, "summary": x.summary, "examples": x.examples, "mode": x.mode, "availability": x.availability,
             "tier": x.tier, "paypal_operations": x.ops, "status": usable(x)} for x in CAPABILITIES]


@app.get("/audit/verify")
def audit_verify() -> dict:
    return {"entries": len(engine.audit.memory), "chain_valid": Audit.verify(engine.audit.memory)}


APP_PAGE = os.path.join(os.path.dirname(__file__), "static", "app.html")
NO_STORE = {"Cache-Control": "no-store, max-age=0"}

# An earlier PayPal Voice build installed a cache-first service worker on http://localhost:8000. If this server is
# ever run on that address, this replacement worker deletes those caches, unregisters itself and reloads the page.
KILL_SWITCH_SW = """self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil((async () => {
  for (const k of await caches.keys()) await caches.delete(k);
  await self.registration.unregister();
  for (const c of await self.clients.matchAll({ type: 'window' })) c.navigate(c.url);
})()));
"""


@app.get("/", include_in_schema=False)
def app_page() -> FileResponse:
    """The PayPal Voice demo app: speech in (Web Speech API), speech out (speechSynthesis), live API trace."""
    return FileResponse(APP_PAGE, media_type="text/html", headers=NO_STORE)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/sw.js", include_in_schema=False)
def service_worker_kill_switch() -> Response:
    return Response(KILL_SWITCH_SW, media_type="text/javascript", headers=NO_STORE)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "env": engine.config.env, "account_mode": engine.config.account_mode, "simulator": bool(mock),
            "capabilities": len(CAPABILITIES), "endpoints": len(ENDPOINTS), "build": BUILD}
