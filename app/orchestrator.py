"""Orchestrator - runs the plan -> research -> generate -> verify -> critique
-> (route back & revise)* -> finalize loop and records a full audit trail.

Routing table (failure category -> responsible agent):
  unsupported / contradicted / conflicting / fabricated citation / injection echo
  / undisclosed conflict / critic issues          -> researcher (targeted retrieval + redraft)
  calculation error / invalid API / test failure
  / invalid tool call / unsafe code              -> coder
  false premise / unsafe action / dangerous output -> finalizer (reject or block)
"""
from __future__ import annotations

import time

from .agents.coder import CoderAgent
from .agents.critic import CriticAgent
from .agents.finalizer import FinalizerAgent
from .agents.planner import PlannerAgent
from .agents.researcher import ResearcherAgent
from .agents.verifier import VerifierAgent
from .config import settings
from .context import RunContext
from .faults import detected, inject_faults

planner, researcher, coder, verifier, critic_agent, finalizer = (
    PlannerAgent(), ResearcherAgent(), CoderAgent(), VerifierAgent(), CriticAgent(), FinalizerAgent())


def _generator_for(ttype: str):
    return coder if ttype in ("calculation", "code", "tool_action") else researcher


def _claim_diff(prev: dict, new: dict) -> dict:
    p = {c["text"] for c in prev.get("claims", [])}
    n = {c["text"] for c in new.get("claims", [])}
    return {"removed": sorted(p - n), "added": sorted(n - p), "answer_changed": prev.get("answer") != new.get("answer"),
            "code_changed": prev.get("code") != new.get("code"), "actions_changed": prev.get("actions") != new.get("actions")}


async def run_task(ctx: RunContext) -> dict:
    t0 = time.time()
    ctx.emit("orchestrator", "start", f"Run {ctx.id} started in {settings.mode.upper()} mode",
             {"task": ctx.task, "options": ctx.options, "mode": settings.mode, "generator_model": settings.generator_model if settings.llm_enabled else None,
              "verifier_model": settings.verifier_model if settings.llm_enabled else None, "user_docs": len(ctx.user_docs)})
    draft = report = None
    critic_issues: list[dict] = []
    rounds = 0
    reports: list[dict] = []
    try:
        plan = await planner.run(ctx)
        if ctx.safety.get("level") == "block":
            result = await finalizer.finalize(ctx, None, None, [], 0)
            return _finish(ctx, result, reports, t0)

        # evidence first (also used to resolve ambiguity with examples)
        queries = list(dict.fromkeys([ctx.task] + plan.get("search_queries", []) + plan.get("subquestions", [])))[:5]
        await researcher.gather(ctx, queries, round_=0)
        if plan.get("blocking_ambiguity"):
            result = await finalizer.finalize(ctx, None, None, [], 0)
            return _finish(ctx, result, reports, t0)

        gen = _generator_for(plan["task_type"])
        draft = await gen.draft(ctx, round_=1)
        ctx.emit(gen.name, "draft", f"Draft 1 with {len(draft['claims'])} claim(s)"
                 + (", code" if draft.get("code") else "") + (f", {len(draft['actions'])} action(s)" if draft.get("actions") else ""),
                 {"draft": draft}, round_=1)
        if ctx.options.get("inject_faults"):
            draft, faults = inject_faults(draft)
            ctx.injected_faults = faults
            ctx.emit("fault-injector", "inject", f"Injected {len(faults)} fault(s) into draft 1 (red-team mode)", faults, level="warn", round_=1)

        max_rounds = max(1, int(ctx.options.get("max_rounds") or settings.max_rounds))
        for rnd in range(1, max_rounds + 1):
            rounds = rnd
            report = await verifier.verify(ctx, draft, rnd)
            critic_issues = await critic_agent.review(ctx, draft, report, rnd)
            reports.append({"round": rnd, "report": report, "critic": critic_issues})
            blocking = [i for i in report["issues"] + critic_issues if i["severity"] in ("critical", "major")]
            if rnd == 1 and ctx.injected_faults:
                for f in ctx.injected_faults:
                    f["detected"] = detected(f, report["issues"] + critic_issues)
            if not blocking:
                break
            fixable = [i for i in blocking if i["route_to"] in ("researcher", "coder")]
            terminal = [i for i in blocking if i["route_to"] == "finalizer"]
            if terminal or not fixable:
                ctx.emit("orchestrator", "route", f"{len(terminal) or len(blocking)} issue(s) cannot be fixed by revision -> finalizer", round_=rnd, level="warn")
                break
            if rnd == max_rounds:
                ctx.emit("orchestrator", "route", "Revision budget exhausted -> finalizer decides with what is verified", round_=rnd, level="warn")
                break
            # ---- route back for correction
            routes = sorted({i["route_to"] for i in fixable})
            ctx.emit("orchestrator", "route", f"Routing {len(fixable)} issue(s) back to {', '.join(routes)}",
                     [{"category": i["category"], "to": i["route_to"], "detail": i["detail"][:140]} for i in fixable], round_=rnd, level="warn")
            if "researcher" in routes:
                targets = [i.get("claim_text") or i.get("term") or "" for i in fixable if i["route_to"] == "researcher"]
                targets = [t for t in dict.fromkeys(targets) if t][:4]
                if targets:
                    await researcher.gather(ctx, targets, targeted=True, round_=rnd)
            feedback = fixable
            reviser = coder if ("coder" in routes and gen is coder) else gen
            new_draft = await reviser.draft(ctx, feedback=feedback, previous=draft, round_=rnd + 1)
            diff = _claim_diff(draft, new_draft)
            ctx.revisions.append({"round": rnd + 1, "author": new_draft["author"], "reason": [f"{i['category']}: {i['detail'][:120]}" for i in fixable],
                                  "diff": diff, "draft": new_draft})
            ctx.emit(reviser.name, "revise", f"Draft {rnd + 1}: -{len(diff['removed'])} / +{len(diff['added'])} claims"
                     + (", code changed" if diff["code_changed"] else "") + (", actions changed" if diff["actions_changed"] else ""),
                     {"diff": diff, "draft": new_draft}, round_=rnd + 1)
            if not any(diff.values()):
                ctx.emit("orchestrator", "route", "Revision made no progress -> stop and let finalizer decide", round_=rnd + 1, level="warn")
                draft = new_draft
                break
            draft = new_draft
        result = await finalizer.finalize(ctx, draft, report, critic_issues, rounds)
    except Exception as ex:  # never leave a run hanging
        ctx.emit("orchestrator", "error", f"Run failed: {ex!r}", level="error")
        result = {"status": "ERROR", "answer": "Internal error - see audit log.", "confidence": 0.0, "rejection_reasons": [repr(ex)]}
    return _finish(ctx, result, reports, t0, draft, rounds)


