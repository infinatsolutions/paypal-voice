"""Validate the request bodies the backbone builds against PayPal's official OpenAPI specs.

Specs live in ../specs (copied from github.com/paypal/paypal-rest-api-specifications).
Personal Payments has no public GitHub spec; its request schemas are written by hand
from the developer-site schema and guide (research/personal_payments.json).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC_DIR = Path(__file__).resolve().parent.parent / "specs"

_money_v1 = {"type": "object", "required": ["currency", "value"],
             "properties": {"currency": {"type": "string", "minLength": 3, "maxLength": 3},
                            "value": {"type": "string", "pattern": r"^\d{1,10}(\.\d{1,2})?$"}}}
_money_v2 = {"type": "object", "required": ["currency_code", "value"],
             "properties": {"currency_code": {"type": "string", "minLength": 3, "maxLength": 3},
                            "value": {"type": "string", "pattern": r"^\d{1,10}(\.\d{1,2})?$"}}}
_payee_v1 = {"type": "object", "required": ["id", "type"],
             "properties": {"id": {"type": "string", "minLength": 1},
                            "type": {"enum": ["EMAIL", "PHONE", "ACCOUNT_NUMBER", "EXTERNAL_IDENTIFIER"]},
                            "name": {"type": "string"}, "country_code": {"type": "string", "minLength": 2, "maxLength": 2}}}
_pp_options = {"type": "object", "required": ["amount", "payee", "payment_type"],
               "properties": {"amount": _money_v1, "payee": _payee_v1,
                              "intent": {"enum": ["PAYMENT", "AUTHORIZATION"]},
                              "payment_type": {"enum": ["PERSONAL", "PURCHASE"]},
                              "note_to_payee": {"type": "string", "maxLength": 1000},
                              "fee": {"type": "object", "properties": {"payer": {"enum": ["PAYER", "PAYEE"]}}}}}

HAND_SCHEMAS: dict[tuple[str, str], dict] = {
    ("POST", "/v1/payments/personal-payments/funding-options"): _pp_options,
    ("POST", "/v1/payments/personal-payments/pay"): _pp_options,
    ("POST", "/v1/payments/personal-payment-tokens"): _pp_options,
    ("POST", "/v1/payments/personal-payments"): {"type": "object", "required": ["funding_option_id"],
        "properties": {"funding_option_id": {"type": "string", "minLength": 1}, "note_to_payee": {"type": "string"}}},
    ("POST", "/v1/payments/personal-payments/assess-capabilities"): {"type": "object", "required": ["payer_country_code"],
        "properties": {"payer_country_code": {"type": "string", "minLength": 2, "maxLength": 2},
                       "payee_country_codes": {"type": "array", "items": {"type": "string"}}}},
    ("POST", "/v1/payments/refunds"): {"type": "object", "required": ["transaction_id", "amount"],
        "properties": {"transaction_id": {"type": "string"}, "amount": _money_v2}},
    ("POST", "/v2/personal-payments/tokens"): {"type": "object", "required": ["receiver", "amount", "payment_type", "intent"],
        "properties": {"receiver": {"type": "object", "required": ["id", "type"],
                                    "properties": {"id": {"type": "string"}, "type": {"enum": ["EMAIL", "PHONE", "ACCOUNT_NUMBER"]}}},
                       "amount": _money_v2, "payment_type": {"enum": ["PERSONAL", "PURCHASE"]},
                       "intent": {"const": "CAPTURE"}, "note_to_receiver": {"type": "string"},
                       "external_reference_id": {"type": "string"}}},
    ("POST", "/v1/checkout/payment-resources"): {"type": "object", "required": ["integration_mode", "type"],
        "properties": {"integration_mode": {"const": "LINK"}, "type": {"const": "BUY_NOW"},
                       "reusable": {"const": "MULTIPLE"}, "line_items": {"type": "array"}}},
    ("POST", "/v2/pricing/quote-exchange-rates"): {"type": "object", "required": ["quote_items"],
        "properties": {"quote_items": {"type": "array", "minItems": 1}}},
}


@lru_cache(maxsize=None)
def _specs() -> list[tuple[dict, Registry, str]]:
    out = []
    for f in sorted(SPEC_DIR.glob("*.json")):
        spec = json.loads(f.read_text())
        uri = f"urn:paypal:{f.stem}"
        reg = Registry().with_resource(uri, Resource.from_contents(spec, default_specification=DRAFT202012))
        out.append((spec, reg, uri))
    return out


def _path_regex(template: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\{[^/]+\}", "[^/]+", template) + "$")


@lru_cache(maxsize=None)
def find_operation(method: str, path: str) -> tuple[str, dict, Registry, str, str] | None:
    """Return (operationId, operation, registry, uri, path_template) for a concrete request path."""
    method = method.lower()
    for spec, reg, uri in _specs():
        for template, item in spec.get("paths", {}).items():
            if method in item and _path_regex(template).match(path):
                return item[method].get("operationId", ""), item[method], reg, uri, template
    return None


def validate_request(method: str, path: str, body: dict | None) -> list[str]:
    """Return a list of contract violations (empty list = valid or no schema known)."""
    path = path.split("?")[0]
    for (m, tmpl), schema in HAND_SCHEMAS.items():
        if m == method.upper() and _path_regex(tmpl).match(path):
            v = Draft202012Validator(schema)
            return [f"{'/'.join(map(str, e.path)) or '<body>'}: {e.message}" for e in v.iter_errors(body or {})]
    found = find_operation(method, path)
    if not found or body is None:
        return []
    _, op, reg, uri, _ = found
    rb = op.get("requestBody")
    if not rb:
        return []
    if "$ref" in rb:
        rb = reg.resolver(base_uri=uri).lookup(rb["$ref"]).contents
    schema = rb.get("content", {}).get("application/json", {}).get("schema")
    if not schema:
        return []
    v = Draft202012Validator({"$ref": f"{uri}#" + schema["$ref"][1:]} if "$ref" in schema else schema, registry=reg)
    return [f"{'/'.join(map(str, e.path)) or '<body>'}: {e.message}" for e in v.iter_errors(body)]


def operation_count() -> int:
    return sum(1 for spec, _, _ in _specs() for item in spec["paths"].values()
               for m in item if m in ("get", "post", "put", "patch", "delete"))
