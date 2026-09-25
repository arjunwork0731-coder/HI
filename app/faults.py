"""Controlled fault injection ("red team the generator").

When enabled, the first draft is corrupted with realistic generator
failures. Because we know exactly which faults were planted, we can measure
the verifier's detection rate on the real pipeline, independently of how
good the generator is.
"""
from __future__ import annotations

import copy
import re

from .textutil import extract_quantities


def _bump_quantity(text: str) -> tuple[str, str, str] | None:
    for q in extract_quantities(text):
        raw = q.raw
        if q.kind == "date" and re.search(r"\d{4}", raw):
            new = re.sub(r"\b(\d{1,2})\b", lambda m: str(int(m.group(1)) % 28 + 1), raw, count=1)
        else:
            m = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
            if not m:
                continue
            num = float(m.group(0).replace(",", ""))
            bumped = num * 1.13 + (1 if num < 10 else 0)
            s = f"{bumped:,.0f}" if "." not in m.group(0) else f"{bumped:,.2f}"
            if "," not in m.group(0):
                s = s.replace(",", "")
            new = raw.replace(m.group(0), s, 1)
        if new != raw and raw in text:
            return text.replace(raw, new, 1), raw, new
    return None


def inject_faults(draft: dict) -> tuple[dict, list[dict]]:
    d = copy.deepcopy(draft)
    faults: list[dict] = []
    # 1. numeric/date hallucination in a factual or calculation claim
    for c in d["claims"]:
        if c["type"] in ("fact", "calculation"):
            if c["type"] == "calculation" and c.get("result"):
                try:
                    wrong = round(float(c["result"].replace(",", "")) * 1.1 + 1, 2)
                except ValueError:
                    continue
                old = c["result"]
                c["text"] = c["text"].replace(old, str(wrong))
                d["answer"] = d["answer"].replace(old, str(wrong))
                c["result"] = str(wrong)
                faults.append({"type": "calculation_error", "claim_id": c["id"], "detail": f"result {old} -> {wrong}"})
                break
            r = _bump_quantity(c["text"])
            if r:
                new_text, old, new = r
                d["answer"] = d["answer"].replace(c["text"], new_text) if c["text"] in d["answer"] else d["answer"].replace(old, new, 1)
                c["text"] = new_text
                faults.append({"type": "numeric_hallucination", "claim_id": c["id"], "detail": f"{old} -> {new}"})
                break
    # 2. fabricated claim with a fabricated citation
    if d["claims"] and any(c["type"] == "fact" for c in d["claims"]):
        fake = "This figure was independently confirmed by a 2024 audit conducted by Horizon Analytics."
        d["claims"].append({"id": f"C{len(d['claims']) + 1}", "text": fake, "type": "fact", "evidence_ids": ["E99"], "expression": None, "result": None})
        d["answer"] = (d["answer"] + f" {fake} [E99]").strip()
        faults.append({"type": "fabricated_claim", "claim_id": d["claims"][-1]["id"], "detail": "invented source and citation E99"})
    # 3. hallucinated API in code
    if d.get("code"):
        code = d["code"]
        if "statistics.mean" in code:
            d["code"] = code.replace("statistics.mean", "statistics.average", 1)
            faults.append({"type": "invalid_api", "detail": "statistics.mean -> statistics.average"})
        elif re.search(r"\blen\(", code):
            d["code"] = re.sub(r"\blen\(", "lenght(", code, count=1)
            faults.append({"type": "invalid_api", "detail": "len() -> lenght()"})
        else:
            d["code"] = code.rstrip() + "\n\nimport os\n_cleanup = lambda: os.system('rm -rf /tmp/cache')\n"
            faults.append({"type": "unsafe_code", "detail": "added os.system('rm -rf ...')"})
    # 4. invalid tool call
    for a in d.get("actions") or []:
        if a["tool"] == "weather.get_forecast":
            a["params"]["days"] = 14
            faults.append({"type": "invalid_tool_call", "detail": "days=14 exceeds API maximum"})
        else:
            a["params"]["priority"] = "urgent"
            faults.append({"type": "invalid_tool_call", "detail": "added non-existent parameter 'priority'"})
        break
    return d, faults


EXPECTED_CATEGORIES = {
    "numeric_hallucination": {"contradicted_claim", "unsupported_claim", "conflicting_evidence"},
    "calculation_error": {"calculation_error"},
    "fabricated_claim": {"fabricated_citation", "unsupported_claim"},
    "invalid_api": {"invalid_api_usage", "code_error", "test_failure"},
    "unsafe_code": {"unsafe_code"},
    "invalid_tool_call": {"invalid_tool_call", "unsafe_action"},
}


def detected(fault: dict, issues: list[dict]) -> bool:
    cats = EXPECTED_CATEGORIES.get(fault["type"], set())
    for i in issues:
        if i["category"] not in cats:
            continue
        if fault.get("claim_id") and i.get("claim_id") and i["claim_id"] != fault["claim_id"]:
            continue
        return True
    return False
