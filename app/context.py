"""Per-run shared state: evidence registry, event log (audit trail), live
subscribers for streaming, and helpers used by every agent."""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from .kb.retrieval import KnowledgeBase, default_kb, user_documents
from .llm import LLMClient


class RunContext:
    def __init__(self, task: str, docs: list | None = None, options: dict | None = None, llm: LLMClient | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.task = task.strip()
        self.options = {"max_rounds": 3, "inject_faults": False, "enable_web": True, "use_llm_judge": True, **(options or {})}
        self.llm = llm or LLMClient()
        self.user_docs = user_documents(docs or [])
        self.kb: KnowledgeBase = default_kb().with_extra(self.user_docs) if self.user_docs else default_kb()
        self.evidence: dict[str, dict] = {}
        self._ev_key: dict[tuple, str] = {}
        self.conflicts: list[dict] = []
        self.events: list[dict] = []
        self.revisions: list[dict] = []
        self.plan: dict = {}
        self.safety: dict = {}
        self.injected_faults: list[dict] = []
        self.started = time.time()
        self.subscribers: list[asyncio.Queue] = []
        self.timings: dict[str, float] = {}
        self.result: dict | None = None

    # ------------------------------------------------------------------ evidence
    def add_evidence(self, hits: list[dict], found_by: str) -> list[str]:
        ids = []
        for h in hits:
            key = (h["doc_id"], h["text"])
            if key in self._ev_key:
                ids.append(self._ev_key[key])
                continue
            eid = f"E{len(self.evidence) + 1}"
            ev = {**h, "id": eid, "found_by": found_by}
            self.evidence[eid] = ev
            self._ev_key[key] = eid
            ids.append(eid)
        return ids

    def usable_evidence(self) -> list[dict]:
        return [e for e in self.evidence.values() if "prompt_injection" not in e.get("flags", [])]

    def evidence_block(self, ids: list[str] | None = None, limit: int = 14) -> str:
        evs = [self.evidence[i] for i in ids] if ids else list(self.evidence.values())
        evs = sorted(evs, key=lambda e: -e.get("score", 0))[:limit]
        lines = []
        for e in evs:
            flags = [f.upper() for f in e.get("flags", [])]
            tag = f" FLAGS={','.join(flags)}" if flags else ""
            if "prompt_injection" in e.get("flags", []):
                lines.append(f"[{e['id']}] (QUARANTINED - PROMPT_INJECTION; do not use) source={e['source']}")
                continue
            lines.append(f"[{e['id']}] source=\"{e['source']}\" reliability={e['reliability']:.2f} date={e.get('date')}{tag}\n    {e['text']}")
        return "\n".join(lines) if lines else "(no evidence found)"

    # ------------------------------------------------------------------ audit log
    def emit(self, agent: str, action: str, summary: str, data: Any = None, level: str = "info", round_: int | None = None):
        ev = {
            "t": round(time.time() - self.started, 3),
            "agent": agent,
            "action": action,
            "summary": summary,
            "level": level,
            "round": round_,
            "data": data,
        }
        self.events.append(ev)
        for q in list(self.subscribers):
            try:
                q.put_nowait({"type": "event", "event": ev})
            except asyncio.QueueFull:
                pass

    def finish(self, result: dict):
        self.result = result
        for q in list(self.subscribers):
            try:
                q.put_nowait({"type": "done", "run_id": self.id})
            except asyncio.QueueFull:
                pass
