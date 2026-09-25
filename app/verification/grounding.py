"""Deterministic grounding: does the evidence support, contradict, or say
nothing about a claim?

Signals used:
  * lexical coverage of the claim's content words by the passage (+ its title)
  * quantity agreement: every number/date/percentage in the claim must be
    present in the passage (with rounding tolerance); an *aligned* different
    quantity of the same kind is a contradiction signal
  * polarity: negation mismatch or antonyms between claim and passage
  * named entities in the claim must appear in the passage/title/doc entity
  * source weighting: reliability, superseded documents, quarantined
    (prompt-injection) documents
"""
from __future__ import annotations

import math
import re

from ..textutil import (antonym_conflict, capitalized_entities, content_tokens, extract_quantities, has_negation,
                        split_sentences, tokens)

SUPPORT_COVERAGE = 0.6
CONTRA_COVERAGE = 0.5
MIN_SUPPORT_RELIABILITY = 0.5


def effective_reliability(ev: dict) -> float:
    r = float(ev.get("reliability", 0.5))
    flags = ev.get("flags", [])
    if "prompt_injection" in flags:
        return 0.0
    if "superseded" in flags:
        r *= 0.5
    return r


_UNIT_WORDS = {"inr", "crore", "lakh", "million", "billion", "thousand", "usd", "rs", "percent", "per", "cent", "fy", "about", "approximately"}


def _aligned(qc, claim_text: str, qp, passage: str) -> bool:
    """Are two quantities talking about the same thing? Compare the words that
    follow/precede each number (e.g. '3,400 employees' vs '4,100 employees')."""
    def ctx(raw: str, text: str) -> set:
        i = text.lower().find(raw.lower())
        if i < 0:
            return set()
        def clean(ts):  # drop numbers, fiscal-year ids, currency and scale words: they say nothing about *what* is counted
            return [t for t in ts if not re.search(r"\d", t) and t not in _UNIT_WORDS]
        before = clean(tokens(text[max(0, i - 40):i]))[-2:]
        after = clean(tokens(text[i + len(raw): i + len(raw) + 40]))[:2]
        return set(before + after)
    a, b = ctx(qc.raw, claim_text), ctx(qp.raw, passage)
    if qc.kind in ("date", "year") and qp.kind in ("date", "year"):
        return True  # dates for the same event/attribute - topical coverage already checked
    return bool(a & b)


_IDF: dict | None = None
_IDF_MAX = 5.0
RARE_IDF = 3.0


def _idf() -> dict:
    """Inverse document frequency of terms in the bundled knowledge base.
    Rare, informative words (e.g. 'crash', 'ceo') weigh more than common ones."""
    global _IDF, _IDF_MAX
    if _IDF is None:
        from ..kb.retrieval import default_kb
        kb = default_kb()
        _IDF = dict(kb._index.idf)
        n = max(kb._index.N, 1)
        _IDF_MAX = math.log(1 + (n + 0.5) / 0.5)
    return _IDF


def term_weight(t: str) -> float:
    return _idf().get(t, _IDF_MAX)