def _finish(ctx: RunContext, result: dict, reports: list[dict], t0: float, draft: dict | None = None, rounds: int = 0) -> dict:
    first = reports[0]["report"] if reports else None
    last = reports[-1]["report"] if reports else None
    all_issues = [i for r in reports for i in r["report"]["issues"] + r["critic"]]
    gen_metrics = {
        "rounds": rounds,
        "revisions": len(ctx.revisions),
        "first_draft_claims": len(first["claims"]) if first else 0,
        "first_draft_supported_ratio": _ratio(first),
        "final_supported_ratio": _ratio(last),
        "generator_self_confidence": (draft or {}).get("self_confidence"),
    }
    ver_metrics = {
        "checks_run": sum(len(c.get("checks", [])) for r in reports for c in r["report"]["claims"])
        + sum(1 for r in reports if r["report"].get("code")) + sum(len(r["report"].get("actions", [])) for r in reports),
        "issues_found": len(all_issues),
        "issues_by_category": _count(i["category"] for i in all_issues),
        "evidence_items": len(ctx.evidence),
        "quarantined_sources": sum("prompt_injection" in e["flags"] for e in ctx.evidence.values()),
        "source_conflicts": len(ctx.conflicts),
        "injected_faults": len(ctx.injected_faults),
        "injected_faults_detected": sum(bool(f.get("detected")) for f in ctx.injected_faults),
    }
    llm_calls = ctx.llm.calls
    record = {
        "id": ctx.id,
        "task": ctx.task,
        "options": ctx.options,
        "mode": settings.mode,
        "models": {"generator": settings.generator_model, "verifier": settings.verifier_model} if settings.llm_enabled else None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ctx.started)),
        "duration_s": round(time.time() - t0, 2),
        "plan": ctx.plan,
        "safety": ctx.safety,
        "result": result,
        "evidence": list(ctx.evidence.values()),
        "conflicts": ctx.conflicts,
        "verification_rounds": reports,
        "revisions": ctx.revisions,
        "injected_faults": ctx.injected_faults,
        "events": ctx.events,
        "metrics": {"generation": gen_metrics, "verification": ver_metrics,
                    "llm_calls": {"total": len(llm_calls), "by_role": _count(c["role"] for c in llm_calls),
                                  "failed": sum(not c["ok"] for c in llm_calls)}},
        "user_docs": ctx.user_docs,
    }
    ctx.emit("orchestrator", "done", f"Finished: {result['status']} in {record['duration_s']}s")
    record["events"] = ctx.events
    ctx.finish(record)
    return record


def _ratio(rep: dict | None):
    if not rep or not rep["claims"]:
        return None
    return round(sum(c["verdict"] in ("SUPPORTED", "DISCLOSED_CONFLICT") for c in rep["claims"]) / len(rep["claims"]), 3)


def _count(it) -> dict:
    out: dict = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out
