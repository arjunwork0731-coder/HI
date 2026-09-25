"""FastAPI application: REST + Server-Sent Events API and the audit dashboard."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import prompts
from .config import BASE_DIR, settings
from .context import RunContext
from .kb.retrieval import default_kb
from .orchestrator import run_task
from .store import get_run, list_runs, save_run
from .tools.api_registry import TOOLS

STATIC = BASE_DIR / "static"
EVAL_DIR = BASE_DIR / "eval"
MAX_CONCURRENT = 4

app = FastAPI(title="VeriMind - Multi-Agent Reasoning & Verification Engine", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ACTIVE: dict[str, RunContext] = {}
_sem = asyncio.Semaphore(MAX_CONCURRENT)


class Doc(BaseModel):
    title: str | None = None
    text: str = Field(..., max_length=20000)
    reliability: float | None = Field(None, ge=0, le=1)


class RunRequest(BaseModel):
    task: str = Field(..., min_length=3, max_length=2000)
    documents: list[Doc] = Field(default_factory=list, max_length=5)
    inject_faults: bool = False
    enable_web: bool = True
    use_llm_judge: bool = True
    max_rounds: int = Field(3, ge=1, le=5)


def _options(req: RunRequest) -> dict:
    return {"inject_faults": req.inject_faults, "enable_web": req.enable_web and settings.enable_web,
            "use_llm_judge": req.use_llm_judge, "max_rounds": req.max_rounds}


async def _execute(ctx: RunContext):
    async with _sem:
        record = await run_task(ctx)
    save_run(record)
    await asyncio.sleep(30)  # keep in memory briefly for late stream subscribers
    ACTIVE.pop(ctx.id, None)


@app.get("/api/health")
async def health():
    kb = default_kb()
    return {"status": "ok", "mode": settings.mode, "provider": settings.provider if settings.llm_enabled else None,
            "generator_model": settings.generator_model if settings.llm_enabled else None,
            "verifier_model": settings.verifier_model if settings.llm_enabled else None,
            "web_retrieval": settings.enable_web, "kb_documents": len(kb.documents), "kb_passages": len(kb.passages),
            "max_rounds": settings.max_rounds, "accept_threshold": settings.accept_threshold}


@app.post("/api/runs")
async def start_run(req: RunRequest):
    docs = [d.model_dump() for d in req.documents]
    ctx = RunContext(req.task, docs=docs, options=_options(req))
    ACTIVE[ctx.id] = ctx
    asyncio.create_task(_execute(ctx))
    return {"run_id": ctx.id}


@app.post("/api/runs/sync")
async def run_sync(req: RunRequest):
    ctx = RunContext(req.task, docs=[d.model_dump() for d in req.documents], options=_options(req))
    async with _sem:
        record = await run_task(ctx)
    save_run(record)
    return record


@app.get("/api/runs")
async def runs(limit: int = 30):
    return list_runs(limit)


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str):
    ctx = ACTIVE.get(run_id)
    if ctx and ctx.result:
        return ctx.result
    if ctx:
        return {"id": run_id, "task": ctx.task, "running": True, "events": ctx.events}
    rec = get_run(run_id)
    if not rec:
        raise HTTPException(404, "run not found")
    return rec


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str, request: Request):
    ctx = ACTIVE.get(run_id)
    if ctx is None:
        rec = get_run(run_id)
        if not rec:
            raise HTTPException(404, "run not found")

        async def replay():
            for e in rec["events"]:
                yield f"data: {json.dumps({'type': 'event', 'event': e}, default=str)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'run_id': run_id})}\n\n"
        return StreamingResponse(replay(), media_type="text/event-stream")

    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    backlog = list(ctx.events)
    ctx.subscribers.append(q)

    async def gen():
        try:
            for e in backlog:
                yield f"data: {json.dumps({'type': 'event', 'event': e}, default=str)}\n\n"
            if ctx.result:
                yield f"data: {json.dumps({'type': 'done', 'run_id': run_id})}\n\n"
                return
            # backlog was snapshotted synchronously before subscribing, so the queue holds only newer events
            while True:
                if await request.is_disconnected():
                    return
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(msg, default=str)}\n\n"
                if msg["type"] == "done":
                    return
        finally:
            if q in ctx.subscribers:
                ctx.subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/scenarios")
async def scenarios():
    cases = json.loads((EVAL_DIR / "dataset.json").read_text(encoding="utf-8"))
    return [{k: c.get(k) for k in ("id", "category", "task", "documents", "expected_status", "notes")} for c in cases]


@app.get("/api/eval")
async def eval_results():
    p = EVAL_DIR / "results" / "latest.json"
    if not p.exists():
        return JSONResponse({"available": False})
    data = json.loads(p.read_text(encoding="utf-8"))
    data["available"] = True
    return data


@app.post("/api/eval/verifier")
async def eval_verifier_live():
    """Re-run the verifier benchmark live (deterministic path + LLM judge if configured)."""
    from eval.run_eval import run_verifier_bench
    t0 = time.time()
    res = await run_verifier_bench(use_llm_judge=True)
    res["seconds"] = round(time.time() - t0, 2)
    return res


@app.get("/api/kb")
async def kb_docs():
    return [{k: d.get(k) for k in ("id", "title", "source", "source_type", "reliability", "date", "entity", "supersedes", "text")}
            for d in default_kb().documents.values()]


@app.get("/api/tools")
async def tools():
    return TOOLS


@app.get("/api/prompts")
async def get_prompts():
    return {k: v for k, v in vars(prompts).items() if k.isupper()}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
