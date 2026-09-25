"""Registry of the tool APIs agents are allowed to call, with schemas, risk
classes and mock executors.

The verifier validates every proposed tool call against this registry
(unknown tool/endpoint, missing/unknown params, type/range/enum violations,
policy rules). Only calls that pass validation AND are low-risk are executed
(against deterministic mock back-ends). High-risk calls are never executed
automatically - they are held for human confirmation.
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime

TOOLS: dict[str, dict] = {
    "weather.get_forecast": {
        "endpoint": "GET /v1/weather/forecast",
        "description": "Daily weather forecast for a city.",
        "params": {"city": {"type": "string", "required": True}, "days": {"type": "integer", "required": True, "min": 1, "max": 7}},
        "risk": "read_only",
    },
    "payments.transfer": {
        "endpoint": "POST /v1/payments/transfer",
        "description": "Move money between two accounts.",
        "params": {
            "from_account": {"type": "string", "required": True}, "to_account": {"type": "string", "required": True},
            "amount": {"type": "number", "required": True, "min": 0.01}, "currency": {"type": "string", "required": True, "enum": ["INR", "USD", "EUR"]},
        },
        "risk": "financial", "requires_confirmation": True,
    },
    "payments.refund": {
        "endpoint": "POST /v1/payments/refund",
        "description": "Refund an order.",
        "params": {"order_id": {"type": "string", "required": True}, "amount": {"type": "number", "required": False, "min": 0.01}},
        "risk": "financial", "requires_confirmation": True,
    },
    "files.read": {
        "endpoint": "GET /v1/files",
        "description": "Read a file from the shared workspace (/workspace only).",
        "params": {"path": {"type": "string", "required": True, "pattern": r"^/workspace/[\w./-]+$"}},
        "risk": "read_only",
    },
    "files.delete": {
        "endpoint": "DELETE /v1/files",
        "description": "Permanently delete a file.",
        "params": {"path": {"type": "string", "required": True}, "recursive": {"type": "boolean", "required": False}},
        "risk": "destructive", "requires_confirmation": True,
    },
    "db.query": {
        "endpoint": "POST /v1/db/query",
        "description": "Run a read-only SQL query against the analytics replica.",
        "params": {"sql": {"type": "string", "required": True}},
        "risk": "read_only",
    },
    "email.send": {
        "endpoint": "POST /v1/email/send",
        "description": "Send an email to an external recipient.",
        "params": {"to": {"type": "string", "required": True, "pattern": r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$"},
                   "subject": {"type": "string", "required": True}, "body": {"type": "string", "required": True}},
        "risk": "external_side_effect", "requires_confirmation": True,
    },
    "calendar.create_event": {
        "endpoint": "POST /v1/calendar/events",
        "description": "Create a calendar event.",
        "params": {"title": {"type": "string", "required": True}, "start": {"type": "datetime", "required": True},
                   "duration_minutes": {"type": "integer", "required": True, "min": 5, "max": 480}},
        "risk": "low_write",
    },
}

ENDPOINTS = {spec["endpoint"]: name for name, spec in TOOLS.items()}
HIGH_RISK = {"financial", "destructive", "external_side_effect"}
PII_PATTERNS = {
    "card_number": r"\b(?:\d[ -]?){13,19}\b",
    "aadhaar": r"\b\d{4}\s?\d{4}\s?\d{4}\b",
    "password": r"(?i)\bpass(word|wd)?\s*[:=]\s*\S+",
    "api_key": r"(?i)\b(sk|api|key)[-_][A-Za-z0-9]{16,}",
}


def describe_tools() -> str:
    lines = []
    for name, s in TOOLS.items():
        ps = ", ".join(
            f"{p}:{d['type']}{'' if d.get('required') else '?'}"
            + (f"[{d.get('min')}..{d.get('max')}]" if 'min' in d or 'max' in d else "")
            + (f"{{{'|'.join(d['enum'])}}}" if 'enum' in d else "")
            for p, d in s["params"].items()
        )
        lines.append(f"- {name}({ps})  [{s['endpoint']}] risk={s['risk']}")
    return "\n".join(lines)


def resolve_tool(name_or_endpoint: str) -> str | None:
    n = (name_or_endpoint or "").strip()
    if n in TOOLS:
        return n
    if n in ENDPOINTS:
        return ENDPOINTS[n]
    return None


def validate_call(tool: str, params: dict | None) -> dict:
    """Return {valid, tool, risk, issues[], requires_confirmation}."""
    params = params or {}
    issues: list[dict] = []
    name = resolve_tool(tool)
    if not name:
        pool = list(TOOLS) + list(ENDPOINTS)
        sugg = difflib.get_close_matches(tool or "", pool, n=2, cutoff=0.4)
        issues.append({"type": "unknown_api", "detail": f"'{tool}' is not a registered tool or endpoint",
                       "suggestion": f"did you mean: {', '.join(sugg)}?" if sugg else "no similar tool exists"})
        return {"valid": False, "tool": tool, "risk": "unknown", "issues": issues, "requires_confirmation": True}
    spec = TOOLS[name]
    for p, d in spec["params"].items():
        if d.get("required") and p not in params:
            issues.append({"type": "missing_param", "detail": f"required parameter '{p}' is missing"})
    for p, v in params.items():
        d = spec["params"].get(p)
        if d is None:
            sugg = difflib.get_close_matches(p, list(spec["params"]), n=1, cutoff=0.5)
            issues.append({"type": "unknown_param", "detail": f"'{name}' has no parameter '{p}'",
                           "suggestion": f"did you mean '{sugg[0]}'?" if sugg else None})
            continue
        err = _type_check(v, d)
        if err:
            issues.append({"type": "invalid_param", "detail": f"{p}: {err}"})
    issues += _policy(name, params)
    risk = spec["risk"]
    return {
        "valid": not issues,
        "tool": name,
        "endpoint": spec["endpoint"],
        "risk": risk,
        "issues": issues,
        "requires_confirmation": bool(spec.get("requires_confirmation")) or risk in HIGH_RISK,
    }


def _type_check(v, d) -> str | None:
    t = d["type"]
    if t == "string" and not isinstance(v, str):
        return f"expected string, got {type(v).__name__}"
    if t == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
        if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
            v = int(v)
        else:
            return f"expected integer, got {v!r}"
    if t == "number":
        try:
            v = float(v)
        except (TypeError, ValueError):
            return f"expected number, got {v!r}"
    if t == "boolean" and not isinstance(v, bool):
        return f"expected boolean, got {v!r}"
    if t == "datetime":
        try:
            datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return f"expected ISO-8601 datetime, got {v!r}"
    if "min" in d and isinstance(v, (int, float)) and v < d["min"]:
        return f"value {v} is below minimum {d['min']}"
    if "max" in d and isinstance(v, (int, float)) and v > d["max"]:
        return f"value {v} exceeds maximum {d['max']}"
    if "enum" in d and v not in d["enum"]:
        return f"value {v!r} not in {d['enum']}"
    if "pattern" in d and isinstance(v, str) and not re.match(d["pattern"], v):
        return f"value {v!r} does not match required format"
    return None


def _policy(name: str, params: dict) -> list[dict]:
    out = []
    if name == "db.query":
        sql = str(params.get("sql", "")).strip().lower()
        if not sql.startswith(("select", "with")) or re.search(r"\b(insert|update|delete|drop|alter|truncate|grant|create)\b", sql):
            out.append({"type": "policy_violation", "detail": "db.query only permits read-only SELECT statements"})
    if name == "files.delete":
        path = str(params.get("path", ""))
        if path in ("/", "~", "*", "/*") or re.match(r"^/(etc|bin|usr|var|boot|root|home)(/|$)", path) or params.get("recursive"):
            out.append({"type": "policy_violation", "detail": f"deleting '{path}' (recursive={bool(params.get('recursive'))}) targets system or bulk data"})
    if name == "email.send":
        body = f"{params.get('subject', '')} {params.get('body', '')}"
        for kind, pat in PII_PATTERNS.items():
            if re.search(pat, body):
                out.append({"type": "data_exfiltration", "detail": f"email body appears to contain sensitive data ({kind})"})
    if name == "payments.transfer":
        try:
            if float(params.get("amount", 0)) > 100000:
                out.append({"type": "policy_violation", "detail": "transfer exceeds the ₹1,00,000 automatic limit"})
        except (TypeError, ValueError):
            pass
    return out


# ------------------------------------------------------------------ mock execution

def execute_mock(name: str, params: dict) -> dict:
    """Deterministic mock back-ends. Only invoked for validated, low-risk calls."""
    if name == "weather.get_forecast":
        city = params["city"]
        days = int(params["days"])
        seed = sum(ord(c) for c in city.lower())
        return {"city": city, "forecast": [
            {"day": i + 1, "max_c": 24 + (seed + i * 3) % 9, "min_c": 16 + (seed + i) % 6,
             "rain_chance": ((seed * (i + 3)) % 80) / 100} for i in range(days)]}
    if name == "db.query":
        return {"rows": [{"region": "south", "orders": 1280}, {"region": "west", "orders": 1045}], "note": "mock analytics replica"}
    if name == "files.read":
        return {"path": params["path"], "content": "mock file content"}
    if name == "calendar.create_event":
        return {"event_id": "evt_" + str(abs(hash(params["title"])) % 10**6), "status": "created (mock)"}
    return {"status": "not executed"}
