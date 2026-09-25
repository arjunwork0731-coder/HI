"""PLANNER agent - classifies the task, surfaces ambiguity, assumptions and
presuppositions (possible false premises), and plans retrieval.

Deterministic safety screening and ambiguity detection always run, even
when an LLM planner is available, so the LLM cannot talk the system out of
them.
"""
from __future__ import annotations

import re

from .. import prompts
from ..context import RunContext
from ..tools.api_registry import TOOLS
from ..verification.safety import assess_task

CODE_RE = re.compile(r"\b(write|implement|create|generate|fix|debug|code)\b.{0,60}\b(function|code|script|program|class|method|snippet|python)\b|\bpython (function|code|script)\b", re.I)
TOOL_RE = re.compile(r"\b(forecast|weather|transfer|refund|delete|remove|wipe|send (an )?email|email|schedule (a )?(meeting|event)|calendar|run (a )?(sql|query)|query the database|select .+ from|api|endpoint)\b|/v\d+/", re.I)
QUESTION_RE = re.compile(r"^\s*(what|when|who|whom|whose|which|where|why|how|is|are|was|were|does|do|did|can|could|should|explain|describe|tell)\b", re.I)
EXPLICIT_TOOL_RE = re.compile(r"\b(weather|payments|files|db|email|calendar)\.[a-z_]+\b|/v\d+/", re.I)
CALC_RE = re.compile(r"(\d[\d,.]*\s*[-+*/×x÷^]\s*\d)|\b(calculate|compute|how much|percent(age)?|growth|increase|decrease|interest|average|sum of|product of|ratio|% of)\b", re.I)
WHY_RE = re.compile(r"^\s*(why|how come)\s+(did|does|do|is|was|were|are|has|have)\s+(?P<rest>.+?)\??\s*$", re.I)


class PlannerAgent:
    name = "planner"

    async def run(self, ctx: RunContext) -> dict:
        task = ctx.task
        safety = assess_task(task)
        ctx.safety = safety
        if safety["level"] != "safe":
            ctx.emit(self.name, "safety_screen", f"Task risk level: {safety['level'].upper()}",
                     safety, level="warn" if safety["level"] == "confirm" else "error")

        heuristic = self._heuristic_plan(ctx)
        plan = heuristic
        if ctx.llm.available:
            try:
                llm_plan = await ctx.llm.chat_json(prompts.PLANNER, f"TASK:\n{task}", role="planner")
                plan = self._merge(heuristic, llm_plan)
                plan["source"] = "llm+rules"
            except Exception as ex:  # fall back to rules
                ctx.emit(self.name, "llm_fallback", f"LLM planner failed, using rules: {ex}", level="warn")
        # deterministic ambiguity check (entity alias collision in the knowledge base)
        amb = self._alias_ambiguity(ctx)
        if amb and not plan.get("blocking_ambiguity"):
            plan["blocking_ambiguity"] = True
            plan["interpretations"] = amb["interpretations"]
            plan["clarification_question"] = amb["question"]
            plan["ambiguity_source"] = "entity alias collision in evidence"
        ctx.plan = plan
        ctx.emit(self.name, "plan", self._summary(plan), plan)
        return plan

    # ------------------------------------------------------------------ rules
    def _heuristic_plan(self, ctx: RunContext) -> dict:
        t = ctx.task
        if CODE_RE.search(t):
            ttype = "code"
        elif EXPLICIT_TOOL_RE.search(t) or (TOOL_RE.search(t) and not QUESTION_RE.match(t)):
            ttype = "tool_action"
        elif CALC_RE.search(t):
            ttype = "calculation"
        else:
            ttype = "factual_qa"
        presup = []
        m = WHY_RE.match(t)
        if m:
            presup.append(m.group("rest").strip().rstrip("?") + ".")
        return {
            "task_type": ttype,
            "interpretations": [t],
            "blocking_ambiguity": False,
            "clarification_question": "",
            "assumptions": [],
            "presuppositions": presup,
            "subquestions": [t],
            "search_queries": [t],
            "needs_calculation": ttype == "calculation",
            "needs_code": ttype == "code",
            "needs_tools": ttype == "tool_action",
            "source": "rules",
        }

    def _merge(self, h: dict, l: dict) -> dict:
        p = dict(h)
        for k in ("interpretations", "assumptions", "presuppositions", "subquestions", "search_queries"):
            v = l.get(k)
            if isinstance(v, list) and v:
                p[k] = [str(x) for x in v][:6]
        if l.get("task_type") in {"factual_qa", "calculation", "code", "tool_action", "mixed"}:
            # rules win for code/tool detection (they gate safety-relevant paths)
            if h["task_type"] in ("code", "tool_action"):
                p["task_type"] = h["task_type"]
            else:
                p["task_type"] = l["task_type"]
        p["blocking_ambiguity"] = bool(l.get("blocking_ambiguity")) and len(p.get("interpretations", [])) > 1
        p["clarification_question"] = l.get("clarification_question") or ""
        for k in ("needs_calculation", "needs_code", "needs_tools"):
            p[k] = bool(l.get(k)) or h[k]
        if h["presuppositions"] and not l.get("presuppositions"):
            p["presuppositions"] = h["presuppositions"]
        p["search_queries"] = list(dict.fromkeys([ctx_q for ctx_q in p.get("search_queries", []) if ctx_q]))[:4] or h["search_queries"]
        return p

    def _alias_ambiguity(self, ctx: RunContext) -> dict | None:
        hits = ctx.kb.search(ctx.task, k=8)
        best: dict[str, tuple[float, dict]] = {}
        for h in hits:
            for alias in h.get("aliases") or []:
                if alias in ctx.task.lower():
                    ent = h["entity"]
                    if ent not in best or h["score"] > best[ent][0]:
                        best[ent] = (h["score"], h)
        if len(best) < 2:
            return None
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
        (e1, (s1, _)), (e2, (s2, _)) = ranked[0], ranked[1]
        if s2 / max(s1, 1e-6) < 0.75:
            return None  # the task context clearly favours one reading
        return {"interpretations": [e1, e2],
                "question": f"Your question could refer to {e1} or {e2}. Which one do you mean?"}

    @staticmethod
    def _summary(p: dict) -> str:
        s = f"Task type: {p['task_type']}"
        if p.get("blocking_ambiguity"):
            s += f" | AMBIGUOUS: {', '.join(p['interpretations'][:3])}"
        if p.get("presuppositions"):
            s += f" | presupposes: {p['presuppositions'][0][:80]}"
        if p.get("assumptions"):
            s += f" | assumptions: {len(p['assumptions'])}"
        return s
