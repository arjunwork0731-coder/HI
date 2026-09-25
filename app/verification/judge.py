"""Secondary, model-based verification path: an LLM entailment judge.

Independence measures:
  * uses the (configurable) VERIFIER_MODEL, ideally a different model family
  * sees only the claim and raw evidence - never the generator's reasoning
  * its output is itself verified: the quoted span must exist verbatim in the
    cited passage, and the claim's numbers must appear in that passage;
    otherwise the judgement is discarded as a verifier hallucination.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from .. import prompts
from ..textutil import extract_quantities
from .grounding import normalize_quote


async def llm_judge(llm, claim: str, passages: list[dict]) -> dict:
    if not llm.available or not passages:
        return {"verdict": "SKIPPED", "reason": "LLM judge unavailable" if not llm.available else "no passages"}
    block = "\n".join(f"[{p['id']}] (reliability {p.get('reliability', 0.5):.2f}) {p['text']}" for p in passages[:5])
    try:
        out = await llm.chat_json(prompts.JUDGE, f"CLAIM:\n{claim}\n\nPASSAGES:\n{block}", role="verifier:judge",
                                  model=llm.cfg.verifier_model, temperature=0.0, max_tokens=400)
    except Exception as ex:
        return {"verdict": "ERROR", "reason": str(ex)[:200]}
    verdict = str(out.get("verdict", "")).upper().replace(" ", "_")
    if verdict not in {"SUPPORTED", "CONTRADICTED", "NOT_ENOUGH_INFO"}:
        return {"verdict": "ERROR", "reason": f"invalid verdict {verdict!r}"}
    eid = out.get("evidence_id")
    quote = out.get("quote") or ""
    res = {"verdict": verdict, "evidence_id": eid, "quote": quote, "reason": str(out.get("reason", ""))[:300], "quote_verified": None}
    if verdict in {"SUPPORTED", "CONTRADICTED"}:
        passage = next((p for p in passages if p["id"] == eid), None)
        ok = False
        if passage and quote:
            nq, np_ = normalize_quote(quote), normalize_quote(passage["text"])
            ok = nq in np_ or SequenceMatcher(None, nq, np_).find_longest_match(0, len(nq), 0, len(np_)).size >= 0.85 * len(nq)
        res["quote_verified"] = ok
        if not ok:
            res["original_verdict"] = verdict
            res["verdict"] = "DISCARDED"
            res["reason"] = "judge quote not found in cited passage (possible verifier hallucination)"
        elif verdict == "SUPPORTED":
            cq = extract_quantities(claim)
            pq = extract_quantities(passage["text"])
            missing = [q.raw for q in cq if not any(q.matches(p) for p in pq)]
            if missing:
                res["original_verdict"] = verdict
                res["verdict"] = "NOT_ENOUGH_INFO"
                res["reason"] = f"judge said SUPPORTED but quantities {missing} are not in the passage"
    return res
