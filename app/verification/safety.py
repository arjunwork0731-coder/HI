"""Unsafe-action and risky-content detection (rule based, deterministic).

Three layers:
  * task intent     - is the user asking for something destructive / harmful?
  * proposed actions - validated tool calls (see tools.api_registry)
  * output content  - does an answer instruct the reader to do something dangerous?
"""
from __future__ import annotations

import re

RULES = [
    # (category, severity, pattern)
    ("destructive_system", "block", r"\brm\s+-rf?\b|\bdel\s+/[sq]\b|\bformat\s+(the\s+)?(c:|disk|drive|hard\s*drive)|\bmkfs\b|\bdd\s+if=.*of=/dev/"),
    ("destructive_system", "block", r"\b(delet(e|es|ing)|wip(e|es|ing)|eras(e|es|ing)|destroy(s|ing)?|remov(e|es|ing))\s+(all|every|the entire|entire|whole)\b.{0,40}\b(files?|data|database|logs?|directory|directories|folders?|disk|home|records|backups?)"),
    ("destructive_data", "block", r"\bdrop\s+(table|database|schema)\b|\btruncate\s+table\b|\bdelete\s+from\s+\w+(?![^;]*\bwhere\b)"),
    ("security_bypass", "block", r"\b(disable|bypass|turn off|circumvent)\b.{0,30}\b(firewall|antivirus|authentication|2fa|mfa|security|audit log|logging)\b"),
    ("credential_exfiltration", "block", r"\b(steal|exfiltrate|dump|harvest|leak|send)\b.{0,40}\b(passwords?|credentials?|api keys?|tokens?|credit card|card numbers|aadhaar|ssn)\b"),
    ("malware", "block", r"\b(keylogger|ransomware|botnet|trojan|rootkit|credential stealer|ddos (tool|script)|reverse shell)\b"),
    ("violent_harm", "block", r"\b(make|build|synthes[iz]e|assemble)\b.{0,30}\b(bomb|explosive|nerve agent|bioweapon|chemical weapon|pipe bomb)\b"),
    ("financial_action", "confirm", r"\b(transfer|send|wire|pay|move)\b.{0,40}(₹|\$|rs\.?|inr|usd|rupees|dollars|money|funds)|\brefund\s+(order|payment|the|customer|#?\d)"),
    ("external_communication", "confirm", r"\b(send|email|mail)\b.{0,30}\b(email|mail|message)\b.{0,40}\b(to|@)\b"),
    ("privilege_change", "confirm", r"\b(grant|give|elevate)\b.{0,30}\b(admin|root|sudo|superuser)\b"),
]
_COMPILED = [(c, s, re.compile(p, re.I)) for c, s, p in RULES]

DANGEROUS_OUTPUT = [
    (r"\brm\s+-rf\s+(/|~|\*|\$HOME)", "instructs recursive deletion of system/home files"),
    (r"curl[^|\n]*\|\s*(sudo\s+)?(ba)?sh", "pipes remote script into a shell"),
    (r"chmod\s+(-R\s+)?777\s+/", "makes system paths world-writable"),
    (r"\bdrop\s+(table|database)\b", "destructive SQL"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
]


def assess_task(task: str) -> dict:
    hits = []
    for cat, sev, pat in _COMPILED:
        m = pat.search(task or "")
        if m:
            hits.append({"category": cat, "severity": sev, "match": m.group(0)[:80]})
    level = "block" if any(h["severity"] == "block" for h in hits) else ("confirm" if hits else "safe")
    return {"level": level, "hits": hits}


def assess_output(text: str) -> list[dict]:
    out = []
    for pat, why in DANGEROUS_OUTPUT:
        m = re.search(pat, text or "", re.I)
        if m:
            out.append({"type": "dangerous_instruction", "detail": why, "match": m.group(0)[:60]})
    return out
