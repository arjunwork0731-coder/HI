"""CODER / TOOL-USE agent - calculations (as checkable expressions), code
(with tests), and tool/API call proposals. Never executes anything itself:
execution happens only inside the verifier's sandbox / mock tool layer."""
from __future__ import annotations

import json
import re

from .. import prompts
from ..context import RunContext
from ..textutil import extract_quantities
from ..tools.api_registry import TOOLS, describe_tools, resolve_tool
from ..tools.calculator import CalcError, safe_eval
from .researcher import _fmt_feedback, _slim_draft, normalize_draft

GROWTH_RE = re.compile(r"\b(growth|grow|grew|increase|increased|change|rise|rose|decline|decrease)\b", re.I)

CODE_TEMPLATES = [
    (r"\bprime", "is_prime", '''def is_prime(n: int) -> bool:
    """Return True if n is a prime number."""
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True''', '''assert is_prime(2) and is_prime(3) and is_prime(97)
assert not is_prime(1) and not is_prime(0) and not is_prime(-7) and not is_prime(91)
print("all tests passed")'''),
    (r"\bfactorial", "factorial", '''def factorial(n: int) -> int:
    """Return n! for n >= 0."""
    if n < 0:
        raise ValueError("n must be non-negative")
    result = 1
    for k in range(2, n + 1):
        result *= k
    return result''', '''assert factorial(0) == 1 and factorial(5) == 120
try:
    factorial(-1)
    raise AssertionError("expected ValueError")
except ValueError:
    pass
print("all tests passed")'''),
    (r"\bfibonacci", "fibonacci", '''def fibonacci(n: int) -> list[int]:
    """Return the first n Fibonacci numbers."""
    seq = []
    a, b = 0, 1
    for _ in range(max(n, 0)):
        seq.append(a)
        a, b = b, a + b
    return seq''', '''assert fibonacci(0) == [] and fibonacci(1) == [0]
assert fibonacci(7) == [0, 1, 1, 2, 3, 5, 8]
print("all tests passed")'''),
    (r"\bpalindrome", "is_palindrome", '''def is_palindrome(text: str) -> bool:
    """Case- and punctuation-insensitive palindrome check."""
    cleaned = [c.lower() for c in text if c.isalnum()]
    return cleaned == cleaned[::-1]''', '''assert is_palindrome("A man, a plan, a canal: Panama")
assert not is_palindrome("hello") and is_palindrome("")
print("all tests passed")'''),
    (r"\b(mean|average)\b", "average", '''import statistics

def average(values: list[float]) -> float:
    """Arithmetic mean of a non-empty list."""
    if len(values) == 0:
        raise ValueError("values must not be empty")
    return statistics.mean(values)''', '''assert average([1, 2, 3, 4]) == 2.5
assert average([5]) == 5
print("all tests passed")'''),
    (r"\bword count|count (the )?words", "word_count", '''from collections import Counter

def word_count(text: str) -> dict[str, int]:
    """Count case-insensitive word frequencies."""
    words = [w.strip(".,!?;:").lower() for w in text.split()]
    return dict(Counter(w for w in words if w))''', '''assert word_count("the cat and the hat") == {"the": 2, "cat": 1, "and": 1, "hat": 1}
assert word_count("") == {}
print("all tests passed")'''),
]


