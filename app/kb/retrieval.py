"""Evidence retrieval: BM25 over sentence-level passages.

Sources are the bundled knowledge base, documents supplied by the user with a
task, and (optionally) Wikipedia. Every passage keeps source metadata
(reliability, date, supersedes) so the verifier can weigh evidence and
resolve conflicts.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from ..textutil import split_sentences, tokens

CORPUS_PATH = Path(__file__).with_name("corpus.json")

INJECTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts|messages)",
    r"disregard (all |any )?(the )?(previous|prior|above|system) (instructions|prompt)",
    r"(note|message|instruction)s? to (ai|llm|language model|assistant|chatbot)s?",
    r"\byou (must|should) (now )?(answer|respond|say|report|state)\b",
    r"reveal (your|the) (system )?prompt",
    r"\bsystem prompt\b",
    r"do not (mention|cite|use) .{0,40}(sources?|isro|official)",
    r"\bact as\b.{0,30}\b(admin|developer|jailbroken|dan)\b",
    r"\bnew instructions?:",
]
_INJ = [re.compile(p, re.I) for p in INJECTION_PATTERNS]


def scan_injection(text: str) -> list[str]:
    return [m.group(0) for p in _INJ for m in [p.search(text or "")] if m]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.N = len(docs)
        self.avgdl = sum(len(d) for d in docs) / max(self.N, 1)
        df: Counter = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}
        self.tf = [Counter(d) for d in docs]

    def score(self, q: list[str], i: int) -> float:
        tf, dl = self.tf[i], len(self.docs[i])
        s = 0.0
        for t in q:
            if t not in tf:
                continue
            f = tf[t]
            s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / max(self.avgdl, 1)))
        return s


class KnowledgeBase:
    """A searchable set of documents chunked into sentence passages."""

    def __init__(self, documents: list[dict]):
        self.documents = {d["id"]: d for d in documents}
        self.passages: list[dict] = []
        for d in documents:
            sents = split_sentences(d["text"])
            for i, s in enumerate(sents):
                self.passages.append({"doc_id": d["id"], "idx": i, "text": s})
        # index each sentence together with the doc title (helps entity matching)
        self._index = BM25([tokens(p["text"] + " " + self.documents[p["doc_id"]]["title"]) for p in self.passages])
        # flag superseded docs
        self.superseded_by: dict[str, str] = {}
        for d in documents:
            for old in d.get("supersedes", []) or []:
                self.superseded_by[old] = d["id"]

    @classmethod
    def load_default(cls) -> "KnowledgeBase":
        return cls(json.loads(CORPUS_PATH.read_text(encoding="utf-8")))

    def with_extra(self, extra_docs: list[dict]) -> "KnowledgeBase":
        return KnowledgeBase(list(self.documents.values()) + extra_docs)

    def search(self, query: str, k: int = 6, min_score: float = 0.5, exclude_docs: set | None = None) -> list[dict]:
        q = tokens(query)
        if not q:
            return []
        scored = []
        for i, p in enumerate(self.passages):
            if exclude_docs and p["doc_id"] in exclude_docs:
                continue
            s = self._index.score(q, i)
            if s >= min_score:
                scored.append((s, i))
        scored.sort(reverse=True)
        out = []
        for s, i in scored[:k]:
            p = self.passages[i]
            d = self.documents[p["doc_id"]]
            out.append(self._to_hit(p, d, s))
        return out

    def _to_hit(self, p: dict, d: dict, score: float) -> dict:
        flags = []
        inj = scan_injection(p["text"]) or scan_injection(d["text"])
        if inj:
            flags.append("prompt_injection")
        if p["doc_id"] in self.superseded_by:
            flags.append("superseded")
        if d.get("reliability", 0.5) < 0.5:
            flags.append("low_reliability")
        return {
            "doc_id": d["id"],
            "title": d["title"],
            "source": d.get("source", ""),
            "source_type": d.get("source_type", "unknown"),
            "reliability": float(d.get("reliability", 0.5)),
            "date": d.get("date"),
            "entity": d.get("entity"),
            "aliases": d.get("aliases", []),
            "text": p["text"],
            "score": round(score, 3),
            "flags": flags,
            "injection_matches": inj,
            "superseded_by": self.superseded_by.get(p["doc_id"]),
            "origin": d.get("origin", "kb"),
            "url": d.get("url"),
        }


def user_documents(docs: list[dict | str]) -> list[dict]:
    """Normalise documents attached to a task. They are untrusted by default."""
    out = []
    for i, d in enumerate(docs or []):
        if isinstance(d, str):
            d = {"text": d}
        text = (d.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "id": f"user-{i + 1}",
            "title": d.get("title") or f"User document {i + 1}",
            "source": "provided with task",
            "source_type": "user",
            "reliability": float(d.get("reliability", 0.5)),
            "date": d.get("date"),
            "entity": d.get("title") or "user document",
            "text": text,
            "origin": "user",
        })
    return out


_default_kb: KnowledgeBase | None = None


def default_kb() -> KnowledgeBase:
    global _default_kb
    if _default_kb is None:
        _default_kb = KnowledgeBase.load_default()
    return _default_kb
