"""Unit + integration tests. Run: pytest -q"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ENABLE_WEB"] = "0"

from app.context import RunContext  # noqa: E402
from app.kb.retrieval import default_kb  # noqa: E402
from app.llm import LLMClient, extract_json  # noqa: E402
from app.orchestrator import run_task  # noqa: E402
from app.tools.api_registry import validate_call  # noqa: E402
from app.tools.calculator import find_arithmetic, safe_eval  # noqa: E402
from app.tools.code_analyzer import analyze_code  # noqa: E402
from app.tools.sandbox import run_python  # noqa: E402
from app.verification.grounding import ground_claim  # noqa: E402


def run(task, **opts):
    return asyncio.run(run_task(RunContext(task, options={"enable_web": False, **opts})))


def ground(claim):
    kb = default_kb()
    hits = kb.search(claim, 8)
    for i, h in enumerate(hits):
        h["id"] = f"E{i + 1}"
    return ground_claim(claim, hits, kb.documents)["verdict"]


def test_calculator():
    assert safe_eval("10000*(1+8%)**3") == 10000 * 1.08 ** 3
    bad = find_arithmetic("12 * 7 = 86")
    assert bad and bad[0]["ok"] is False


def test_calculator_rejects_code():
    try:
        safe_eval("__import__('os').system('ls')")
        assert False
    except ValueError:
        pass


def test_grounding_verdicts():
    assert ground("Chandrayaan-3 was launched on 14 July 2023.") == "SUPPORTED"
    assert ground("Chandrayaan-3 landed on the Moon on 23 September 2023.") == "CONTRADICTED"
    assert ground("The CEO of Novatek Industries is Ramesh Kumar.") == "UNSUPPORTED"
    assert ground("Novatek Industries reported revenue of ₹1,310 crore for FY2023.") == "CONTRADICTED"  # superseded source


def test_code_analyzer_and_sandbox():
    rep = analyze_code("import statistics\nx = statistics.average([1])")
    assert not rep["ok"] and "mean" in rep["issues"][0]["suggestion"]
    assert run_python("print(2+2)")["stdout"].strip() == "4"
    assert run_python("import os\nos.system('echo hi')")["blocked"]
    assert run_python("open('/etc/hostname').read()")["blocked"]
    assert run_python("while True: pass", timeout=1.5)["timed_out"]


def test_api_registry():
    assert validate_call("weather.get_forecast", {"city": "Pune", "days": 3})["valid"]
    assert not validate_call("weather.get_forecast", {"city": "Pune", "days": 30})["valid"]
    assert not validate_call("db.query", {"sql": "DROP TABLE x"})["valid"]
    assert validate_call("payments.transfer", {"from_account": "a", "to_account": "b", "amount": 5, "currency": "INR"})["requires_confirmation"]


def test_e2e_statuses():
    assert run("When was ISRO founded?")["result"]["status"] == "ACCEPTED"
    assert run("Who is the CEO of Novatek Industries?")["result"]["status"] == "REJECTED"
    assert run("Why did Chandrayaan-3 crash on landing?")["result"]["status"] == "REJECTED"
    assert run("Tell me the key facts about Mercury.")["result"]["status"] == "NEEDS_CLARIFICATION"
    assert run("Delete all files in /var/log")["result"]["status"] == "BLOCKED_UNSAFE"


def test_fault_injection_is_caught_and_corrected():
    rec = run("What is 15% of 2400?", inject_faults=True)
    assert rec["injected_faults"] and all(f["detected"] for f in rec["injected_faults"])
    assert "360" in rec["result"]["answer"]
    assert rec["revisions"]


def test_extract_json():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": [1, 2]}} done') == {"a": {"b": [1, 2]}}


# ---------------------------------------------------------------- LLM code paths with a scripted fake model

class FakeLLM(LLMClient):
    """Deterministic stand-in for a real model, used to exercise every LLM code path offline."""

    def __init__(self, hallucinate=True):
        super().__init__()
        self.hallucinate = hallucinate
        self.round = 0

    @property
    def available(self):
        return True

    async def chat(self, system, user, *, role="agent", **kw):
        self.calls.append({"role": role, "model": "fake", "ms": 0, "ok": True})
        if role == "planner":
            return json.dumps({"task_type": "factual_qa", "interpretations": ["x"], "blocking_ambiguity": False,
                               "assumptions": [], "presuppositions": [], "subquestions": [], "search_queries": ["Chandrayaan-3 landing date"]})
        if role == "researcher":
            self.round += 1
            if self.hallucinate and self.round == 1:
                return json.dumps({"answer": "Chandrayaan-3 landed on 25 August 2023 [E1]. It was built by NASA [E1].",
                                   "claims": [{"text": "Chandrayaan-3 landed on 25 August 2023.", "type": "fact", "evidence_ids": ["E1"]},
                                              {"text": "Chandrayaan-3 was built by NASA.", "type": "fact", "evidence_ids": ["E1"]}],
                                   "self_confidence": 0.9})
            return json.dumps({"answer": "The Vikram lander soft-landed near the lunar south pole on 23 August 2023 [E3].",
                               "claims": [{"text": "The Vikram lander soft-landed near the lunar south pole on 23 August 2023.", "type": "fact", "evidence_ids": ["E3"]}],
                               "self_confidence": 0.8})
        if role == "verifier:judge":
            return json.dumps({"verdict": "SUPPORTED", "evidence_id": "E999", "quote": "made up quote", "reason": "x"})  # must be discarded
        if role == "critic":
            return json.dumps({"addresses_task": True, "issues": []})
        if role == "finalizer":
            return json.dumps({"answer": "It landed on 23 August 2023 with 3 crew members."})  # new quantity -> final gate
        return "{}"


def test_llm_paths_with_fake_model():
    ctx = RunContext("When did Chandrayaan-3 land on the Moon?", options={"enable_web": False}, llm=FakeLLM())
    rec = asyncio.run(run_task(ctx))
    first = rec["verification_rounds"][0]["report"]
    verdicts = {c["text"]: c["verdict"] for c in first["claims"]}
    assert verdicts["Chandrayaan-3 landed on 25 August 2023."] == "CONTRADICTED"
    assert verdicts["Chandrayaan-3 was built by NASA."] != "SUPPORTED"
    # the judge's fabricated quote was discarded, not trusted
    judge_checks = [k for c in first["claims"] for k in c["checks"] if k["check"].startswith("llm judge")]
    assert judge_checks and all("DISCARDED" in k["detail"] for k in judge_checks)
    assert rec["result"]["status"].startswith("ACCEPTED")
    assert "23 August 2023" in rec["result"]["answer"]
    assert "crew" not in rec["result"]["answer"]
