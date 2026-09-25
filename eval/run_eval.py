"""Evaluation harness.

Two separate measurements, as required ("measure verification quality
separately from generation quality"):

1. VERIFIER BENCHMARK  - fixed, labelled candidate outputs (facts, calculations,
   code, tool calls). Generation is out of the loop, so this isolates how well
   the verification layer detects hallucinations / errors / unsafe actions.
   Reported as precision / recall / F1 / false-rejection rate, plus an
   ablation (deterministic-only vs deterministic + LLM judge) when an LLM is set.

2. END-TO-END SUITE    - adversarial tasks (ambiguous, incomplete, conflicting,
   misleading, false-premise, invalid API, unsafe ...). Scores the final
   decision of the whole system and compares it with a generator-only
   baseline (the first draft shipped without verification). Run once clean
   and once with fault injection, which plants known errors into the first
   draft to measure detection + self-correction on the live pipeline.

Usage:
    python -m eval.run_eval                 # everything, web retrieval disabled for reproducibility
    python -m eval.run_eval --part verifier
    python -m eval.run_eval --part e2e --web
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents.verifier import VerifierAgent  # noqa: E402
from app.config import settings  # noqa: E402
from app.context import RunContext  # noqa: E402
from app.orchestrator import run_task  # noqa: E402
from app.tools.api_registry import validate_call  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"
ACCEPT = {"ACCEPTED", "ACCEPTED_WITH_CAVEATS"}


# ============================================================================ verifier benchmark

def _prf(tp, fp, fn, tn) -> dict:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3),
            "accuracy": round((tp + tn) / max(tp + fp + fn + tn, 1), 3), "false_rejection_rate": round(fp / max(fp + tn, 1), 3)}


async def run_verifier_bench(use_llm_judge: bool = True) -> dict:
    bench = json.loads((EVAL_DIR / "verifier_bench.json").read_text(encoding="utf-8"))
    v = VerifierAgent()
    items = []

    for f in bench["facts"]:
        ctx = RunContext(f["claim"], options={"enable_web": False, "use_llm_judge": use_llm_judge})
        r = await v._verify_claim(ctx, {"id": f["id"], "text": f["claim"], "type": "fact", "evidence_ids": []})
        flagged = r["verdict"] != "SUPPORTED"
        items.append({"id": f["id"], "kind": "fact", "label": f["label"], "category": f.get("category", "ok"), "expected": f["expected"],
                      "predicted": r["verdict"], "flagged": flagged, "confidence": r["confidence"], "claim": f["claim"],
                      "checks": [{"check": c["check"], "verdict": c["verdict"], "detail": c["detail"][:200]} for c in r["checks"]]})

    for c in bench["calculations"]:
        ctx = RunContext(c.get("task") or c["claim"], options={"enable_web": False, "use_llm_judge": use_llm_judge})
        ctx.add_evidence(ctx.kb.search("Novatek revenue FY2023 FY2022", k=6), found_by="bench")
        r = await v._verify_claim(ctx, {"id": c["id"], "text": c["claim"], "type": "calculation", "evidence_ids": [],
                                        "expression": c["expression"], "result": c["result"]})
        items.append({"id": c["id"], "kind": "calculation", "label": c["label"], "category": c.get("category", "ok"),
                      "predicted": r["verdict"], "flagged": r["verdict"] != "SUPPORTED", "claim": c["claim"],
                      "checks": [{"check": x["check"], "verdict": x["verdict"], "detail": x["detail"][:200]} for x in r["checks"]]})

    for k in bench["code"]:
        rep = v._verify_code({"code": k["code"], "tests": k["tests"]})
        items.append({"id": k["id"], "kind": "code", "label": k["label"], "category": k.get("category", "ok"),
                      "predicted": "PASS" if rep["passed"] else "FAIL", "flagged": not rep["passed"],
                      "checks": [{"check": i["category"], "verdict": "fail", "detail": i["detail"][:200]} for i in rep["issues"]]})

    for a in bench["actions"]:
        rep = validate_call(a["tool"], a["params"])
        status = "REJECTED" if not rep["valid"] else ("HELD_FOR_APPROVAL" if rep["requires_confirmation"] else "EXECUTED")
        items.append({"id": a["id"], "kind": "action", "label": a["label"], "category": a.get("category", "ok"),
                      "predicted": status, "flagged": status == "REJECTED", "expected_status": a.get("expected_status"),
                      "gating_ok": (status == a["expected_status"]) if a.get("expected_status") else None,
                      "checks": [{"check": i["type"], "verdict": "fail", "detail": i["detail"]} for i in rep["issues"]]})

    def score(sub):
        tp = sum(1 for i in sub if i["label"] == "bad" and i["flagged"])
        fn = sum(1 for i in sub if i["label"] == "bad" and not i["flagged"])
        fp = sum(1 for i in sub if i["label"] == "ok" and i["flagged"])
        tn = sum(1 for i in sub if i["label"] == "ok" and not i["flagged"])
        return _prf(tp, fp, fn, tn)

    by_kind = {k: score([i for i in items if i["kind"] == k]) for k in ("fact", "calculation", "code", "action")}
    cat = defaultdict(lambda: [0, 0])
    for i in items:
        if i["label"] == "bad":
            cat[f"{i['kind']}:{i['category']}"][1] += 1
            cat[f"{i['kind']}:{i['category']}"][0] += int(i["flagged"])
    facts = [i for i in items if i["kind"] == "fact"]
    fact_exact = sum(1 for i in facts if i["predicted"] == i["expected"]) / max(len(facts), 1)
    gating = [i for i in items if i["kind"] == "action" and i["gating_ok"] is not None]
    return {
        "overall": score(items),
        "by_kind": by_kind,
        "recall_by_category": {k: {"caught": c, "total": t, "recall": round(c / t, 3)} for k, (c, t) in sorted(cat.items())},
        "fact_verdict_exact_accuracy": round(fact_exact, 3),
        "fact_confusion": dict(Counter(f"{i['expected']}->{i['predicted']}" for i in facts)),
        "action_gating_accuracy": round(sum(i["gating_ok"] for i in gating) / max(len(gating), 1), 3),
        "llm_judge": bool(use_llm_judge and settings.llm_enabled),
        "items": items,
    }


# ============================================================================ end-to-end

def _text(res: dict) -> str:
    parts = [res.get("answer") or "", " ".join(res.get("caveats") or []), " ".join(res.get("rejection_reasons") or [])]
    return " ".join(parts).lower()


def _content_ok(case: dict, answer: str, full: str) -> tuple[bool, list[str]]:
    probs = []
    a, f = answer.lower(), full.lower()
    for s in case.get("must_include", []):
        if s.lower() not in f:
            probs.append(f"missing '{s}'")
    if case.get("must_include_any") and not any(s.lower() in f for s in case["must_include_any"]):
        probs.append(f"missing any of {case['must_include_any']}")
    for s in case.get("must_not_include", []):
        if s.lower() in a:
            probs.append(f"contains forbidden '{s}'")
    return not probs, probs


def _baseline(case: dict, record: dict) -> dict:
    """What a generator-only system (no verifier, no finalizer) would have shipped."""
    expected = set(case["expected_status"])
    first = None
    for e in record["events"]:
        if e["action"] == "draft" and isinstance(e.get("data"), dict):
            first = e["data"].get("draft")
            break
    if first is None:
        # blocked/clarified before generation: a generator-only system would have simply complied
        return {"pass": bool(expected & ACCEPT), "why": "no gate: would have complied"}
    if record["injected_faults"]:
        return {"pass": False, "why": "planted faults reach the user"}
    status = "ACCEPTED" if first.get("claims") or first.get("code") or first.get("actions") else "REJECTED"
    if first.get("actions"):
        return {"pass": "ACCEPTED" in expected, "why": "would execute tool calls without validation"}
    ok_content, probs = _content_ok(case, first.get("answer", ""), first.get("answer", ""))
    return {"pass": status in expected and ok_content, "why": "; ".join(probs) or status}


async def run_e2e(inject: bool, web: bool, dataset: str = "dataset.json") -> dict:
    cases = json.loads((EVAL_DIR / dataset).read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        ctx = RunContext(case["task"], docs=case.get("documents"), options={"enable_web": web, "inject_faults": inject})
        t0 = time.time()
        rec = await run_task(ctx)
        res = rec["result"]
        status_ok = res["status"] in case["expected_status"]
        content_ok, probs = _content_ok(case, res.get("answer", ""), _text(res)) if res["status"] in ACCEPT else (True, [])
        base = _baseline(case, rec)
        rows.append({
            "id": case["id"], "category": case["category"], "task": case["task"], "expected": case["expected_status"],
            "status": res["status"], "confidence": res.get("confidence"), "pass": status_ok and content_ok,
            "status_ok": status_ok, "content_problems": probs, "answer": (res.get("answer") or "")[:400],
            "rounds": rec["metrics"]["generation"]["rounds"], "first_draft_supported_ratio": rec["metrics"]["generation"]["first_draft_supported_ratio"],
            "final_supported_ratio": rec["metrics"]["generation"]["final_supported_ratio"],
            "issues_found": rec["metrics"]["verification"]["issues_found"], "checks_run": rec["metrics"]["verification"]["checks_run"],
            "faults": rec["injected_faults"], "baseline_pass": base["pass"], "baseline_note": base["why"],
            "false_accept": res["status"] in ACCEPT and not (set(case["expected_status"]) & ACCEPT),
            "false_reject": res["status"] not in ACCEPT and bool(set(case["expected_status"]) & ACCEPT) and not status_ok,
            "run_id": rec["id"], "seconds": round(time.time() - t0, 2),
        })
    n = len(rows)
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    faults = [f for r in rows for f in r["faults"]]
    fr = [r["first_draft_supported_ratio"] for r in rows if r["first_draft_supported_ratio"] is not None]
    return {
        "fault_injection": inject,
        "cases": n,
        "accuracy": round(sum(r["pass"] for r in rows) / n, 3),
        "baseline_accuracy_generator_only": round(sum(r["baseline_pass"] for r in rows) / n, 3),
        "false_accept_rate": round(sum(r["false_accept"] for r in rows) / max(sum(1 for r in rows if not set(r["expected"]) & ACCEPT), 1), 3),
        "false_reject_rate": round(sum(r["false_reject"] for r in rows) / max(sum(1 for r in rows if set(r["expected"]) & ACCEPT), 1), 3),
        "by_category": {c: {"n": len(v), "accuracy": round(sum(x["pass"] for x in v) / len(v), 3)} for c, v in sorted(by_cat.items())},
        "faults_injected": len(faults),
        "fault_detection_rate": round(sum(bool(f.get("detected")) for f in faults) / max(len(faults), 1), 3) if faults else None,
        "faults_by_type": {t: {"injected": sum(1 for f in faults if f["type"] == t), "detected": sum(1 for f in faults if f["type"] == t and f.get("detected"))}
                           for t in sorted({f["type"] for f in faults})},
        "generation": {
            "avg_first_draft_supported_ratio": round(sum(fr) / len(fr), 3) if fr else None,
            "runs_needing_revision": sum(1 for r in rows if r["rounds"] > 1),
            "avg_rounds": round(sum(r["rounds"] for r in rows) / n, 2),
        },
        "verification": {
            "avg_checks_per_run": round(sum(r["checks_run"] for r in rows) / n, 1),
            "avg_issues_per_run": round(sum(r["issues_found"] for r in rows) / n, 2),
        },
        "avg_seconds_per_task": round(sum(r["seconds"] for r in rows) / n, 2),
        "rows": rows,
    }


# ============================================================================ report

def write_report(results: dict, path: Path):
    L = [f"# VeriMind evaluation report", "", f"Generated {results['generated_at']} - mode **{results['mode']}**"
         + (f" (generator `{results['models']['generator']}`, verifier `{results['models']['verifier']}`)" if results.get("models") else ""), ""]
    vb = results.get("verifier")
    if vb:
        o = vb["overall"]
        L += ["## 1. Verification quality (verifier benchmark, generation out of the loop)", "",
              f"LLM judge used: **{vb['llm_judge']}**", "",
              "| scope | precision | recall | F1 | false-rejection rate | n |", "|---|---|---|---|---|---|",
              f"| **overall** | {o['precision']} | {o['recall']} | {o['f1']} | {o['false_rejection_rate']} | {o['tp'] + o['fp'] + o['fn'] + o['tn']} |"]
        for k, s in vb["by_kind"].items():
            L.append(f"| {k} | {s['precision']} | {s['recall']} | {s['f1']} | {s['false_rejection_rate']} | {s['tp'] + s['fp'] + s['fn'] + s['tn']} |")
        L += ["", f"Exact 3-way fact verdict accuracy (SUPPORTED / CONTRADICTED / UNSUPPORTED): **{vb['fact_verdict_exact_accuracy']}**  ",
              f"Tool-call approval gating accuracy: **{vb['action_gating_accuracy']}**", "", "Recall by error category:", "",
              "| category | caught / total |", "|---|---|"]
        L += [f"| {k} | {v['caught']} / {v['total']} |" for k, v in vb["recall_by_category"].items()]
        if results.get("verifier_det_only"):
            d = results["verifier_det_only"]["overall"]
            L += ["", f"Ablation - deterministic path only: precision {d['precision']}, recall {d['recall']}, F1 {d['f1']}, false-rejection {d['false_rejection_rate']}"]
        misses = [i for i in vb["items"] if (i["label"] == "bad") != i["flagged"]]
        if misses:
            L += ["", "Errors (for transparency):", ""]
            L += [f"- `{i['id']}` label={i['label']} predicted={i['predicted']}" + (f" - {i.get('claim', '')}" if i.get("claim") else "") for i in misses]
    for key, title in (("e2e_clean", "2. End-to-end decisions (clean generator)"), ("e2e_faults", "3. End-to-end with fault injection (red-team)"),
                       ("e2e_heldout", "4. Held-out suite (written after development - first run scored 12/14 before one generic fix; see README)")):
        e = results.get(key)
        if not e:
            continue
        L += ["", f"## {title}", "",
              f"- Decision accuracy: **{e['accuracy']}** vs generator-only baseline **{e['baseline_accuracy_generator_only']}** ({e['cases']} tasks)",
              f"- False-accept rate (unreliable/unsafe answer delivered): **{e['false_accept_rate']}**",
              f"- False-reject rate (good answer withheld): **{e['false_reject_rate']}**"]
        if e["faults_injected"]:
            L.append(f"- Injected faults detected: **{e['fault_detection_rate']}** ({e['faults_injected']} faults) - " +
                     ", ".join(f"{t}: {v['detected']}/{v['injected']}" for t, v in e["faults_by_type"].items()))
        g = e["generation"]
        L += [f"- Generation quality: first-draft claim support ratio {g['avg_first_draft_supported_ratio']}, "
              f"{g['runs_needing_revision']} runs needed revision, avg rounds {g['avg_rounds']}",
              f"- Verification effort: {e['verification']['avg_checks_per_run']} checks / run, {e['verification']['avg_issues_per_run']} issues / run", "",
              "| category | n | accuracy |", "|---|---|---|"]
        L += [f"| {c} | {v['n']} | {v['accuracy']} |" for c, v in e["by_category"].items()]
        L += ["", "| id | expected | got | pass | baseline |", "|---|---|---|---|---|"]
        L += [f"| {r['id']} | {'/'.join(r['expected'])} | {r['status']} | {'yes' if r['pass'] else 'NO'} | {'yes' if r['baseline_pass'] else 'no'} |" for r in e["rows"]]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["all", "verifier", "e2e"], default="all")
    ap.add_argument("--web", action="store_true", help="allow Wikipedia retrieval (less reproducible)")
    ap.add_argument("--out", default=str(RESULTS_DIR / "latest.json"))
    args = ap.parse_args()
    if not args.web:
        os.environ["ENABLE_WEB"] = "0"
    results = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": settings.mode,
               "models": {"generator": settings.generator_model, "verifier": settings.verifier_model} if settings.llm_enabled else None}
    if args.part in ("all", "verifier"):
        print("running verifier benchmark ...", flush=True)
        results["verifier"] = await run_verifier_bench(use_llm_judge=True)
        if settings.llm_enabled:
            results["verifier_det_only"] = await run_verifier_bench(use_llm_judge=False)
            results["verifier_det_only"].pop("items", None)
        print(json.dumps(results["verifier"]["overall"]), flush=True)
    if args.part in ("all", "e2e"):
        print("running end-to-end suite (clean) ...", flush=True)
        results["e2e_clean"] = await run_e2e(inject=False, web=args.web)
        print(f"  accuracy {results['e2e_clean']['accuracy']} (baseline {results['e2e_clean']['baseline_accuracy_generator_only']})", flush=True)
        print("running end-to-end suite (fault injection) ...", flush=True)
        results["e2e_faults"] = await run_e2e(inject=True, web=args.web)
        print(f"  accuracy {results['e2e_faults']['accuracy']} (baseline {results['e2e_faults']['baseline_accuracy_generator_only']}),"
              f" fault detection {results['e2e_faults']['fault_detection_rate']}", flush=True)
        print("running held-out suite (written after development) ...", flush=True)
        results["e2e_heldout"] = await run_e2e(inject=False, web=args.web, dataset="heldout.json")
        print(f"  accuracy {results['e2e_heldout']['accuracy']} (baseline {results['e2e_heldout']['baseline_accuracy_generator_only']})", flush=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    write_report(results, out.with_name("REPORT.md"))
    print(f"wrote {out} and {out.with_name('REPORT.md')}")


if __name__ == "__main__":
    asyncio.run(main())
