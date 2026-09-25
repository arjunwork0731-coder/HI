"""CRITIC agent - adversarial review of the reasoning (not the facts):
does the answer address the task, does it overclaim, does it ignore
ambiguity or accept a false premise? Deterministic rules always run; an
LLM critic adds judgement when available."""
from __future__ import annotations

import json
import re

from .. import prompts
from ..context import RunContext
from ..textutil import content_tokens, extract_quantities, tokens

GENERIC = set("""tell key fact about what when where who whom how long many much big size large old detail explain describe
give list find show summary summarize summarise info information know please brief short answer question does did do is are
was were current currently latest main important notable according report say percentage percent value number amount total figure""".split())
SYNONYMS = {
    "tall": {"height", "tallest", "metr"}, "height": {"tall", "tallest"}, "land": {"landing", "touch", "soft-land"},
    "found": {"establish"}, "cost": {"inr", "budget", "price", "crore"}, "employee": {"staff", "headcount", "employe"},
    "revenue": {"sale", "turnover"}, "big": {"radius", "size", "largest", "smallest"}, "headquarter": {"based", "headquarter"},
    "launch": {"launched", "liftoff"}, "visible": {"see", "visibl"}, "refund": {"refunded"}, "moon": {"lunar"}, "lunar": {"moon"},
    "year": {"orbital", "period"}, "orbit": {"orbital"}, "melt": {"melting"}, "window": {"within", "day", "period"},
    "found": {"establish", "founded", "founder"}, "old": {"founded", "age"}, "far": {"distance", "closest"},
}
UNITS = {
    "kg": ["kg", "kilogram"], "km": ["km", "kilomet"], "metre": ["metre", "meter", " m "], "mile": ["mile"], "tonne": ["tonne", "ton"],
    "litre": ["litre", "liter"], "celsius": ["°c", "celsius", "degree"], "second": ["second"], "hour": ["hour"],
    "crore": ["crore"], "lakh": ["lakh"], "rupee": ["₹", "rupee", "inr", "crore"], "dollar": ["$", "dollar", "usd"],
}
UNIT_RE = re.compile(r"\b(kilograms?|kg|kilomet(?:re|er)s?|km|met(?:re|er)s?|miles?|tonnes?|lit(?:re|er)s?|celsius|°c|hours?|crores?|lakhs?|rupees?|dollars?)\b", re.I)


def _unit_key(u: str) -> str:
    u = u.lower()
    for k in UNITS:
        if u.startswith(k[:4]) or u == k:
            return k
    return {"kilograms": "kg", "kilogram": "kg", "°c": "celsius"}.get(u, "kg")


OVERCLAIM = re.compile(r"\b(always|never fails|definitely|certainly|guaranteed|undoubtedly|proves|100%)\b", re.I)


