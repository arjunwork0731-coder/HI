"""VERIFIER agent - independent verification of every important output.

It never trusts the generator: it re-extracts claims from the answer text
(catching claims the generator did not list), re-retrieves evidence on its
own, re-computes arithmetic, statically analyses and executes code in the
sandbox, validates tool calls against the registry, and checks the task's
presuppositions. Each claim gets a verdict from multiple independent paths:

  path A (deterministic): citation check, lexical/quantity/polarity grounding,
                          calculator, operand grounding, code analysis+sandbox
  path B (model-based):   LLM entailment judge on a separate verifier model,
                          whose own quotes are verified against the evidence
"""
from __future__ import annotations

import asyncio
import re

from ..context import RunContext
from ..textutil import content_tokens, extract_quantities, split_sentences
from ..tools.api_registry import execute_mock, validate_call
from ..tools.calculator import CalcError, find_arithmetic, numbers_close, safe_eval
from ..tools.code_analyzer import analyze_code
from ..tools.sandbox import run_python
from ..verification.grounding import effective_reliability, ground_claim
from ..verification.judge import llm_judge
from ..verification.safety import assess_output

META_PATTERNS = re.compile(
    r"\b(not (available|provided|found|contain|mention)|does not (contain|say|mention|provide)|no (information|evidence|data)|"
    r"insufficient|cannot (be )?(determine|answer|verify)|could not|unable to|I (can't|cannot|could not)|unknown|not (specified|stated)|"
    r"proposed \d+ tool call|revised:|implemented `)", re.I)
PREMISE_ADDRESSED = re.compile(r"\b(premise|did not|didn't|was not|wasn't|is not|isn't|no evidence|contradict|incorrect|false|myth|actually|in fact)\b", re.I)
SMALL_CONSTANTS = {0.0, 1.0, 2.0, 100.0, 12.0, 365.0, 1000.0}


