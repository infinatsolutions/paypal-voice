"""Thin, safe PayPal REST client: OAuth, idempotency, retries, typed errors.

Two kinds of tokens:
* app token  - client-credentials grant; acts on the API caller's own account (OWNER mode)
* user token - from Log in with PayPal (authorization-code grant); acts for a signed-in
  PayPal user (PERSONAL mode, e.g. Personal Payments, Identity userinfo)
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

# Calls made while handling the current voice turn (set by the engine; safe across concurrent sessions).
TRACE: contextvars.ContextVar[list | None] = contextvars.ContextVar("paypal_trace", default=None)

BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
    "mock": "https://mock.paypal.local",
}
WEB_URLS = {
    "sandbox": "https://www.sandbox.paypal.com",
    "live": "https://www.paypal.com",
    "mock": "https://mock.paypal.local",
}


class PayPalError(Exception):
    def __init__(self, status: int, name: str, message: str, debug_id: str | None = None,
                 details: list | None = None, op: str | None = None):
        super().__init__(f"{status} {name}: {message}")
        self.status, self.name, self.message = status, name, message
        self.debug_id, self.details, self.op = debug_id, details or [], op

    @property
    def issues(self) -> list[str]:
        return [d.get("issue", "") for d in self.details if isinstance(d, dict)]


@dataclass
class Token:
    value: str
    expires_at: float
    scopes: str = ""
    refresh_token: str | None = None

    @property
    def fresh(self) -> bool:
        return time.time() < self.expires_at - 60


class PayPalClient:
    def __init__(self, env: str, client_id: str = "", client_secret: str = "",
                 transport: httpx.AsyncBaseTransport | None = None, max_retries: int = 2):
        if env not in BASE_URLS:
            raise ValueError(f"env must be one of {list(BASE_URLS)}")
        self.env = env
        self.base = BASE_URLS[env]
        self.web = WEB_URLS[env]
        self._id, self._secret = client_id, client_secret
        self._app_token: Token | None = None
        self._lock = asyncio.Lock()
        self.max_retries = max_retries
        self.http = httpx.AsyncClient(base_url=self.base, transport=transport, timeout=30.0)
        self.calls: list[dict] = []        # request log (no bodies, no secrets) for diagnostics

    # ------------------------------------------------------------ auth
    def _basic(self) -> str:
        return "Basic " + base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()

    async def app_token(self) -> str:
        async with self._lock:
            if self._app_token and self._app_token.fresh:
                return self._app_token.value
            r = await self.http.post("/v1/oauth2/token", data={"grant_type": "client_credentials"},
                                     headers={"Authorization": self._basic(), "Accept": "application/json"})
            if r.status_code != 200:
                raise self._error(r, "oauth2.token")
            j = r.json()
            self._app_token = Token(j["access_token"], time.time() + int(j.get("expires_in", 3600)), j.get("scope", ""))
            return self._app_token.value

    async def exchange_code(self, code: str) -> Token:
        """Log in with PayPal: authorization code -> user token."""
        r = await self.http.post("/v1/oauth2/token", data={"grant_type": "authorization_code", "code": code},
                                 headers={"Authorization": self._basic(), "Accept": "application/json"})
        if r.status_code != 200:
            raise self._error(r, "oauth2.token(authorization_code)")
        j = r.json()
        return Token(j["access_token"], time.time() + int(j.get("expires_in", 3600)), j.get("scope", ""), j.get("refresh_token"))

    async def refresh(self, tok: Token) -> Token:
        r = await self.http.post("/v1/oauth2/token", data={"grant_type": "refresh_token", "refresh_token": tok.refresh_token or ""},
                                 headers={"Authorization": self._basic(), "Accept": "application/json"})
        if r.status_code != 200:
            raise self._error(r, "oauth2.token(refresh_token)")
        j = r.json()
        return Token(j["access_token"], time.time() + int(j.get("expires_in", 3600)), j.get("scope", tok.scopes), tok.refresh_token)

    def login_url(self, redirect_uri: str, state: str, scopes: str = "openid profile email") -> str:
        from urllib.parse import urlencode
        q = urlencode({"flowEntry": "static", "client_id": self._id, "response_type": "code",
                       "scope": scopes, "redirect_uri": redirect_uri, "state": state})
        return f"{self.web}/signin/authorize?{q}"

    # ------------------------------------------------------------ requests
    async def request(self, method: str, path: str, *, op: str, json: Any = None, params: dict | None = None,
                      user_token: Token | None = None, idempotency_key: str | None = None,
                      extra_headers: dict | None = None) -> Any:
        method = method.upper()
        token = user_token.value if user_token else await self.app_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                   "Content-Type": "application/json", "Prefer": "return=representation"}
        if method == "POST":
            headers["PayPal-Request-Id"] = idempotency_key or str(uuid.uuid4())
        if extra_headers:
            headers.update(extra_headers)
        retryable = method in ("GET", "PUT", "DELETE") or "PayPal-Request-Id" in headers
        attempt = 0
        while True:
            t0 = time.time()
            try:
                r = await self.http.request(method, path, json=json, params=_clean(params), headers=headers)
            except httpx.TransportError as e:
                if retryable and attempt < self.max_retries:
                    attempt += 1
                    await asyncio.sleep(0.4 * 2 ** attempt)
                    continue
                raise PayPalError(0, "NETWORK_ERROR", str(e), op=op) from e
            rec = {"op": op, "method": method, "path": path, "status": r.status_code,
                   "ms": int((time.time() - t0) * 1000), "debug_id": r.headers.get("paypal-debug-id")}
            self.calls.append(rec)
            if (trace := TRACE.get()) is not None:
                trace.append(rec)
            if r.status_code in (429, 500, 502, 503, 504) and retryable and attempt < self.max_retries:
                attempt += 1
                delay = float(r.headers.get("retry-after", 0) or 0.4 * 2 ** attempt)
                await asyncio.sleep(min(delay, 5))
                continue
            if r.status_code >= 400:
                raise self._error(r, op)
            if r.status_code == 204 or not r.content:
                return {}
            return r.json()

    @staticmethod
    def _error(r: httpx.Response, op: str) -> PayPalError:
        try:
            j = r.json()
        except ValueError:
            j = {}
        name = j.get("name") or j.get("error") or f"HTTP_{r.status_code}"
        msg = j.get("message") or j.get("error_description") or r.reason_phrase
        return PayPalError(r.status_code, name, msg, j.get("debug_id") or r.headers.get("paypal-debug-id"),
                           j.get("details"), op)

    async def aclose(self) -> None:
        await self.http.aclose()


def _clean(params: dict | None) -> dict | None:
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None and v != ""}