class CriticAgent:
    name = "critic"

    async def review(self, ctx: RunContext, draft: dict, report: dict, round_: int) -> list[dict]:
        issues = self._rules(ctx, draft, report)
        if ctx.llm.available and ctx.plan.get("task_type") not in ("tool_action",):
            try:
                out = await ctx.llm.chat_json(
                    prompts.CRITIC,
                    f"TASK:\n{ctx.task}\n\nDRAFT ANSWER:\n{draft.get('answer')}\n\nCLAIMS:\n"
                    + json.dumps([{"i": i, "text": c["text"]} for i, c in enumerate(draft["claims"])], ensure_ascii=False)
                    + (f"\n\nCODE:\n{draft['code'][:3000]}" if draft.get("code") else "")
                    + ("\n\nKNOWN SOURCE CONFLICTS: " + "; ".join(c["detail"] for c in ctx.conflicts) if ctx.conflicts else ""),
                    role="critic", model=ctx.llm.cfg.verifier_model, temperature=0.1, max_tokens=700)
                for it in out.get("issues") or []:
                    sev = it.get("severity", "low")
                    if sev not in ("high", "medium", "low"):
                        continue
                    issues.append({"severity": {"high": "major", "medium": "minor", "low": "minor"}[sev],
                                   "category": f"critic:{it.get('type', 'other')}", "route_to": "researcher" if ctx.plan.get("task_type") != "code" else "coder",
                                   "detail": str(it.get("detail", ""))[:300], "source": "llm-critic"})
            except Exception as ex:
                ctx.emit(self.name, "llm_fallback", f"LLM critic unavailable: {ex}", level="warn", round_=round_)
        ctx.emit(self.name, "review", f"{len(issues)} reasoning issue(s)" if issues else "No reasoning issues found",
                 issues, round_=round_, level="warn" if any(i["severity"] == "major" for i in issues) else "info")
        return issues

    def _rules(self, ctx: RunContext, draft: dict, report: dict) -> list[dict]:
        issues = []
        ttype = ctx.plan.get("task_type")
        answer = draft.get("answer", "")
        if ttype in ("factual_qa", "mixed", "calculation") and not draft.get("code"):
            # 1. does the answer (and its supporting evidence) cover the task's focus terms?
            supported_ids = {e for c in report["claims"] if c["verdict"] in ("SUPPORTED", "DISCLOSED_CONFLICT")
                             for e in c.get("supporting_evidence", []) + c.get("evidence_ids", [])}
            ans_hay = set(tokens(answer + " " + " ".join(ctx.evidence[e]["text"] + " " + ctx.evidence[e]["title"]
                                                         for e in supported_ids if e in ctx.evidence)))
            all_hay = set(tokens(" ".join(e["text"] + " " + e["title"] for e in ctx.usable_evidence())))
            focus = [t for t in dict.fromkeys(content_tokens(ctx.task)) if t not in GENERIC and len(t) > 2]
            for term in focus:
                if _present(term, ans_hay):
                    continue
                if not _present(term, all_hay):
                    # only names, acronyms and identifiers (CEO, FY2019, Hosur) are hard evidence gaps;
                    # ordinary words may just be paraphrased in the sources
                    sev = "major" if _is_specific(term, ctx.task) else "minor"
                    issues.append({"severity": sev, "category": "critic:unanswered", "route_to": "researcher", "term": term,
                                   "detail": f"no evidence mentions '{term}', which the question asks about - the answer cannot address it",
                                   "hint": f"search for '{term}'; if nothing reliable exists, say the information is unavailable"})
                else:
                    issues.append({"severity": "minor", "category": "critic:unanswered", "route_to": "researcher", "term": term,
                                   "detail": f"the answer does not address '{term}'"})
            # task quantities (years, dates) must be covered
            ans_q = extract_quantities(answer + " " + " ".join(c["text"] for c in draft["claims"]))
            ev_q = [q for e in ctx.usable_evidence() for q in extract_quantities(e["text"])]
            for q in extract_quantities(ctx.task):
                if q.kind in ("year", "date") and not any(q.matches(a) or a.matches(q) for a in ans_q):
                    sev = "major" if not any(q.matches(e) or e.matches(q) for e in ev_q) else "minor"
                    issues.append({"severity": sev, "category": "critic:unanswered", "route_to": "researcher",
                                   "detail": f"the question asks about {q.raw} but the answer {'and the evidence do' if sev == 'major' else 'does'} not cover it"})
        if ttype in ("factual_qa", "mixed", "calculation") and not draft.get("code"):
            # 2. the question asks for a value in a unit that no source provides
            unit_q = UNIT_RE.search(ctx.task)
            if unit_q:
                unit = unit_q.group(0).lower()
                stems = UNITS[_unit_key(unit)]
                hay = (answer + " " + " ".join(e["text"] for e in ctx.usable_evidence())).lower()
                if not any(re.search(rf"\b{re.escape(st)}", hay) for st in stems):
                    issues.append({"severity": "major", "category": "critic:unanswered", "route_to": "researcher", "term": unit,
                                   "detail": f"the question asks for a value in {unit}, but no evidence states one"})
        if ttype == "calculation" and draft["claims"]:
            # 3. a calculation must use the numbers given in the task
            task_nums = [q for q in extract_quantities(ctx.task) if q.kind not in ("year", "date")]
            used = " ".join((c.get("expression") or "") + " " + c["text"] for c in draft["claims"])
            used_q = extract_quantities(used) + [q for x in re.findall(r"\d+(?:\.\d+)?", used) for q in extract_quantities(x)]
            if task_nums and not any(t.matches(u) or u.matches(t) or float(t.value) == float(u.value) for t in task_nums for u in used_q
                                     if not isinstance(u.value, str) and not isinstance(t.value, str)):
                issues.append({"severity": "major", "category": "critic:unanswered", "route_to": "coder",
                               "detail": f"the calculation ignores the numbers given in the task ({', '.join(q.raw for q in task_nums)}) - it answers a different question"})
        if OVERCLAIM.search(answer):
            issues.append({"severity": "minor", "category": "critic:overclaim", "route_to": "researcher",
                           "detail": f"absolute language ('{OVERCLAIM.search(answer).group(0)}') stronger than evidence supports"})
        if len(ctx.plan.get("interpretations") or []) > 1 and not ctx.plan.get("blocking_ambiguity") and not ctx.plan.get("assumptions"):
            issues.append({"severity": "minor", "category": "critic:ambiguity", "route_to": "researcher",
                           "detail": "task has several readings but the answer does not state which one it uses"})
        return issues


def _is_specific(term: str, task: str) -> bool:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", task)
    for i, w in enumerate(words):
        if tokens(w) and tokens(w)[0] == term:
            if any(ch.isdigit() for ch in w) or (w.isupper() and len(w) > 1) or (i > 0 and w[0].isupper()):
                return True
    return False


def _present(term: str, hay: set) -> bool:
    if term in hay:
        return True
    cands = {term} | SYNONYMS.get(term, set())
    return any(h.startswith(c) or c.startswith(h) and len(h) >= 4 for c in cands for h in hay)
