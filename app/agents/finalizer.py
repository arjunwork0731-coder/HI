"""FINALIZER agent - the decision layer. Assembles the user-facing answer
from verified claims only, attaches confidence, caveats and citations, or
rejects / blocks / asks for clarification with explicit reasons."""
from __future__ import annotations

import re
from statistics import mean

from .. import prompts
from ..config import settings
from ..context import RunContext
from ..textutil import extract_quantities

GOOD = {"SUPPORTED", "DISCLOSED_CONFLICT"}
BAD = {"UNSUPPORTED", "CONTRADICTED", "CALC_ERROR", "INVALID", "CONFLICTING", "FAILED"}


class FinalizerAgent:
    name = "finalizer"

    async def finalize(self, ctx: RunContext, draft: dict | None, report: dict | None, critic: list[dict], rounds: int) -> dict:
        res = await self._decide(ctx, draft, report, critic or [], rounds)
        res.setdefault("assumptions", ctx.plan.get("assumptions", []))
        res["confidence"] = round(float(res.get("confidence", 0.0)), 3)
        ctx.emit(self.name, "decision", f"{res['status']} (confidence {res['confidence']:.2f})"
                 + (f" - {res['rejection_reasons'][0]}" if res.get("rejection_reasons") else ""),
                 {k: res.get(k) for k in ("status", "confidence", "rejection_reasons", "caveats")},
                 level="info" if res["status"].startswith("ACCEPTED") else "warn")
        return res

    async def _decide(self, ctx, draft, report, critic, rounds) -> dict:
        plan = ctx.plan
        if ctx.safety.get("level") == "block":
            cats = ", ".join(sorted({h["category"] for h in ctx.safety["hits"]}))
            return {"status": "BLOCKED_UNSAFE", "answer": "I won't carry out this request: it asks for a potentially harmful or destructive action "
                    f"({cats}). No agent generated output and no tool was executed.",
                    "confidence": 0.95, "rejection_reasons": [f"unsafe task intent: {h['category']} ('{h['match']}')" for h in ctx.safety["hits"]]}

        if plan.get("blocking_ambiguity"):
            return self._clarify(ctx)

        issues = (report or {}).get("issues", []) + critic
        claims = (report or {}).get("claims", [])

        # --- hard stops
        fp = [i for i in issues if i["category"] == "false_premise"]
        if fp:
            ev_ids = fp[0].get("evidence") or []
            corr = " ".join(f"{ctx.evidence[e]['text']} [{e}]" for e in ev_ids[:2] if e in ctx.evidence)
            return {"status": "REJECTED", "confidence": 0.9,
                    "answer": f"I can't answer this as asked because it rests on a false premise. {fp[0]['detail']}. " + (f"The evidence says: {corr}" if corr else ""),
                    "rejection_reasons": [fp[0]["detail"]], "verified_claims": _slim(c for c in claims if c["verdict"] in GOOD)}
        danger = [i for i in issues if i["category"] in ("dangerous_output", "unsafe_action", "unsafe_code") and i["severity"] == "critical"]
        if danger:
            return {"status": "BLOCKED_UNSAFE", "confidence": 0.9,
                    "answer": "The generated solution contained unsafe operations that could not be removed, so it was blocked.",
                    "rejection_reasons": [i["detail"] for i in danger], "actions": (report or {}).get("actions", [])}

        # --- tool actions
        missing_tool = [i for i in issues if i["category"] == "invalid_tool_call" and i["route_to"] == "finalizer"]
        if missing_tool:
            return {"status": "REJECTED", "confidence": 0.85, "actions": (report or {}).get("actions", []),
                    "answer": "I can't do this reliably: " + "; ".join(i["detail"] for i in missing_tool) + ". Nothing was executed.",
                    "rejection_reasons": [i["detail"] for i in missing_tool]}
        actions = (report or {}).get("actions") or []
        if actions:
            return self._actions(ctx, draft, actions, issues)

        # --- code
        if draft and draft.get("code") is not None or (report or {}).get("code"):
            code_rep = report.get("code") if report else None
            if code_rep and code_rep["passed"]:
                caveats = [i["detail"] for i in code_rep["issues"] if i["severity"] == "minor"]
                return {"status": "ACCEPTED" if not caveats else "ACCEPTED_WITH_CAVEATS", "confidence": 0.9 if not caveats else 0.75,
                        "answer": draft.get("answer") or "Code verified.", "code": draft["code"], "tests": draft.get("tests"),
                        "execution": code_rep.get("execution"), "caveats": caveats}
            reasons = [i["detail"] for i in (code_rep or {}).get("issues", []) if i["severity"] in ("critical", "major")] or ["no code produced"]
            return {"status": "REJECTED", "confidence": 0.2, "answer": "I could not produce code that passes independent verification.",
                    "code": (draft or {}).get("code"), "rejection_reasons": reasons}

        # --- claims
        good = [c for c in claims if c["verdict"] in GOOD]
        bad = [c for c in claims if c["verdict"] in BAD]
        uncertain = [c for c in claims if c["verdict"] == "UNCERTAIN"]
        unanswered = [i for i in issues if i["category"] == "critic:unanswered" and i["severity"] == "major"]
        if unanswered:
            return {"status": "REJECTED", "confidence": 0.85,
                    "answer": "I can't give a reliable answer: the available sources do not contain the information requested ("
                    + "; ".join(i["detail"] for i in unanswered[:2]) + ")."
                    + (" Related verified facts: " + " ".join(f"{c['text']} [{', '.join(_ids(c))}]" for c in good[:2]) if good else ""),
                    "rejection_reasons": ["insufficient evidence: " + i["detail"] for i in unanswered],
                    "verified_claims": _slim(good), "removed_claims": _removed(bad)}
        if not good:
            reasons = [f"{c['verdict'].lower()}: {c['text'][:120]}" for c in bad] or (draft or {}).get("insufficient") or ["no verifiable claims"]
            return {"status": "REJECTED", "confidence": 0.8,
                    "answer": "I can't give a reliable answer: none of the generated claims could be verified against trustworthy evidence.",
                    "rejection_reasons": reasons, "removed_claims": _removed(bad)}

        conf = mean(c["confidence"] for c in good)
        conf *= 0.92 ** len(bad) * 0.9 ** len(uncertain) * 0.97 ** max(rounds - 1, 0)
        unresolved = [c for c in ctx.conflicts if c["resolution"] == "unresolved"]
        disclosed = [c for c in good if c["verdict"] == "DISCLOSED_CONFLICT"]
        if disclosed:
            conf *= 0.85
        caveats = []
        if bad:
            caveats.append(f"{len(bad)} claim(s) failed verification and were removed.")
        if uncertain:
            caveats.append(f"{len(uncertain)} claim(s) have conflicting verifier verdicts (lower confidence).")
        for c in unresolved:
            if {c["a"], c["b"]} & {e for g in good for e in _ids(g)}:
                caveats.append(f"Sources disagree ({c['detail']}); {c['rule']}.")
        for c in ctx.conflicts:
            if c["resolution"] == "resolved" and {c["a"], c["b"]} & {e for g in good for e in _ids(g)}:
                caveats.append(f"Conflict resolved: {c['rule']}.")
        caveats += [f"Assumption: {a}" for a in ctx.plan.get("assumptions", [])]
        caveats += [f"Not covered: {x}" for x in (draft or {}).get("insufficient", [])]
        minor_critic = [i["detail"] for i in critic if i["severity"] == "minor"]
        caveats += minor_critic[:2]

        if conf < settings.accept_threshold:
            return {"status": "REJECTED", "confidence": conf,
                    "answer": "I can't give a sufficiently reliable answer (verified confidence below threshold).",
                    "rejection_reasons": [f"confidence {conf:.2f} < threshold {settings.accept_threshold}"],
                    "verified_claims": _slim(good), "removed_claims": _removed(bad)}

        if not bad and not uncertain and report.get("passed"):
            answer = draft["answer"]
        else:
            answer = await self._compose(ctx, good, caveats)
        status = "ACCEPTED" if not caveats else "ACCEPTED_WITH_CAVEATS"
        return {"status": status, "answer": answer, "confidence": conf, "verified_claims": _slim(good + uncertain),
                "removed_claims": _removed(bad), "caveats": list(dict.fromkeys(caveats))}

    # ------------------------------------------------------------------ helpers
    def _clarify(self, ctx: RunContext) -> dict:
        plan = ctx.plan
        parts = []
        for interp in plan.get("interpretations", [])[:3]:
            doc = next((d for d in ctx.kb.documents.values() if d.get("entity") == interp), None)
            if doc:  # definitional first sentence of that entity's document
                from ..textutil import split_sentences
                first = split_sentences(doc["text"])[0]
                hit = next((h for h in ctx.kb.search(first, k=3) if h["doc_id"] == doc["id"]), None)
                if hit:
                    eid = ctx.add_evidence([hit], found_by=self.name)[0]
                    parts.append(f"If you mean {interp}: {hit['text']} [{eid}]")
                    continue
            hits = [h for h in ctx.kb.search(f"{interp} {ctx.task}", k=2) if "prompt_injection" not in h["flags"]]
            if hits:
                eid = ctx.add_evidence(hits[:1], found_by=self.name)[0]
                parts.append(f"If you mean {interp}: {hits[0]['text']} [{eid}]")
        q = plan.get("clarification_question") or "Could you clarify what you mean?"
        return {"status": "NEEDS_CLARIFICATION", "confidence": 0.5, "clarification": q,
                "answer": q + ("\n\n" + "\n".join(parts) if parts else ""),
                "rejection_reasons": ["ambiguous request: " + " / ".join(plan.get("interpretations", []))]}

    def _actions(self, ctx, draft, actions, issues) -> dict:
        executed = [a for a in actions if a["status"].startswith("EXECUTED")]
        held = [a for a in actions if a["status"] == "HELD_FOR_APPROVAL"]
        rejected = [a for a in actions if a["status"] == "REJECTED"]
        caveats = [f"Not covered: {x}" for x in (draft or {}).get("insufficient", [])]
        if rejected and not executed and not held:
            reasons = [f"{a['requested']['tool']}: {i['detail']}" + (f" ({i['suggestion']})" if i.get("suggestion") else "")
                       for a in rejected for i in a["issues"]]
            return {"status": "REJECTED", "confidence": 0.85, "answer": "The requested tool call is invalid and was not executed: " + "; ".join(reasons),
                    "rejection_reasons": reasons, "actions": actions}
        if held:
            return {"status": "REQUIRES_APPROVAL", "confidence": 0.8, "actions": actions, "caveats": caveats,
                    "answer": "The request is valid but high-risk, so nothing was executed. Awaiting human approval for: "
                    + "; ".join(f"{a['tool']}({', '.join(f'{k}={v!r}' for k, v in a['requested']['params'].items())})" for a in held),
                    "rejection_reasons": [f"{a['tool']} is a {a['risk']} action and requires human confirmation" for a in held]}
        lines = [f"{a['tool']} -> {a.get('result')}" for a in executed]
        return {"status": "ACCEPTED" if not caveats and not rejected else "ACCEPTED_WITH_CAVEATS", "confidence": 0.9 if not caveats else 0.75,
                "answer": ((draft or {}).get("answer", "") + "\n" + "\n".join(lines)).strip(), "actions": actions, "caveats": caveats}

    async def _compose(self, ctx: RunContext, good: list[dict], caveats: list[str]) -> str:
        base = " ".join(f"{c['text'].rstrip('.')}." + (f" [{', '.join(_ids(c))}]" if _ids(c) else "") for c in good)
        if not ctx.llm.available:
            return base
        try:
            out = await ctx.llm.chat_json(prompts.FINALIZER,
                                          f"TASK:\n{ctx.task}\n\nVERIFIED CLAIMS:\n" + "\n".join(f"- {c['text']} [{', '.join(_ids(c))}]" for c in good)
                                          + "\n\nCAVEATS:\n" + "\n".join(f"- {c}" for c in caveats), role="finalizer", temperature=0.1, max_tokens=600)
            text = str(out.get("answer", "")).strip()
        except Exception:
            return base
        # final gate: the polished answer may not introduce new quantities
        allowed = [q for c in good for q in extract_quantities(c["text"])]
        new_q = [q.raw for q in extract_quantities(text) if not any(q.matches(a) or a.matches(q) for a in allowed)]
        if not text or new_q:
            ctx.emit(self.name, "final_gate", f"Polished answer introduced unverified quantities {new_q}; using verified claims verbatim", level="warn")
            return base
        return text


def _ids(c: dict) -> list[str]:
    ids = c.get("citation_repair") or c.get("supporting_evidence") or c.get("evidence_ids") or []
    return [i for i in ids if re.fullmatch(r"E\d+", i)][:3]


def _slim(claims) -> list[dict]:
    return [{"id": c["id"], "text": c["text"], "verdict": c["verdict"], "confidence": c["confidence"], "evidence": _ids(c)} for c in claims]


def _removed(claims) -> list[dict]:
    return [{"id": c["id"], "text": c["text"], "verdict": c["verdict"],
             "reason": next((ch["detail"] for ch in c.get("checks", []) if ch["verdict"] == "fail"), c["verdict"])} for c in claims]
