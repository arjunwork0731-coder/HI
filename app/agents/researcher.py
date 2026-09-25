"""RESEARCHER agent - retrieves evidence (knowledge base, user documents,
Wikipedia), quarantines manipulated sources, detects source conflicts, and
drafts evidence-grounded answers with per-claim citations."""
from __future__ import annotations

import json

from .. import prompts
from ..context import RunContext
from ..kb.retrieval import KnowledgeBase
from ..textutil import content_tokens
from ..tools.web import wikipedia_search
from ..verification.grounding import detect_source_conflicts, effective_reliability


class ResearcherAgent:
    name = "researcher"

    async def gather(self, ctx: RunContext, queries: list[str], *, targeted: bool = False, round_: int | None = None) -> list[str]:
        new_ids: list[str] = []
        before = set(ctx.evidence)
        for q in queries:
            hits = ctx.kb.search(q, k=6 if not targeted else 4)
            new_ids += ctx.add_evidence(hits, found_by=self.name)
        top_score = max((ctx.evidence[i]["score"] for i in new_ids), default=0)
        use_web = ctx.options.get("enable_web", True)
        if use_web and (targeted or top_score < 4.0):
            web_docs = []
            for q in queries[:2]:
                web_docs += await wikipedia_search(q, limit=2)
            fresh = [d for d in web_docs if d["id"] not in ctx.kb.documents]
            if fresh:
                ctx.kb = KnowledgeBase(list(ctx.kb.documents.values()) + fresh)
                for q in queries:
                    new_ids += ctx.add_evidence([h for h in ctx.kb.search(q, k=4) if h["origin"] == "web"], found_by=self.name)
                ctx.emit(self.name, "web_search", f"Fetched {len(fresh)} Wikipedia page(s)", [d["title"] for d in fresh], round_=round_)
        added = [i for i in dict.fromkeys(new_ids) if i not in before]
        quarantined = [i for i in added if "prompt_injection" in ctx.evidence[i]["flags"]]
        ctx.emit(self.name, "retrieve", f"{len(added)} new evidence passage(s) for {len(queries)} quer{'y' if len(queries) == 1 else 'ies'}",
                 {"queries": queries, "evidence": [self._brief(ctx.evidence[i]) for i in added]}, round_=round_)
        for i in quarantined:
            ctx.emit(self.name, "quarantine", f"{i} quarantined: prompt-injection text in '{ctx.evidence[i]['source']}'",
                     {"evidence_id": i, "matches": ctx.evidence[i].get("injection_matches")}, level="warn", round_=round_)
        conflicts = detect_source_conflicts(list(ctx.evidence.values()), ctx.kb.documents)
        known = {(c["a"], c["b"]) for c in ctx.conflicts}
        for c in conflicts:
            if (c["a"], c["b"]) not in known:
                ctx.conflicts.append(c)
                ctx.emit(self.name, "conflict", f"Conflict {c['a']} vs {c['b']}: {c['detail'][:90]} -> {c['resolution']} ({c['rule']})",
                         c, level="warn", round_=round_)
        return added

    @staticmethod
    def _brief(e: dict) -> dict:
        return {"id": e["id"], "source": e["source"], "reliability": e["reliability"], "flags": e["flags"], "text": e["text"][:160]}

    # ------------------------------------------------------------------ drafting
    async def draft(self, ctx: RunContext, feedback: list[dict] | None = None, previous: dict | None = None, round_: int = 1) -> dict:
        if ctx.llm.available:
            try:
                return await self._llm_draft(ctx, feedback, previous)
            except Exception as ex:
                ctx.emit(self.name, "llm_fallback", f"LLM drafting failed, using extractive drafting: {ex}", level="warn", round_=round_)
        return self._extractive_draft(ctx, feedback, previous)

    async def _llm_draft(self, ctx: RunContext, feedback, previous) -> dict:
        system = prompts.RESEARCHER_DRAFT
        user = f"TASK:\n{ctx.task}\n\n"
        if ctx.plan.get("assumptions"):
            user += "PLANNER ASSUMPTIONS: " + "; ".join(ctx.plan["assumptions"]) + "\n"
        if ctx.plan.get("presuppositions"):
            user += "PRESUPPOSITIONS TO CHECK (may be false): " + "; ".join(ctx.plan["presuppositions"]) + "\n"
        if ctx.conflicts:
            user += "KNOWN SOURCE CONFLICTS:\n" + "\n".join(
                f"- {c['a']} vs {c['b']}: {c['detail']} -> {c['resolution']}, prefer {c['preferred']} ({c['rule']})" for c in ctx.conflicts) + "\n"
        user += f"\nEVIDENCE:\n{ctx.evidence_block()}\n"
        if feedback:
            system += prompts.REVISION_SUFFIX.format(draft=json.dumps(_slim_draft(previous), ensure_ascii=False)[:4000],
                                                     feedback=_fmt_feedback(feedback))
        out = await ctx.llm.chat_json(system, user, role="researcher", temperature=0.2)
        return normalize_draft(out, author="researcher")

    def _extractive_draft(self, ctx: RunContext, feedback, previous) -> dict:
        """Offline generator: select the most relevant reliable sentences verbatim."""
        rejected_texts = {f.get("claim_text") for f in (feedback or []) if f.get("claim_text")}
        conflict_losers = {c["a"] if c["preferred"] == c["b"] else c["b"] for c in ctx.conflicts if c["resolution"] == "resolved"}
        q_toks = set(content_tokens(ctx.task))
        scored = []
        for e in ctx.usable_evidence():
            if e["id"] in conflict_losers or effective_reliability(e) < 0.5 or e["text"] in rejected_texts:
                continue
            overlap = len(q_toks & set(content_tokens(e["text"] + " " + e.get("entity", "")))) / max(len(q_toks), 1)
            scored.append(((overlap * 2 + e["score"] / 10) * (0.5 + effective_reliability(e) / 2), e))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return normalize_draft({"answer": "The available evidence does not answer this question.", "claims": [],
                                    "insufficient": [ctx.task], "self_confidence": 0.1}, author="researcher(extractive)")
        top = scored[0][0]
        chosen = [e for s, e in scored if s >= 0.6 * top][:3]
        # for unresolved conflicts, lead with the preferred (more reliable) source and disclose the other one
        disclosed = []
        for c in ctx.conflicts:
            if c["resolution"] != "unresolved" or not ({c["a"], c["b"]} & {e["id"] for e in chosen}):
                continue
            pref, other = c["preferred"], c["b"] if c["preferred"] == c["a"] else c["a"]
            chosen = [e for e in chosen if e["id"] != other]
            if pref not in {e["id"] for e in chosen}:
                chosen.insert(0, ctx.evidence[pref])
            disclosed.append(ctx.evidence[other])
        claims = [{"text": e["text"], "type": "fact", "evidence_ids": [e["id"]]} for e in chosen]
        answer = " ".join(f"{e['text']} [{e['id']}]" for e in chosen)
        for oe in disclosed:
            answer += f" However, another source reports differently: {oe['text']} [{oe['id']}]"
            claims.append({"text": oe["text"], "type": "fact", "evidence_ids": [oe["id"]], "disclosed_conflict": True})
        return normalize_draft({"answer": answer, "claims": claims, "self_confidence": 0.6}, author="researcher(extractive)")


