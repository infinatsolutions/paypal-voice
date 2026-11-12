"""Build a ready-to-use engine from environment variables."""
from __future__ import annotations

import os
from decimal import Decimal

from .engine import Config, Contacts, VoiceEngine
from .mock_paypal import NOW, MockPayPal
from .paypal_client import PayPalClient

DEMO_CONTACTS = {
    "sam": {"id": "sam@example.com", "type": "EMAIL", "name": "Sam Rivera"},
    "maya": {"id": "maya@example.com", "type": "EMAIL", "name": "Maya Chen"},
    "jordan": {"id": "jordan@example.com", "type": "EMAIL", "name": "Jordan Lee"},
    "priya": {"id": "priya@example.com", "type": "EMAIL", "name": "Priya Nair"},
    "my mom": {"id": "+16145550142", "type": "PHONE", "name": "Mom", "aliases": ["mom", "mum", "mother"]},
}


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def build_engine(env: str | None = None, audit_path: str | None = None):
    _load_dotenv()
    env = env or os.environ.get("PAYPAL_ENV", "mock")
    mock = MockPayPal() if env == "mock" else None
    client = PayPalClient(env, os.environ.get("PAYPAL_CLIENT_ID", "mock-id" if mock else ""),
                          os.environ.get("PAYPAL_CLIENT_SECRET", "mock-secret" if mock else ""),
                          transport=mock.transport() if mock else None)
    cfg = Config(
        env=env,
        account_mode=os.environ.get("ACCOUNT_MODE", "BOTH"),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"),
        home_country=os.environ.get("HOME_COUNTRY", "US"),
        personal_payments_enabled=_flag("PERSONAL_PAYMENTS_ENABLED", env == "mock"),
        login_enabled=_flag("LOGIN_WITH_PAYPAL_ENABLED", True),
        payouts_enabled=_flag("PAYOUTS_ENABLED", env == "mock"),
        second_factor=_flag("SECOND_FACTOR", False),
        max_per_action=Decimal(os.environ.get("MAX_PER_ACTION", "500")),
        daily_limit=Decimal(os.environ.get("DAILY_LIMIT", "1000")),
        audit_path=audit_path if audit_path is not None else os.environ.get("AUDIT_PATH", "audit.jsonl"),
        contacts_path=os.environ.get("CONTACTS_PATH", "contacts.json"),
    )
    contacts = Contacts(None if mock else cfg.contacts_path, seed=DEMO_CONTACTS if mock else None)
    now = (lambda: NOW) if mock else None
    return VoiceEngine(client, cfg, contacts, now=now), mock