class CoderAgent:
    name = "coder"

    async def draft(self, ctx: RunContext, feedback: list[dict] | None = None, previous: dict | None = None, round_: int = 1) -> dict:
        if ctx.llm.available:
            try:
                return await self._llm(ctx, feedback, previous)
            except Exception as ex:
                ctx.emit(self.name, "llm_fallback", f"LLM coder failed, using rule-based fallback: {ex}", level="warn", round_=round_)
        if feedback and previous:
            return self._offline_revise(ctx, feedback, previous)
        return self._offline(ctx)

    async def _llm(self, ctx: RunContext, feedback, previous) -> dict:
        system = prompts.CODER.replace("{tools}", describe_tools())
        user = f"TASK:\n{ctx.task}\n\nTASK TYPE (from planner): {ctx.plan.get('task_type')}\n\nEVIDENCE:\n{ctx.evidence_block(limit=10)}\n"
        if feedback:
            system += prompts.REVISION_SUFFIX.format(draft=json.dumps(_slim_draft(previous), ensure_ascii=False)[:5000],
                                                     feedback=_fmt_feedback(feedback))
        out = await ctx.llm.chat_json(system, user, role="coder", temperature=0.1, max_tokens=2500)
        return normalize_draft(out, author="coder")

    # ------------------------------------------------------------------ offline (rule based)
    def _offline(self, ctx: RunContext) -> dict:
        ttype = ctx.plan.get("task_type")
        if ttype == "code":
            return self._offline_code(ctx)
        if ttype == "tool_action":
            return self._offline_tools(ctx)
        return self._offline_calc(ctx)

    def _offline_calc(self, ctx: RunContext) -> dict:
        t = ctx.task
        claims, answer = [], ""
        # compound / simple interest: "₹10,000 at 8% for 3 years", "What will ₹10,000 grow to at 8% compound interest for 3 years?"
        pm = re.search(r"(?:₹|rs\.?\s*|\$|inr\s*)\s*([\d,]+(?:\.\d+)?)|([\d]{1,3}(?:,\d{2,3})+(?:\.\d+)?)", t, re.I)
        rm = re.search(r"([\d.]+)\s*%", t)
        ym = re.search(r"(\d+)\s*years?", t, re.I)
        m = pm and rm and ym
        if m and re.search(r"interest|compound", t, re.I):
            p = (pm.group(1) or pm.group(2)).replace(",", "")
            r, n = rm.group(1), ym.group(1)
            if re.search(r"compound", t, re.I):
                expr = f"{p}*(1+{r}/100)**{n}"
                label = f"Amount after {n} years with annual compounding"
            else:
                expr = f"{p}*{r}*{n}/100"
                label = f"Simple interest over {n} years"
            res = round(safe_eval(expr), 2)
            claims.append({"text": f"{label} = {expr} = {res:,.2f}", "type": "calculation", "expression": expr, "result": str(res)})
            answer = f"{label}: {res:,.2f}."
        # "15% of 2400"
        elif re.search(r"([\d.]+)\s*%\s*of\s*(?:₹|\$)?\s*([\d,]+(?:\.\d+)?)", t):
            m = re.search(r"([\d.]+)\s*%\s*of\s*(?:₹|\$)?\s*([\d,]+(?:\.\d+)?)", t)
            expr = f"{m.group(1)}/100*{m.group(2).replace(',', '')}"
            res = round(safe_eval(expr), 4)
            claims.append({"text": f"{m.group(1)}% of {m.group(2)} = {_fmt(res)}", "type": "calculation", "expression": expr, "result": _fmt(res)})
            answer = f"{m.group(1)}% of {m.group(2)} is {_fmt(res)}."
        # growth between explicit numbers: "from 980 to 1240"
        elif GROWTH_RE.search(t) and re.search(r"from\s*(?:₹|\$)?\s*([\d,.]+)\s*(?:\w+\s*){0,2}to\s*(?:₹|\$)?\s*([\d,.]+)", t):
            m = re.search(r"from\s*(?:₹|\$)?\s*([\d,.]+)\s*(?:\w+\s*){0,2}to\s*(?:₹|\$)?\s*([\d,.]+)", t)
            a, b = m.group(1).replace(",", "").rstrip("."), m.group(2).replace(",", "").rstrip(".")
            expr = f"({b}-{a})/{a}*100"
            res = round(safe_eval(expr), 2)
            claims.append({"text": f"Percentage change from {a} to {b} = {expr} = {res}%", "type": "calculation", "expression": expr, "result": str(res)})
            answer = f"The change from {a} to {b} is {res}%."
        # growth from evidence: sentence with two amounts and two fiscal years
        elif GROWTH_RE.search(t):
            d = self._growth_from_evidence(ctx)
            if d:
                claims.append(d)
                answer = d["text"] + f" [{', '.join(d['evidence_ids'])}]"
        else:
            m = re.search(r"[\d(][\d\s.,+\-*/×x÷^()%]*[\d)]", t)
            if m and re.search(r"[-+*/×x÷^]", m.group(0)):
                expr = m.group(0).strip()
                try:
                    res = safe_eval(expr)
                    claims.append({"text": f"{expr} = {_fmt(res)}", "type": "calculation", "expression": expr, "result": _fmt(res)})
                    answer = f"{expr} = {_fmt(res)}"
                except CalcError:
                    pass
        if not claims:
            return normalize_draft({"answer": "I could not identify a computable expression or the required input values.",
                                    "claims": [], "insufficient": [ctx.task], "self_confidence": 0.1}, author="coder(rules)")
        return normalize_draft({"answer": answer, "claims": claims, "self_confidence": 0.7}, author="coder(rules)")

    def _growth_from_evidence(self, ctx: RunContext) -> dict | None:
        for e in sorted(ctx.usable_evidence(), key=lambda e: -e["score"]):
            qs = extract_quantities(e["text"])
            years = [q for q in qs if q.kind == "year"]
            money = [q for q in qs if q.kind == "money"]
            if len(years) >= 2 and len(money) >= 2 and e["reliability"] >= 0.5 and "superseded" not in e["flags"]:
                pairs = sorted(zip([int(y.value) for y in years], money), key=lambda p: p[0])
                (y0, m0), (y1, m1) = pairs[0], pairs[-1]
                a = re.sub(r"[^\d.]", "", m0.raw.replace(",", ""))
                b = re.sub(r"[^\d.]", "", m1.raw.replace(",", ""))
                expr = f"({b}-{a})/{a}*100"
                res = round(safe_eval(expr), 2)
                return {"text": f"Growth from FY{y0} ({m0.raw}) to FY{y1} ({m1.raw}) = {expr} = {res}%", "type": "calculation",
                        "expression": expr, "result": str(res), "evidence_ids": [e["id"]]}
        return None

    def _offline_code(self, ctx: RunContext) -> dict:
        for pat, fname, code, tests in CODE_TEMPLATES:
            if re.search(pat, ctx.task, re.I):
                return normalize_draft({
                    "answer": f"Implemented `{fname}` with tests.", "code": code, "tests": tests,
                    "claims": [{"text": f"The function {fname} passes its unit tests.", "type": "code"}], "self_confidence": 0.7,
                }, author="coder(templates)")
        return normalize_draft({"answer": "Offline mode cannot synthesise arbitrary code (no LLM configured).", "claims": [],
                                "insufficient": ["code generation requires an LLM"], "self_confidence": 0.0}, author="coder(templates)")

    def _offline_tools(self, ctx: RunContext) -> dict:
        t = ctx.task
        actions = []
        endpoint = re.search(r"\b(GET|POST|PUT|DELETE|PATCH)?\s*(/v\d+/[\w/{}-]+)", t)
        explicit = re.search(r"\b([a-z]+\.[a-z_]+)\b", t)
        if re.search(r"forecast|weather", t, re.I):
            city = re.search(r"\b(?:for|in)\s+([A-Z][a-zA-Z]+)", t)
            days = re.search(r"(\d+)\s*[- ]?days?", t)
            actions.append({"tool": "weather.get_forecast", "params": {"city": city.group(1) if city else "", "days": int(days.group(1)) if days else 3},
                            "purpose": "fetch forecast"})
        elif re.search(r"\brefund\b", t, re.I):
            order = re.search(r"order\s*(?:id\s*)?#?\s*([A-Za-z0-9-]+)", t, re.I)
            tool = f"{endpoint.group(1) or 'POST'} {endpoint.group(2)}".strip() if endpoint else "payments.refund"
            actions.append({"tool": tool, "params": {"order_id": order.group(1) if order else ""}, "purpose": "refund order"})
        elif re.search(r"\btransfer\b|\bwire\b|\bsend\b.{0,20}(₹|\$|rs|money)", t, re.I):
            amt = re.search(r"(?:₹|rs\.?|\$|inr|usd)\s*([\d,]+(?:\.\d+)?)", t, re.I)
            frm = re.search(r"from\s+(?:account\s+)?([A-Za-z0-9-]+)", t, re.I)
            to = re.search(r"to\s+(?:account\s+)?([A-Za-z0-9-]+)", t, re.I)
            actions.append({"tool": "payments.transfer", "params": {
                "from_account": frm.group(1) if frm else "", "to_account": to.group(1) if to else "",
                "amount": float(amt.group(1).replace(",", "")) if amt else 0, "currency": "USD" if "$" in t else "INR"}, "purpose": "transfer funds"})
        elif re.search(r"\b(select|query|sql)\b", t, re.I):
            sql = re.search(r'((?:select|delete|update|insert|drop)\b[^"\u201d]*)', t, re.I)
            actions.append({"tool": "db.query", "params": {"sql": sql.group(1) if sql else "SELECT 1"}, "purpose": "query database"})
        elif re.search(r"\bdelete\b|\bremove\b", t, re.I):
            path = re.search(r"(/[\w./*-]*)", t)
            actions.append({"tool": "files.delete", "params": {"path": path.group(1) if path else "", "recursive": bool(re.search(r"\ball\b|\*|recursive", t, re.I))},
                            "purpose": "delete files"})
        elif re.search(r"\bemail\b", t, re.I):
            to = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", t)
            actions.append({"tool": "email.send", "params": {"to": to.group(0).rstrip(".") if to else "", "subject": "Requested message", "body": t}, "purpose": "send email"})
        elif re.search(r"calendar|schedule|meeting|event", t, re.I):
            title = re.search(r"[\"“']([^\"”']+)[\"”']", t)
            start = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", t)
            dur = re.search(r"(\d+)\s*(?:min|minutes)", t)
            actions.append({"tool": "calendar.create_event", "params": {"title": title.group(1) if title else "Meeting",
                            "start": start.group(0) if start else "", "duration_minutes": int(dur.group(1)) if dur else 30}, "purpose": "create event"})
        elif explicit and explicit.group(1) in TOOLS:
            actions.append({"tool": explicit.group(1), "params": {}, "purpose": "requested tool"})
        elif endpoint:
            actions.append({"tool": f"{endpoint.group(1) or 'GET'} {endpoint.group(2)}", "params": {}, "purpose": "requested endpoint"})
        if not actions:
            return normalize_draft({"answer": "I could not map this request to a registered tool.", "claims": [],
                                    "insufficient": [ctx.task], "self_confidence": 0.1}, author="coder(rules)")
        return normalize_draft({"answer": f"Proposed {len(actions)} tool call(s).", "actions": actions, "claims": [], "self_confidence": 0.6},
                               author="coder(rules)")

    def _offline_revise(self, ctx: RunContext, feedback: list[dict], previous: dict) -> dict:
        d = json.loads(json.dumps(previous))
        d["author"] = "coder(rules-revision)"
        notes = []
        # 1. recompute calculations
        for c in d["claims"]:
            if c.get("type") == "calculation" and c.get("expression"):
                try:
                    res = safe_eval(c["expression"])
                    new = _fmt(round(res, 2))
                    if c.get("result") and c["result"] != new:
                        c["text"] = c["text"].replace(c["result"], new)
                        d["answer"] = d["answer"].replace(c["result"], new)
                        c["result"] = new
                        notes.append("recomputed with calculator")
                except CalcError:
                    pass
        # 2. apply analyzer suggestions to code
        if d.get("code"):
            # remove lines flagged as unsafe (line numbers refer to code followed by tests)
            bad_lines = {int(m.group(1)) for f in feedback if f.get("category") == "unsafe_code"
                         for m in [re.match(r"line (\d+):", f.get("detail", ""))] if m}
            if bad_lines:
                lines = d["code"].splitlines()
                kept = [ln for i, ln in enumerate(lines, 1) if i not in bad_lines]
                code = "\n".join(kept)
                for mod in ("os", "shutil", "subprocess", "socket"):
                    if re.search(rf"^\s*import {mod}\s*$", code, re.M) and not re.search(rf"\b{mod}\.", code):
                        code = re.sub(rf"^\s*import {mod}\s*\n?", "", code, flags=re.M)
                d["code"] = code.rstrip() + "\n"
                notes.append(f"removed {len(bad_lines)} unsafe line(s)")
            for f in feedback:
                sug = f.get("hint") or ""
                m = re.search(r"'([\w.]+)' has no attribute '(\w+)'.*?did you mean: (\w+)", (f.get("detail") or "") + " " + sug)
                if m:
                    d["code"] = re.sub(rf"\b{re.escape(m.group(1))}\.{m.group(2)}\b", f"{m.group(1)}.{m.group(3)}", d["code"])
                    notes.append(f"{m.group(1)}.{m.group(2)} -> {m.group(1)}.{m.group(3)}")
                m = re.search(r"name '(\w+)' is used but never defined.*?did you mean: (\w+)", (f.get("detail") or "") + " " + sug)
                if m:
                    d["code"] = re.sub(rf"\b{m.group(1)}\b", m.group(2), d["code"])
                    notes.append(f"{m.group(1)} -> {m.group(2)}")
        # 3. repair tool calls
        for a in d.get("actions", []):
            for f in feedback:
                if f.get("action_tool") != a["tool"]:
                    continue
                det = f.get("detail", "")
                m = re.search(r"(\w+): value (\S+) exceeds maximum (\S+)", det)
                if m and m.group(1) in a["params"]:
                    a["params"][m.group(1)] = int(float(m.group(3)))
                    notes.append(f"clamped {m.group(1)} to API maximum {m.group(3)}")
                    d.setdefault("insufficient", []).append(f"the API only supports {m.group(1)} <= {m.group(3)}")
                m = re.search(r"did you mean: ([^,?]+)", f.get("hint") or "")
                if f.get("category") == "invalid_tool_call" and "not a registered" in det and m and resolve_tool(m.group(1).strip()):
                    real = resolve_tool(m.group(1).strip())
                    notes.append(f"'{a['tool']}' does not exist; using registered tool {real}")
                    d.setdefault("insufficient", []).append(f"requested endpoint {a['tool']} does not exist")
                    a["tool"] = real
        # drop parameters the (possibly re-mapped) tool does not accept
        for a in d.get("actions", []):
            spec = TOOLS.get(resolve_tool(a["tool"]) or "")
            if spec:
                for p in [p for p in a["params"] if p not in spec["params"]]:
                    a["params"].pop(p)
                    notes.append(f"removed unsupported parameter '{p}'")
        d["answer"] = (d.get("answer") or "") + (f" (Revised: {'; '.join(dict.fromkeys(notes))}.)" if notes else "")
        return d


def _fmt(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.4f}".rstrip("0").rstrip(".")