class VerifierAgent:
    name = "verifier"

    async def verify(self, ctx: RunContext, draft: dict, round_: int) -> dict:
        issues: list[dict] = []
        ttype = ctx.plan.get("task_type", "factual_qa")
        claims = [dict(c) for c in draft["claims"]]
        if ttype not in ("code", "tool_action"):
            hidden = self._hidden_claims(draft, claims)
            if hidden:
                ctx.emit(self.name, "hidden_claims", f"Found {len(hidden)} checkable statement(s) in the answer that were not declared as claims",
                         [h["text"] for h in hidden], round_=round_)
            claims += hidden

        # --- per-claim verification (claims verified concurrently)
        results = await asyncio.gather(*[self._verify_claim(ctx, c) for c in claims if c["type"] != "code"])
        by_id = {r["id"]: r for r in results}

        # --- code
        code_report = None
        if draft.get("code"):
            code_report = self._verify_code(draft)
            issues += code_report["issues"]
        for c in claims:
            if c["type"] == "code":
                ok = bool(code_report and code_report["passed"])
                by_id[c["id"]] = {**c, "verdict": "SUPPORTED" if ok else "FAILED", "confidence": 0.9 if ok else 0.1,
                                  "checks": [{"check": "code_execution", "verdict": "pass" if ok else "fail",
                                              "detail": "static analysis + sandboxed tests" if ok else "code failed verification"}]}
        verified_claims = [by_id[c["id"]] for c in claims if c["id"] in by_id]

        # a conflicting claim is acceptable if the answer openly presents the other side too
        ans_q = extract_quantities(draft.get("answer", ""))
        for r in verified_claims:
            if r["verdict"] != "CONFLICTING":
                continue
            other_q = [q for e in r.get("contradicting_evidence", []) if e in ctx.evidence for q in extract_quantities(ctx.evidence[e]["text"])]
            if other_q and any(q.matches(a) or a.matches(q) for q in other_q for a in ans_q):
                r["verdict"] = "DISCLOSED_CONFLICT"
                r["confidence"] = 0.7
                r["checks"].append({"check": "conflict disclosure", "verdict": "pass",
                                    "detail": "sources disagree, and the answer presents both values"})

        for r in verified_claims:
            issues += self._claim_issues(r)

        # --- arithmetic statements anywhere in the answer
        for a in find_arithmetic(draft.get("answer", "")):
            if a["ok"] is False:
                issues.append({"severity": "major", "category": "calculation_error", "route_to": "coder",
                               "detail": f"'{a['span']}' is wrong: {a['expression']} = {a['actual']}", "hint": f"correct value is {a['actual']}"})

        # --- tool actions
        action_reports = []
        for a in draft.get("actions") or []:
            rep = validate_call(a["tool"], a.get("params"))
            rep["requested"] = a
            if not rep["valid"]:
                unsafe = [i for i in rep["issues"] if i["type"] in ("policy_violation", "data_exfiltration")]
                for i in rep["issues"]:
                    issues.append({"severity": "critical" if i in unsafe else "major",
                                   "category": "unsafe_action" if i in unsafe else "invalid_tool_call",
                                   "route_to": "finalizer" if i in unsafe else "coder", "action_tool": a["tool"],
                                   "detail": f"{a['tool']}: {i['detail']}", "hint": i.get("suggestion")})
                rep["status"] = "REJECTED"
            elif rep["requires_confirmation"]:
                rep["status"] = "HELD_FOR_APPROVAL"
                issues.append({"severity": "minor", "category": "requires_approval", "route_to": "finalizer", "action_tool": a["tool"],
                               "detail": f"{rep['tool']} is a {rep['risk']} action; it will not be executed without human confirmation"})
            else:
                rep["status"] = "EXECUTED (sandbox mock)"
                rep["result"] = execute_mock(rep["tool"], a.get("params") or {})
            action_reports.append(rep)

        # --- tools the user explicitly named must exist and must be the ones actually called
        if ttype == "tool_action":
            from ..tools.api_registry import TOOLS
            namespaces = {n.split(".")[0] for n in TOOLS}
            named = {m.group(0) for m in re.finditer(r"\b([a-z]+)\.[a-z_]+\b", ctx.task) if m.group(1) in namespaces}
            for n in sorted(named):
                if n not in TOOLS:
                    issues.append({"severity": "major", "category": "invalid_tool_call", "route_to": "finalizer",
                                   "detail": f"the task asks for '{n}', which is not a registered tool", "hint": "no such API exists"})
                elif not any(a.get("tool") == n for a in draft.get("actions") or []):
                    issues.append({"severity": "major", "category": "invalid_tool_call", "route_to": "coder",
                                   "detail": f"the task asks for '{n}' but the proposed calls use a different tool"})

        # --- presuppositions (false-premise detection)
        presup_reports = []
        for p in ctx.plan.get("presuppositions") or []:
            pr = await self._verify_claim(ctx, {"id": "P", "text": p, "type": "fact", "evidence_ids": []})
            presup_reports.append(pr)
            if pr["verdict"] == "CONTRADICTED":
                issues.append({"severity": "critical", "category": "false_premise", "route_to": "finalizer",
                               "detail": f"The question presupposes '{p}', which the evidence contradicts",
                               "evidence": pr.get("contradicting_evidence", []),
                               "addressed_in_answer": bool(PREMISE_ADDRESSED.search(draft.get("answer", "")))})

        # --- conflicts must be disclosed
        answer = draft.get("answer", "")
        used = {e for r in verified_claims for e in r.get("supporting_evidence", []) + r.get("evidence_ids", [])}
        for c in ctx.conflicts:
            if c["resolution"] != "unresolved" or not ({c["a"], c["b"]} & used):
                continue
            qa = [q.raw for q in extract_quantities(ctx.evidence[c["a"]]["text"])]
            qb = [q.raw for q in extract_quantities(ctx.evidence[c["b"]]["text"])]
            mentions_a = any(x in answer for x in qa) or c["a"] in answer
            mentions_b = any(x in answer for x in qb) or c["b"] in answer
            if not (mentions_a and mentions_b):
                issues.append({"severity": "major", "category": "undisclosed_conflict", "route_to": "researcher",
                               "detail": f"Sources {c['a']} and {c['b']} disagree ({c['detail']}) but the answer presents only one side",
                               "hint": "state both values with their sources and dates"})

        # --- dangerous content in output
        for d in assess_output(answer):
            issues.append({"severity": "critical", "category": "dangerous_output", "route_to": "finalizer", "detail": d["detail"]})
        code_text = (draft.get("code") or "") + "\n" + (draft.get("tests") or "")
        for d in assess_output(code_text):
            line = next((i for i, ln in enumerate(code_text.splitlines(), 1) if d["match"] in ln), None)
            issues.append({"severity": "critical", "category": "unsafe_code", "route_to": "coder",
                           "detail": (f"line {line}: " if line else "") + f"dangerous command in code: {d['detail']}"})

        # --- internal consistency between claims
        issues += self._consistency(verified_claims)

        passed = not any(i["severity"] in ("critical", "major") for i in issues if i["category"] != "false_premise" or not i.get("addressed_in_answer"))
        # a contradicted presupposition always blocks acceptance
        if any(i["category"] == "false_premise" for i in issues):
            passed = False
        stats = self._stats(verified_claims, issues)
        report = {"round": round_, "claims": verified_claims, "code": code_report, "actions": action_reports,
                  "presuppositions": presup_reports, "issues": issues, "passed": passed, "stats": stats}
        ctx.emit(self.name, "verification", self._summary(report), {"stats": stats, "issues": issues}, round_=round_,
                 level="info" if passed else "warn")
        return report

    # ------------------------------------------------------------------ claims
    def _hidden_claims(self, draft: dict, claims: list[dict]) -> list[dict]:
        out = []
        declared = [set(content_tokens(c["text"])) for c in claims]
        for s in split_sentences(draft.get("answer", "")):
            s_clean = re.sub(r"\[E\d+(?:\s*,\s*E\d+)*\]", "", s).strip()
            s_clean = re.sub(r"^(However|Also|Additionally|Moreover|In addition),?\s+(another source reports differently:\s*)?", "", s_clean, flags=re.I)
            if len(s_clean) < 12 or META_PATTERNS.search(s_clean):
                continue
            toks = set(content_tokens(s_clean))
            has_q = bool(extract_quantities(s_clean))
            if not toks or (len(toks) < 3 and not has_q):
                continue
            covered = any(len(toks & d) / max(len(toks), 1) >= 0.7 for d in declared)
            # quantities in the sentence must also appear in some declared claim
            if covered and has_q:
                claim_q = [q for c in claims for q in extract_quantities(c["text"] + " " + (c.get("result") or ""))]
                covered = all(any(q.matches(cq) or cq.matches(q) for cq in claim_q) for q in extract_quantities(s_clean))
            if not covered:
                cites = re.findall(r"E\d+", s)
                out.append({"id": f"H{len(out) + 1}", "text": s_clean, "type": "fact", "evidence_ids": cites, "hidden": True,
                            "expression": None, "result": None})
        return out

    async def _verify_claim(self, ctx: RunContext, c: dict) -> dict:
        res = {**c, "checks": [], "verdict": "UNVERIFIED", "confidence": 0.0}
        if c["type"] == "assumption":
            res.update(verdict="ASSUMPTION", confidence=0.5)
            res["checks"].append({"check": "assumption", "verdict": "warn", "detail": "stated assumption - surfaced to user, not verified"})
            return res

        # citation integrity
        cited = c.get("evidence_ids") or []
        missing = [e for e in cited if e not in ctx.evidence]
        quarantined = [e for e in cited if e in ctx.evidence and "prompt_injection" in ctx.evidence[e]["flags"]]
        if missing:
            res["checks"].append({"check": "citation", "verdict": "fail", "detail": f"cites non-existent evidence {missing} (fabricated citation)"})
        elif quarantined:
            res["checks"].append({"check": "citation", "verdict": "fail", "detail": f"cites quarantined (prompt-injection) evidence {quarantined}"})
        elif cited:
            res["checks"].append({"check": "citation", "verdict": "pass", "detail": f"cited evidence {cited} exists"})
        else:
            res["checks"].append({"check": "citation", "verdict": "warn", "detail": "no citation given"})

        if c["type"] == "calculation" or c.get("expression"):
            return self._verify_calculation(ctx, res)

        # path A: deterministic grounding over cited + independently retrieved evidence
        hits = ctx.kb.search(c["text"], k=6)
        indep = ctx.add_evidence(hits, found_by="verifier")
        pool_ids = list(dict.fromkeys([e for e in cited if e in ctx.evidence] + indep))
        pool = [ctx.evidence[i] for i in pool_ids]
        det = ground_claim(c["text"], pool, ctx.kb.documents)
        res["checks"].append({"check": "grounding (deterministic)", "verdict": _v2c(det["verdict"]), "detail": _det_detail(det),
                              "support": det["best_support"], "contradiction": det["best_contradiction"]})
        # path B: independent LLM judge
        judge = {"verdict": "SKIPPED"}
        if ctx.options.get("use_llm_judge", True) and ctx.llm.available:
            judge_pool = [e for e in pool if "prompt_injection" not in e["flags"]]
            judge_pool.sort(key=lambda e: -effective_reliability(e))
            judge = await llm_judge(ctx.llm, c["text"], judge_pool[:5])
            res["checks"].append({"check": "llm judge (independent model)", "verdict": _v2c(judge["verdict"]),
                                  "detail": f"{judge['verdict']}: {judge.get('reason', '')}" + (f" | quote: \"{judge.get('quote')}\"" if judge.get("quote") else "")})
        verdict, conf = self._combine(det, judge, ctx)
        if det["quarantined_matches"] and verdict != "SUPPORTED":
            res["checks"].append({"check": "manipulation", "verdict": "fail",
                                  "detail": "claim matches only a quarantined (prompt-injection) source"})
            res["injection_echo"] = True
        if missing or quarantined:
            conf *= 0.7
            if verdict == "SUPPORTED":
                res["citation_repair"] = det["supporting_evidence"]
        if cited and verdict == "SUPPORTED" and not set(cited) & set(det["supporting_evidence"]) and not missing:
            res["checks"].append({"check": "citation-support", "verdict": "warn",
                                  "detail": f"claim is supported by {det['supporting_evidence']} rather than the cited {cited}"})
        res.update(verdict=verdict, confidence=round(conf, 3), supporting_evidence=det["supporting_evidence"],
                   contradicting_evidence=det["contradicting_evidence"], note=det.get("note"))
        if verdict == "CONFLICTING" and c.get("disclosed_conflict"):
            res["verdict"] = "DISCLOSED_CONFLICT"
            res["confidence"] = 0.7
        return res

    def _combine(self, det: dict, judge: dict, ctx: RunContext) -> tuple[str, float]:
        dv, dc = det["verdict"], det["confidence"]
        jv = judge.get("verdict", "SKIPPED")
        if jv in ("SKIPPED", "ERROR", "DISCARDED"):
            return dv, dc
        if dv == "SUPPORTED":
            if jv == "SUPPORTED":
                return "SUPPORTED", min(0.99, dc + 0.05)
            if jv == "CONTRADICTED":
                return "UNCERTAIN", 0.45
            return "SUPPORTED", dc * 0.9
        if dv == "UNSUPPORTED":
            if jv == "SUPPORTED":
                ev = ctx.evidence.get(judge.get("evidence_id"), {})
                rel = effective_reliability(ev) if ev else 0.5
                if rel >= 0.5:
                    return "SUPPORTED", round(rel * 0.8, 3)  # paraphrase support, quote-verified
                return "UNSUPPORTED", 0.3
            if jv == "CONTRADICTED":
                return "CONTRADICTED", 0.7
            return "UNSUPPORTED", dc
        if dv == "CONTRADICTED":
            if jv == "SUPPORTED":
                return "UNCERTAIN", 0.4
            return "CONTRADICTED", min(0.99, dc + (0.05 if jv == "CONTRADICTED" else 0))
        return dv, dc  # CONFLICTING

    def _verify_calculation(self, ctx: RunContext, res: dict) -> dict:
        expr = res.get("expression")
        stated_raw = res.get("result")
        if not stated_raw:
            nums = re.findall(r"-?\d[\d,]*(?:\.\d+)?", res["text"].split("=")[-1] if "=" in res["text"] else res["text"])
            stated_raw = nums[-1] if nums else None
        if not expr:
            res.update(verdict="UNSUPPORTED", confidence=0.2)
            res["checks"].append({"check": "calculation", "verdict": "fail", "detail": "calculation claim has no checkable expression"})
            return res
        try:
            actual = safe_eval(expr)
        except CalcError as ex:
            res.update(verdict="INVALID", confidence=0.1)
            res["checks"].append({"check": "calculation", "verdict": "fail", "detail": f"expression not evaluable: {ex}"})
            return res
        stated = None
        try:
            stated = float(str(stated_raw).replace(",", "").rstrip("%")) if stated_raw is not None else None
        except ValueError:
            pass
        calc_ok = stated is not None and numbers_close(stated, actual, str(stated_raw).rstrip("%"))
        res["checks"].append({"check": "calculation (sandboxed calculator)", "verdict": "pass" if calc_ok else "fail",
                              "detail": f"{expr} = {round(actual, 6)}; stated {stated_raw}"})
        res["computed"] = round(actual, 6)
        # operand grounding: inputs must come from the task or reliable evidence
        operands = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", re.sub(r"\*\*\s*\d+", "", expr))]
        known = self._known_numbers(ctx)
        ungrounded = [o for o in operands if o not in SMALL_CONSTANTS and not any(abs(o - k) < 1e-9 for k in known)]
        ungrounded = list(dict.fromkeys(ungrounded))
        res["checks"].append({"check": "operand grounding", "verdict": "fail" if ungrounded else "pass",
                              "detail": f"inputs not found in task or evidence: {ungrounded}" if ungrounded else "all inputs traced to task or evidence"})
        if not calc_ok:
            res.update(verdict="CALC_ERROR", confidence=0.05)
        elif ungrounded:
            res.update(verdict="UNSUPPORTED", confidence=0.3, ungrounded_operands=ungrounded)
        else:
            res.update(verdict="SUPPORTED", confidence=0.95)
        return res

    @staticmethod
    def _known_numbers(ctx: RunContext) -> set[float]:
        nums: set[float] = set()
        texts = [ctx.task] + [e["text"] for e in ctx.usable_evidence() if effective_reliability(e) >= 0.5]
        for t in texts:
            for x in re.findall(r"\d[\d,]*(?:\.\d+)?", t):
                try:
                    v = float(x.replace(",", ""))
                except ValueError:
                    continue
                nums.update({v, round(v / 100, 10), round(1 + v / 100, 10)})  # 8% may appear as 0.08 or 1.08
        return nums

    def _claim_issues(self, r: dict) -> list[dict]:
        v, t = r["verdict"], r["text"]
        base = {"claim_id": r["id"], "claim_text": t}
        fabricated = any(ch["check"] == "citation" and ch["verdict"] == "fail" for ch in r.get("checks", []))
        out = []
        if fabricated and v != "SUPPORTED":
            out.append({**base, "severity": "major", "category": "fabricated_citation", "route_to": "researcher",
                        "detail": next(ch["detail"] for ch in r["checks"] if ch["check"] == "citation")})
        if r.get("injection_echo"):
            out.append({**base, "severity": "critical", "category": "injection_influence", "route_to": "researcher",
                        "detail": "claim repeats content that only appears in a quarantined prompt-injection source"})
        if v == "UNSUPPORTED":
            if r.get("ungrounded_operands"):
                out.append({**base, "severity": "major", "category": "ungrounded_calculation_input", "route_to": "researcher",
                            "detail": f"calculation uses numbers not found in any source: {r['ungrounded_operands']}"})
            else:
                out.append({**base, "severity": "major", "category": "unsupported_claim", "route_to": "researcher",
                            "detail": "no reliable evidence supports this claim (possible hallucination)" + (" [undeclared claim]" if r.get("hidden") else "")})
        elif v == "CONTRADICTED":
            out.append({**base, "severity": "major", "category": "contradicted_claim", "route_to": "researcher",
                        "detail": "reliable evidence contradicts this claim", "evidence": r.get("contradicting_evidence")})
        elif v == "CONFLICTING":
            out.append({**base, "severity": "major", "category": "conflicting_evidence", "route_to": "researcher",
                        "detail": "reliable sources disagree about this claim", "hint": "present both values and their sources"})
        elif v == "UNCERTAIN":
            out.append({**base, "severity": "minor", "category": "verifier_disagreement", "route_to": "researcher",
                        "detail": "deterministic and model-based verifiers disagree; confidence reduced"})
        elif v in ("CALC_ERROR", "INVALID"):
            out.append({**base, "severity": "major", "category": "calculation_error", "route_to": "coder",
                        "detail": f"stated result is wrong; recomputed value is {r.get('computed')}", "hint": f"use {r.get('computed')}"})
        return out

    def _verify_code(self, draft: dict) -> dict:
        code, tests = draft.get("code") or "", draft.get("tests") or ""
        analysis = analyze_code(code + "\n" + tests)
        issues = []
        for i in analysis["issues"]:
            if i["severity"] == "unsafe":
                issues.append({"severity": "critical", "category": "unsafe_code", "route_to": "coder", "detail": f"line {i['line']}: {i['detail']}",
                               "hint": "remove the dangerous operation"})
            elif i["severity"] == "error":
                cat = "invalid_api_usage" if i["type"] in ("invalid_api", "unknown_module", "undefined_name") else "code_error"
                issues.append({"severity": "major", "category": cat, "route_to": "coder", "detail": f"line {i['line']}: {i['detail']}",
                               "hint": i.get("suggestion")})
        run = None
        if not any(i["category"] == "unsafe_code" for i in issues):
            run = run_python(code, tests)
            if run["blocked"]:
                issues.append({"severity": "critical", "category": "unsafe_code", "route_to": "coder",
                               "detail": "sandbox blocked: " + "; ".join(run["blocked"][:3])})
            elif not run["ok"]:
                tail = (run["stderr"] or "").strip().splitlines()[-1:] or ["non-zero exit"]
                issues.append({"severity": "major", "category": "test_failure", "route_to": "coder",
                               "detail": f"sandboxed tests failed: {tail[0][:200]}", "hint": run["stderr"][-600:]})
        if not tests.strip():
            issues.append({"severity": "minor", "category": "no_tests", "route_to": "coder", "detail": "code has no tests; behaviour unverified"})
        passed = not any(i["severity"] in ("critical", "major") for i in issues)
        return {"analysis": analysis, "execution": run, "issues": issues, "passed": passed}

    def _consistency(self, claims: list[dict]) -> list[dict]:
        from ..verification.grounding import assess
        out = []
        ok = [c for c in claims if c["verdict"] in ("SUPPORTED", "DISCLOSED_CONFLICT") and c["type"] == "fact"]
        for i, a in enumerate(ok):
            for b in ok[i + 1:]:
                if a.get("disclosed_conflict") or b.get("disclosed_conflict"):
                    continue
                r = assess(a["text"], b["text"])
                if r["label"] == "contradict" and r["coverage"] >= 0.7:
                    out.append({"severity": "major", "category": "internal_inconsistency", "route_to": "researcher",
                                "claim_id": a["id"], "claim_text": a["text"], "detail": f"{a['id']} and {b['id']} contradict each other: {r['reason']}"})
        return out

    @staticmethod
    def _stats(claims: list[dict], issues: list[dict]) -> dict:
        from collections import Counter
        v = Counter(c["verdict"] for c in claims)
        return {"claims": len(claims), "verdicts": dict(v), "issues": dict(Counter(i["category"] for i in issues)),
                "critical": sum(i["severity"] == "critical" for i in issues), "major": sum(i["severity"] == "major" for i in issues)}

    @staticmethod
    def _summary(r: dict) -> str:
        s = r["stats"]
        v = ", ".join(f"{k}:{n}" for k, n in s["verdicts"].items()) or "no claims"
        state = "PASSED" if r["passed"] else f"FAILED ({s['critical']} critical, {s['major']} major)"
        return f"Round {r['round']} verification {state} | {v}"


def _v2c(v: str) -> str:
    return {"SUPPORTED": "pass", "CONTRADICTED": "fail", "UNSUPPORTED": "fail", "CONFLICTING": "warn", "NOT_ENOUGH_INFO": "fail",
            "SKIPPED": "skip", "ERROR": "skip", "DISCARDED": "warn"}.get(v, "warn")


def _det_detail(det: dict) -> str:
    s = f"{det['verdict']} (conf {det['confidence']})"
    if det["best_support"]:
        s += f"; best support {det['best_support']['evidence_id']} (coverage {det['best_support']['coverage']})"
    if det["best_contradiction"]:
        s += f"; contradiction {det['best_contradiction']['evidence_id']}: {det['best_contradiction'].get('reason', '')}"
    if det.get("note"):
        s += f"; {det['note']}"
    return s