def assess(claim: str, passage_text: str, title: str = "", entity: str = "") -> dict:
    """Compare one claim with one passage."""
    c_toks = set(content_tokens(claim))
    p_toks = set(content_tokens(passage_text + " " + title + " " + (entity or "")))
    total_w = sum(term_weight(t) for t in c_toks) or 1.0
    matched_toks = {t for t in c_toks if t in p_toks or (len(t) >= 4 and any(
        (p.startswith(t) or t.startswith(p)) and len(p) >= 4 for p in p_toks))}
    coverage = sum(term_weight(t) for t in matched_toks) / total_w
    missing_terms = sorted(c_toks - matched_toks)
    missing_rare = [t for t in missing_terms if term_weight(t) >= RARE_IDF]

    cq = extract_quantities(claim)
    pq = extract_quantities(passage_text)
    matched, unmatched, conflicts = [], [], []
    for q in cq:
        if any(q.matches(p) for p in pq):
            matched.append(q.raw)
        else:
            unmatched.append(q.raw)
            same_kind = [p for p in pq if q.same_kind(p) and _aligned(q, claim, p, passage_text)]
            if same_kind:
                conflicts.append(f"claim says {q.raw!r}, source says {', '.join(repr(p.raw) for p in same_kind[:2])}")

    ents = capitalized_entities(claim)
    hay = (passage_text + " " + title + " " + (entity or "")).lower()
    missing_ents = sorted(e for e in ents if e not in hay and e.rstrip("s") not in hay)

    neg_mismatch = has_negation(claim) != has_negation(passage_text)
    antonym = antonym_conflict(claim, passage_text)

    label, reason = "neutral", ""
    time_kinds = {"date", "year"}
    time_conflict = any(q.kind in time_kinds for q in cq if q.raw in unmatched) and any(p.kind in time_kinds for p in pq)
    other_q = [q for q in cq if q.kind not in time_kinds]
    if conflicts and time_conflict and other_q:
        # e.g. claim about FY2019 revenue vs a source about FY2023 revenue: a different
        # period, so the source is silent about the claim rather than contradicting it
        return {"label": "neutral", "coverage": round(coverage, 3), "matched_quantities": matched,
                "unmatched_quantities": unmatched, "quantity_conflicts": [], "missing_entities": missing_ents,
                "negation_mismatch": neg_mismatch, "antonym": antonym,
                "reason": "source refers to a different time period"}
    if coverage >= SUPPORT_COVERAGE and not unmatched and not missing_ents and not neg_mismatch and not antonym and not missing_rare:
        label, reason = "support", "content words, quantities and entities all found in source"
    elif coverage >= CONTRA_COVERAGE and conflicts:
        label, reason = "contradict", "; ".join(conflicts)
    elif coverage >= SUPPORT_COVERAGE and neg_mismatch and not unmatched:
        label, reason = "contradict", "polarity mismatch (negation) with source"
    elif coverage >= CONTRA_COVERAGE and antonym and not unmatched:
        label, reason = "contradict", f"opposing terms {antonym}"
    elif coverage >= 0.4 and not conflicts:
        reason = (f"informative terms {missing_rare} not in source; " if missing_rare else "") + f"partial overlap; missing terms {missing_terms[:5]}" + (f", unmatched quantities {unmatched}" if unmatched else "")
    return {
        "label": label,
        "coverage": round(coverage, 3),
        "matched_quantities": matched,
        "unmatched_quantities": unmatched,
        "quantity_conflicts": conflicts,
        "missing_entities": missing_ents,
        "missing_rare_terms": missing_rare,
        "negation_mismatch": neg_mismatch,
        "antonym": antonym,
        "reason": reason,
    }


def ground_claim(claim: str, evidence: list[dict], documents: dict | None = None) -> dict:
    """Assess a claim against a pool of evidence passages.

    evidence: evidence dicts (id, doc_id, text, title, reliability, flags ...)
    documents: optional doc_id -> full document, used to also test 2-sentence
               windows (claims that combine adjacent sentences).
    """
    supports, contradicts, quarantined_hits, assessments = [], [], [], []
    seen_docs = set()
    for ev in evidence:
        units = [(ev["text"], ev)]
        if documents and ev["doc_id"] in documents and ev["doc_id"] not in seen_docs:
            seen_docs.add(ev["doc_id"])
            sents = split_sentences(documents[ev["doc_id"]]["text"])
            units += [(sents[i] + " " + sents[i + 1], ev) for i in range(len(sents) - 1)]
            units += [(s, ev) for s in sents if s != ev["text"]]
        for text, e in units:
            a = assess(claim, text, e.get("title", ""), e.get("entity", ""))
            if a["label"] == "neutral":
                continue
            rec = {"evidence_id": e["id"], "doc_id": e["doc_id"], "text": text, "reliability": e.get("reliability", 0.5),
                   "effective_reliability": round(effective_reliability(e), 3), "flags": e.get("flags", []), **a}
            if "prompt_injection" in e.get("flags", []):
                quarantined_hits.append(rec)
                continue
            (supports if a["label"] == "support" else contradicts).append(rec)
            assessments.append(rec)

    def best(lst):
        return max(lst, key=lambda r: (r["effective_reliability"], r["coverage"])) if lst else None

    bs, bc = best(supports), best(contradicts)
    s_rel = bs["effective_reliability"] if bs else 0.0
    c_rel = bc["effective_reliability"] if bc else 0.0
    support_docs = {r["doc_id"] for r in supports if r["effective_reliability"] >= MIN_SUPPORT_RELIABILITY}

    valid_s = bool(bs) and s_rel >= MIN_SUPPORT_RELIABILITY
    valid_c = bool(bc) and c_rel >= MIN_SUPPORT_RELIABILITY
    note = ""
    if valid_s and valid_c:
        if bc["doc_id"] in support_docs:
            decision = "SUPPORTED"  # the same document supports it; the 'contradiction' is about another attribute
        elif s_rel - c_rel >= 0.35:
            decision, note = "SUPPORTED", "outweighs a less reliable contradicting source"
        elif c_rel - s_rel >= 0.35:
            decision, note = "CONTRADICTED", "a much more reliable source contradicts it"
        elif bs["coverage"] >= bc["coverage"] + 0.25:
            decision, note = "SUPPORTED", "contradicting passage is only loosely related"
        else:
            decision = "CONFLICTING"
    elif valid_s:
        decision = "SUPPORTED"
    elif valid_c:
        decision = "CONTRADICTED"
    else:
        decision = "UNSUPPORTED"
        if bs:
            note = "only low-reliability or quarantined sources support it"

    if decision == "SUPPORTED":
        conf = min(0.99, s_rel * (0.75 + 0.25 * bs["coverage"]) + 0.04 * (len(support_docs) - 1))
        if valid_c:
            conf *= 0.8
    elif decision == "CONTRADICTED":
        conf = min(0.99, c_rel * (0.75 + 0.25 * bc["coverage"]))
    elif decision == "CONFLICTING":
        conf = 0.4
    else:
        conf = 0.3 if bs else 0.2
    verdict = decision

    return {
        "verdict": verdict,
        "confidence": round(conf, 3),
        "note": note,
        "best_support": _slim(bs),
        "best_contradiction": _slim(bc),
        "supporting_evidence": sorted({r["evidence_id"] for r in supports}),
        "contradicting_evidence": sorted({r["evidence_id"] for r in contradicts}),
        "quarantined_matches": [_slim(r) for r in quarantined_hits[:2]],
    }