def normalize_draft(d: dict, author: str) -> dict:
    claims = []
    for i, c in enumerate(d.get("claims") or []):
        if isinstance(c, str):
            c = {"text": c}
        if not isinstance(c, dict) or not str(c.get("text", "")).strip():
            continue
        claims.append({
            "id": f"C{len(claims) + 1}",
            "text": str(c.get("text")).strip(),
            "type": c.get("type") if c.get("type") in {"fact", "calculation", "assumption", "code"} else "fact",
            "evidence_ids": [str(x).strip("[] ") for x in (c.get("evidence_ids") or [])],
            "expression": c.get("expression") or None,
            "result": None if c.get("result") in (None, "") else str(c.get("result")),
            **({"disclosed_conflict": True} if c.get("disclosed_conflict") else {}),
        })
    actions = []
    for a in d.get("actions") or []:
        if isinstance(a, dict) and a.get("tool"):
            actions.append({"tool": str(a["tool"]), "params": a.get("params") or {}, "purpose": a.get("purpose", "")})
    try:
        sc = float(d.get("self_confidence", 0.5))
    except (TypeError, ValueError):
        sc = 0.5
    return {
        "author": author,
        "answer": str(d.get("answer") or "").strip(),
        "claims": claims,
        "code": d.get("code") or None,
        "tests": d.get("tests") or None,
        "actions": actions,
        "insufficient": [str(x) for x in (d.get("insufficient") or [])],
        "self_confidence": max(0.0, min(1.0, sc)),
    }


def _slim_draft(d: dict | None) -> dict:
    if not d:
        return {}
    return {k: d.get(k) for k in ("answer", "claims", "code", "tests", "actions")}


def _fmt_feedback(feedback: list[dict]) -> str:
    lines = []
    for f in feedback:
        line = f"- [{f.get('category')}] {f.get('detail')}"
        if f.get("claim_text"):
            line += f" | claim: \"{f['claim_text'][:200]}\""
        if f.get("hint"):
            line += f" | hint: {f['hint']}"
        lines.append(line)
    return "\n".join(lines)