def _slim(r):
    if not r:
        return None
    keys = ("evidence_id", "doc_id", "text", "reliability", "effective_reliability", "coverage", "reason", "quantity_conflicts", "flags")
    return {k: r[k] for k in keys if k in r}


def detect_source_conflicts(evidence: list[dict], documents: dict | None = None) -> list[dict]:
    """Find pairs of passages from different documents that disagree about the
    same thing, and try to resolve them (supersedes > reliability > recency)."""
    conflicts, seen = [], set()
    usable = [e for e in evidence if "prompt_injection" not in e.get("flags", [])]
    for i, a in enumerate(usable):
        for b in usable[i + 1:]:
            if a["doc_id"] == b["doc_id"]:
                continue
            key = tuple(sorted((a["doc_id"], b["doc_id"])))
            ra = assess(a["text"], b["text"], b.get("title", ""), b.get("entity", ""))
            rb = assess(b["text"], a["text"], a.get("title", ""), a.get("entity", ""))
            if ra["label"] != "contradict" and rb["label"] != "contradict":
                continue
            detail = ra["reason"] if ra["label"] == "contradict" else rb["reason"]
            if (key, detail) in seen:
                continue
            seen.add((key, detail))
            conflicts.append({"a": a["id"], "b": b["id"], "doc_a": a["doc_id"], "doc_b": b["doc_id"],
                              "detail": detail, **_resolve(a, b, documents)})
    return conflicts


def _resolve(a: dict, b: dict, documents: dict | None) -> dict:
    docs = documents or {}
    sup_a = set(docs.get(a["doc_id"], {}).get("supersedes", []) or [])
    sup_b = set(docs.get(b["doc_id"], {}).get("supersedes", []) or [])
    if b["doc_id"] in sup_a:
        return {"resolution": "resolved", "preferred": a["id"], "rule": f"{a['doc_id']} explicitly supersedes {b['doc_id']}"}
    if a["doc_id"] in sup_b:
        return {"resolution": "resolved", "preferred": b["id"], "rule": f"{b['doc_id']} explicitly supersedes {a['doc_id']}"}
    ra, rb = effective_reliability(a), effective_reliability(b)
    hi, lo = (a, b) if ra >= rb else (b, a)
    newer_is_lower = (lo.get("date") or "") > (hi.get("date") or "")
    if abs(ra - rb) >= 0.25 and not newer_is_lower:
        return {"resolution": "resolved", "preferred": hi["id"], "rule": f"higher source reliability ({max(ra, rb):.2f} vs {min(ra, rb):.2f})"}
    if abs(ra - rb) >= 0.5:
        return {"resolution": "resolved", "preferred": hi["id"], "rule": f"much higher source reliability ({max(ra, rb):.2f} vs {min(ra, rb):.2f})"}
    return {"resolution": "unresolved", "preferred": hi["id"],
            "rule": "sources of comparable standing disagree" + (" (the less reliable source is newer)" if newer_is_lower else "")}


def normalize_quote(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip(" .\"'")
