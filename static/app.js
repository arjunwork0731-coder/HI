/* VeriMind dashboard - vanilla JS, no build step. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);
const api = async (path, opts) => { const r = await fetch(path, opts); if (!r.ok) throw new Error(`${r.status} ${await r.text()}`); return r.json(); };

let current = null; // current run record
let scenarios = [];

/* ------------------------------------------------------------------ tabs */
$$(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "history") loadHistory();
  if (name === "eval") loadEval();
  if (name === "arch") loadArch();
}

/* ------------------------------------------------------------------ init */
(async function init() {
  try {
    const h = await api("/api/health");
    const b = $("#modeBadge");
    if (h.mode === "llm") { b.className = "mode-badge llm"; b.textContent = `LLM mode · gen ${h.generator_model} · verifier ${h.verifier_model}`; }
    else { b.className = "mode-badge offline"; b.textContent = "Offline mode · deterministic agents (no API key)"; }
    if (!h.web_retrieval) { $("#optWeb").checked = false; $("#optWeb").disabled = true; }
  } catch (e) { $("#modeBadge").textContent = "API unreachable"; }
  try {
    scenarios = await api("/api/scenarios");
    const sel = $("#scenario");
    const groups = {};
    scenarios.forEach((s) => (groups[s.category] ||= []).push(s));
    Object.entries(groups).forEach(([cat, list]) => {
      const og = document.createElement("optgroup"); og.label = cat.replace("_", " ");
      list.forEach((s) => { const o = document.createElement("option"); o.value = s.id; o.textContent = s.task.slice(0, 90); og.appendChild(o); });
      sel.appendChild(og);
    });
  } catch (e) { /* ignore */ }
  const params = new URLSearchParams(location.search);
  if (params.get("run")) loadRun(params.get("run"));
})();

$("#scenario").addEventListener("change", (e) => {
  const s = scenarios.find((x) => x.id === e.target.value);
  if (!s) return;
  $("#task").value = s.task;
  const d = (s.documents || [])[0];
  $("#docTitle").value = d ? d.title || "" : ""; $("#docText").value = d ? d.text : "";
  $("#docsBox").open = !!d;
});
$("#task").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) startRun(); });
$("#runBtn").addEventListener("click", startRun);

/* ------------------------------------------------------------------ run */
async function startRun() {
  const task = $("#task").value.trim();
  if (task.length < 3) { $("#task").focus(); return; }
  const docs = $("#docText").value.trim() ? [{ title: $("#docTitle").value.trim() || null, text: $("#docText").value.trim() }] : [];
  const body = { task, documents: docs, inject_faults: $("#optFaults").checked, enable_web: $("#optWeb").checked,
                 use_llm_judge: $("#optJudge").checked, max_rounds: +$("#optRounds").value || 3 };
  resetView();
  $("#runBtn").disabled = true; $("#runBtn").textContent = "Agents working…";
  try {
    const { run_id } = await api("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    history.replaceState(null, "", `?run=${run_id}`);
    stream(run_id);
  } catch (e) {
    $("#runBtn").disabled = false; $("#runBtn").textContent = "Run with verification";
    alert("Could not start run: " + e.message);
  }
}

function resetView() {
  $("#empty").hidden = true; $("#decision").hidden = true;
  ["#timeline", "#claims", "#codeActions", "#evidence", "#conflicts", "#revisions", "#metrics", "#faults"].forEach((s) => ($(s).innerHTML = ""));
  $$(".stage").forEach((s) => (s.className = "stage"));
  $("#loopBadge").hidden = true; $("#roundSel").hidden = true;
  $("#evCount").textContent = ""; $("#evidCount").textContent = "";
}

function stream(runId) {
  const es = new EventSource(`/api/runs/${runId}/stream`);
  let n = 0;
  es.onmessage = async (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "event") { n++; addEvent(msg.event); $("#evCount").textContent = `${n} events`; }
    if (msg.type === "done") { es.close(); await loadRun(runId, true); }
  };
  es.onerror = async () => { es.close(); setTimeout(() => loadRun(runId, true), 800); };
}

const AGENT_STAGE = { planner: "planner", researcher: "researcher", coder: "coder", verifier: "verifier", critic: "critic", finalizer: "finalizer" };
function addEvent(e) {
  const li = document.createElement("li");
  li.className = e.level || "info";
  const data = e.data && Object.keys(e.data).length ? `<details><summary>details</summary><pre class="json">${esc(JSON.stringify(e.data, null, 2)).slice(0, 12000)}</pre></details>` : "";
  li.innerHTML = `<span class="t">${e.t.toFixed(2)}s${e.round ? ` · r${e.round}` : ""}</span><span class="ag">${esc(e.agent)}</span>${esc(e.summary)}${data}`;
  $("#timeline").appendChild(li);
  $("#timeline").scrollTop = $("#timeline").scrollHeight;
  const st = AGENT_STAGE[e.agent];
  if (st) {
    $$(".stage").forEach((s) => { if (s.classList.contains("active")) { s.classList.remove("active"); s.classList.add("done"); } });
    const el = $(`.stage[data-agent="${st}"]`); el.classList.add("active"); el.classList.remove("done");
    if (e.level === "warn" || e.level === "error") el.classList.add("warn");
  }
  if (e.action === "revise") { const lb = $("#loopBadge"); lb.hidden = false; lb.querySelector("span").textContent = String(+lb.querySelector("span").textContent + 1); }
}

async function loadRun(runId, fromStream = false) {
  try {
    let rec = await api(`/api/runs/${runId}`);
    let tries = 0;
    while (rec.running && tries++ < 60) { await new Promise((r) => setTimeout(r, 1000)); rec = await api(`/api/runs/${runId}`); }
    if (!fromStream) { resetView(); rec.events.forEach(addEvent); $("#evCount").textContent = `${rec.events.length} events`; $("#task").value = rec.task; }
    render(rec);
  } catch (e) { console.error(e); }
  $("#runBtn").disabled = false; $("#runBtn").textContent = "Run with verification";
}

/* ------------------------------------------------------------------ render */
function citeHtml(text) {
  return esc(text).replace(/\[((?:E\d+)(?:\s*,\s*E\d+)*)\]/g, (_, ids) =>
    ids.split(/\s*,\s*/).map((id) => `<span class="cite" data-ev="${id}">${id}</span>`).join(""));
}

function render(rec) {
  current = rec;
  $$(".stage").forEach((s) => { s.classList.remove("active"); s.classList.add("done"); });
  const res = rec.result || {};
  $("#empty").hidden = true; $("#decision").hidden = false;
  const pill = $("#statusPill"); pill.className = `pill ${res.status}`; pill.textContent = (res.status || "").replaceAll("_", " ");
  $("#confBar").style.width = pct(res.confidence); $("#confVal").textContent = pct(res.confidence);
  $("#runMeta").textContent = `run ${rec.id} · ${rec.mode} · ${rec.duration_s}s`;
  $("#answer").innerHTML = citeHtml(res.answer || "");
  let x = "";
  if (res.caveats?.length) x += `<div class="box warn"><b>Caveats</b><ul>${res.caveats.map((c) => `<li>${citeHtml(c)}</li>`).join("")}</ul></div>`;
  if (res.rejection_reasons?.length) x += `<div class="box bad"><b>${res.status === "NEEDS_CLARIFICATION" ? "Why clarification is needed" : "Reasons"}</b><ul>${res.rejection_reasons.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>`;
  if (res.removed_claims?.length) x += `<div class="box info"><b>Removed after failing verification</b><ul>${res.removed_claims.map((c) => `<li><span class="v ${c.verdict}">${c.verdict}</span> ${esc(c.text)} <span class="count">— ${esc(c.reason)}</span></li>`).join("")}</ul></div>`;
  if (res.code) x += `<div class="box info"><b>Verified code</b><pre class="code">${esc(res.code)}${res.tests ? "\n\n# tests\n" + esc(res.tests) : ""}</pre>${res.execution ? `<div class="count">sandbox: exit ${res.execution.returncode} in ${res.execution.ms} ms · stdout: ${esc(res.execution.stdout.trim().slice(0, 200))}</div>` : ""}</div>`;
  if (res.assumptions?.length) x += `<div class="box info"><b>Assumptions</b><ul>${res.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></div>`;
  $("#decisionExtras").innerHTML = x;

  // rounds
  const rounds = rec.verification_rounds || [];
  const sel = $("#roundSel");
  sel.innerHTML = rounds.map((r, i) => `<option value="${i}" ${i === rounds.length - 1 ? "selected" : ""}>round ${r.round}${i === rounds.length - 1 ? " (final)" : ""}</option>`).join("");
  sel.hidden = rounds.length < 2;
  sel.onchange = () => renderRound(rounds[+sel.value]);
  if (rounds.length) renderRound(rounds[rounds.length - 1]);
  else $("#claims").innerHTML = `<p class="count">No generation round: the decision was made before drafting (${esc(res.status)}).</p>`;

  renderEvidence(rec);
  renderRevisions(rec);
  renderMetrics(rec);
  document.querySelectorAll(".cite").forEach((c) => c.addEventListener("click", () => highlightEv(c.dataset.ev)));
}

function renderRound(r) {
  const rep = r.report;
  let h = "";
  const all = [...rep.claims];
  if (rep.presuppositions?.length) rep.presuppositions.forEach((p) => all.push({ ...p, id: "premise", text: p.text }));
  if (!all.length) h = `<p class="count">No factual claims in this round.</p>`;
  all.forEach((c) => {
    h += `<div class="claim"><div class="claim-top"><span class="v ${c.verdict}">${esc(c.verdict)}</span><div class="txt">${citeHtml(c.text)}
      ${c.id === "premise" ? '<span class="tag">presupposition</span>' : ""}${c.hidden ? '<span class="tag hidden">undeclared</span>' : ""}${c.type === "calculation" ? '<span class="tag">calculation</span>' : ""}</div>
      <span class="count">${pct(c.confidence)}</span></div>
      <ul class="checks">${(c.checks || []).map((k) => `<li><span class="nm">${esc(k.check)}</span><span class="v ${k.verdict}">${esc(k.verdict)}</span><span>${citeHtml(k.detail || "")}</span></li>`).join("")}</ul></div>`;
  });
  const issues = [...rep.issues, ...(r.critic || [])];
  if (issues.length) {
    h += `<div class="box ${rep.passed ? "info" : "bad"}"><b>Issues in round ${r.round} (${rep.passed ? "passed" : "failed"})</b><ul>${issues.map((i) =>
      `<li><span class="v ${i.severity === "minor" ? "warn" : "fail"}">${esc(i.severity)}</span> <b style="display:inline">${esc(i.category)}</b> → ${esc(i.route_to)}: ${esc(i.detail)}${i.hint ? ` <span class="count">(${esc(i.hint)})</span>` : ""}</li>`).join("")}</ul></div>`;
  } else h += `<div class="box info"><b>No issues — all checks passed.</b></div>`;
  $("#claims").innerHTML = h;

  let ca = "";
  if (rep.code) {
    const ex = rep.code.execution;
    ca += `<h3 style="margin-top:14px">Code verification</h3><ul class="checks">
      <li><span class="nm">static API analysis</span><span class="v ${rep.code.analysis.ok ? "pass" : "fail"}">${rep.code.analysis.ok ? "pass" : "fail"}</span><span>${esc(rep.code.analysis.issues.map((i) => `L${i.line}: ${i.detail}${i.suggestion ? " — " + i.suggestion : ""}`).join("; ") || "no issues")}</span></li>
      <li><span class="nm">sandboxed execution</span><span class="v ${ex ? (ex.ok ? "pass" : "fail") : "skip"}">${ex ? (ex.ok ? "pass" : "fail") : "not run"}</span><span>${ex ? esc(`exit ${ex.returncode}, ${ex.ms} ms ${ex.blocked.length ? "· BLOCKED: " + ex.blocked.join("; ") : ""} ${ex.ok ? "" : (ex.stderr || "").trim().split("\n").slice(-1)[0]}`) : "skipped (unsafe code)"}</span></li></ul>`;
  }
  if (rep.actions?.length) {
    ca += `<h3 style="margin-top:14px">Tool calls</h3>` + rep.actions.map((a) => `<div class="claim"><div class="claim-top"><span class="v ${a.status.split(" ")[0]}">${esc(a.status)}</span>
      <div class="txt mono">${esc(a.requested.tool)}(${esc(JSON.stringify(a.requested.params))})</div><span class="count">${esc(a.risk)}</span></div>
      ${a.issues?.length ? `<ul class="checks">${a.issues.map((i) => `<li><span class="nm">${esc(i.type)}</span><span>${esc(i.detail)}${i.suggestion ? " — " + esc(i.suggestion) : ""}</span></li>`).join("")}</ul>` : ""}
      ${a.result ? `<pre class="json">${esc(JSON.stringify(a.result, null, 1)).slice(0, 1500)}</pre>` : ""}</div>`).join("");
  }
  $("#codeActions").innerHTML = ca;
  document.querySelectorAll("#claims .cite").forEach((c) => c.addEventListener("click", () => highlightEv(c.dataset.ev)));
}

function renderEvidence(rec) {
  const ev = rec.evidence || [];
  $("#evidCount").textContent = `${ev.length} passages`;
  $("#conflicts").innerHTML = (rec.conflicts || []).map((c) => `<div class="conflict"><b>Conflict ${c.a} ↔ ${c.b}</b>: ${esc(c.detail)}<br>
     <span class="v ${c.resolution === "resolved" ? "pass" : "warn"}">${c.resolution}</span> ${esc(c.rule)}${c.resolution === "resolved" ? ` → prefer ${c.preferred}` : ""}</div>`).join("");
  $("#evidence").innerHTML = ev.map((e) => {
    const q = e.flags.includes("prompt_injection");
    return `<div class="ev ${q ? "q" : ""}" id="ev-${e.id}"><div class="ev-top"><b>${e.id}</b> ${esc(e.source)} ${e.date ? "· " + esc(e.date) : ""}
      <span class="rel" title="reliability ${e.reliability}"><i style="width:${e.reliability * 100}%"></i></span> ${e.reliability.toFixed(2)}
      ${e.flags.map((f) => `<span class="flag ${f}">${f === "prompt_injection" ? "QUARANTINED: prompt injection" : f.replace("_", " ")}</span>`).join("")}
      <span class="tag">${esc(e.found_by)}</span>${e.url ? ` <a href="${esc(e.url)}" target="_blank" rel="noopener">source</a>` : ""}</div>${esc(e.text)}</div>`;
  }).join("");
}

function highlightEv(id) {
  const el = $(`#ev-${id}`);
  if (!el) return;
  $$(".ev.hl").forEach((x) => x.classList.remove("hl"));
  el.classList.add("hl"); el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderRevisions(rec) {
  const f = rec.injected_faults || [];
  $("#faults").innerHTML = f.length ? `<div class="box warn"><b>Red-team faults planted in draft 1</b><ul>${f.map((x) =>
    `<li><span class="v ${x.detected ? "pass" : "fail"}">${x.detected ? "caught" : "missed"}</span> ${esc(x.type)} — ${esc(x.detail)}</li>`).join("")}</ul></div>` : "";
  const revs = rec.revisions || [];
  $("#revisions").innerHTML = revs.length ? revs.map((r) => `<div class="rev"><b>Draft ${r.round}</b> by ${esc(r.author)}<div class="count">because: ${esc(r.reason.slice(0, 4).join(" | "))}</div>
      ${r.diff.removed.map((t) => `<div class="rm">− ${esc(t)}</div>`).join("")}${r.diff.added.map((t) => `<div class="add">+ ${esc(t)}</div>`).join("")}
      ${r.diff.code_changed ? '<div class="add">± code changed</div>' : ""}${r.diff.actions_changed ? '<div class="add">± tool calls changed</div>' : ""}</div>`).join("")
    : `<p class="count">No revisions were needed${rec.verification_rounds?.length ? " — the first draft passed verification" : ""}.</p>`;
}

function renderMetrics(rec) {
  const g = rec.metrics.generation, v = rec.metrics.verification, l = rec.metrics.llm_calls;
  const kv = (k, val) => `<div class="kv"><span>${k}</span><span>${val ?? "—"}</span></div>`;
  $("#metrics").innerHTML = `<div class="metric-grid">
    <div class="m"><h4>Generation quality</h4>${kv("rounds", g.rounds)}${kv("revisions", g.revisions)}${kv("first-draft claims", g.first_draft_claims)}
      ${kv("first-draft supported", pct(g.first_draft_supported_ratio))}${kv("final supported", pct(g.final_supported_ratio))}${kv("generator self-confidence", g.generator_self_confidence != null ? pct(g.generator_self_confidence) : "—")}</div>
    <div class="m"><h4>Verification</h4>${kv("checks run", v.checks_run)}${kv("issues found", v.issues_found)}${kv("evidence passages", v.evidence_items)}
      ${kv("quarantined sources", v.quarantined_sources)}${kv("source conflicts", v.source_conflicts)}${v.injected_faults ? kv("planted faults caught", `${v.injected_faults_detected}/${v.injected_faults}`) : ""}${kv("LLM calls", l.total)}</div></div>`;
}

/* ------------------------------------------------------------------ history */
async function loadHistory() {
  const rows = await api("/api/runs?limit=100");
  $("#histTbl tbody").innerHTML = rows.map((r) => `<tr class="click" data-id="${r.id}"><td class="mono">${esc(r.created_at)}</td><td>${esc(r.task)}</td>
    <td><span class="pill ${r.status}">${esc((r.status || "").replaceAll("_", " "))}</span></td><td class="num">${pct(r.confidence)}</td><td>${esc(r.mode)}</td></tr>`).join("")
    || `<tr><td colspan="5" class="count">No runs yet.</td></tr>`;
  $$("#histTbl tr.click").forEach((tr) => tr.addEventListener("click", () => { showTab("run"); history.replaceState(null, "", `?run=${tr.dataset.id}`); loadRun(tr.dataset.id); }));
}
$("#refreshHist").addEventListener("click", loadHistory);

/* ------------------------------------------------------------------ evaluation */
async function loadEval() {
  const d = await api("/api/eval");
  if (!d.available) { $("#evalBody").innerHTML = `<div class="panel">No evaluation results yet. Run <code>python -m eval.run_eval</code>.</div>`; return; }
  const vb = d.verifier, ec = d.e2e_clean, ef = d.e2e_faults;
  let h = `<div class="panel"><div class="panel-head"><h3>Results generated ${esc(d.generated_at)} · mode ${esc(d.mode)}${d.models ? ` · ${esc(d.models.generator)} / ${esc(d.models.verifier)}` : ""}</h3>
    <button class="ghost" id="rerunVb">Re-run verifier benchmark live</button></div>
    <p class="count" style="margin:0">Verification quality is measured separately from generation quality: the verifier benchmark feeds fixed, labelled outputs to the verifier; the end-to-end suite scores whole-system decisions against a generator-only baseline.</p></div>`;
  h += `<div class="stats">`;
  if (vb) h += stat("Verifier F1 (error detection)", vb.overall.f1, `precision ${vb.overall.precision} · recall ${vb.overall.recall}`)
       + stat("False-rejection rate", vb.overall.false_rejection_rate, "correct outputs wrongly flagged");
  if (ec) h += stat("End-to-end decision accuracy", ec.accuracy, `generator-only baseline ${ec.baseline_accuracy_generator_only}`)
       + stat("False-accept rate", ec.false_accept_rate, "unreliable/unsafe answers delivered");
  if (d.e2e_heldout) h += stat("Held-out decision accuracy", d.e2e_heldout.accuracy, `${d.e2e_heldout.cases} unseen tasks · baseline ${d.e2e_heldout.baseline_accuracy_generator_only}`);
  if (ef) h += stat("Planted faults caught", ef.fault_detection_rate, `${ef.faults_injected} faults · accuracy ${ef.accuracy} after self-correction`);
  h += `</div>`;
  if (ec) {
    h += `<div class="panel"><h3>Decision accuracy: with verification vs generator-only</h3><div class="cmp">
      ${cmpRow("Clean · VeriMind", ec.accuracy, "var(--accent)")}${cmpRow("Clean · generator only", ec.baseline_accuracy_generator_only, "var(--muted)")}
      ${ef ? cmpRow("Red-team · VeriMind", ef.accuracy, "var(--accent)") + cmpRow("Red-team · generator only", ef.baseline_accuracy_generator_only, "var(--muted)") : ""}</div></div>`;
  }
  if (vb) {
    h += `<div class="cols"><div class="panel"><h3>Verifier benchmark by output type</h3><table class="tbl"><thead><tr><th>type</th><th class="num">precision</th><th class="num">recall</th><th class="num">F1</th><th class="num">false-rej.</th><th class="num">n</th></tr></thead><tbody>
      ${Object.entries(vb.by_kind).map(([k, s]) => `<tr><td>${k}</td><td class="num">${s.precision}</td><td class="num">${s.recall}</td><td class="num">${s.f1}</td><td class="num">${s.false_rejection_rate}</td><td class="num">${s.tp + s.fp + s.fn + s.tn}</td></tr>`).join("")}</tbody></table>
      <p class="count">Exact fact-verdict accuracy ${vb.fact_verdict_exact_accuracy} · approval gating ${vb.action_gating_accuracy} · LLM judge ${vb.llm_judge ? "on" : "off"}</p>
      ${d.verifier_det_only ? `<p class="count">Ablation, deterministic only: P ${d.verifier_det_only.overall.precision} R ${d.verifier_det_only.overall.recall} F1 ${d.verifier_det_only.overall.f1} FRR ${d.verifier_det_only.overall.false_rejection_rate}</p>` : ""}</div>
      <div class="panel"><h3>Recall by error category</h3><table class="tbl"><tbody>${Object.entries(vb.recall_by_category).map(([k, v]) =>
        `<tr><td class="mono">${esc(k)}</td><td class="num">${v.caught}/${v.total}</td><td><span class="v ${v.recall === 1 ? "pass" : v.recall > 0 ? "warn" : "fail"}">${pct(v.recall)}</span></td></tr>`).join("")}</tbody></table></div></div>`;
    const miss = vb.items.filter((i) => (i.label === "bad") !== i.flagged);
    if (miss.length) h += `<div class="panel"><h3>Verifier errors (shown for transparency)</h3><table class="tbl"><tbody>${miss.map((i) =>
      `<tr><td class="mono">${esc(i.id)}</td><td>${i.label === "ok" ? "false alarm" : "missed"}</td><td><span class="v ${i.predicted}">${esc(i.predicted)}</span></td><td>${esc(i.claim || "")}</td></tr>`).join("")}</tbody></table></div>`;
  }
  [["End-to-end suite (clean)", ec], ["End-to-end suite with fault injection", ef], ["Held-out suite (written after development)", d.e2e_heldout]].forEach(([title, e]) => {
    if (!e) return;
    h += `<div class="panel"><h3>${title}</h3><p class="count">${e.cases} tasks · false-accept ${e.false_accept_rate} · false-reject ${e.false_reject_rate} · avg rounds ${e.generation.avg_rounds} · first-draft support ${pct(e.generation.avg_first_draft_supported_ratio)}
      ${e.faults_injected ? ` · faults caught ${Object.entries(e.faults_by_type).map(([t, v]) => `${t} ${v.detected}/${v.injected}`).join(", ")}` : ""}</p>
      <table class="tbl"><thead><tr><th>id</th><th>category</th><th>task</th><th>expected</th><th>decision</th><th>pass</th><th>baseline</th></tr></thead><tbody>
      ${e.rows.map((r) => `<tr class="click" data-id="${r.run_id}"><td class="mono">${r.id}</td><td>${esc(r.category)}</td><td>${esc(r.task)}</td><td class="count">${r.expected.join(" / ")}</td>
        <td><span class="pill ${r.status}">${esc(r.status.replaceAll("_", " "))}</span></td><td><span class="v ${r.pass ? "pass" : "fail"}">${r.pass ? "pass" : "fail"}</span></td><td><span class="v ${r.baseline_pass ? "pass" : "fail"}">${r.baseline_pass ? "pass" : "fail"}</span></td></tr>`).join("")}</tbody></table></div>`;
  });
  $("#evalBody").innerHTML = h;
  $("#rerunVb")?.addEventListener("click", async (ev) => {
    ev.target.disabled = true; ev.target.textContent = "Running…";
    try { const r = await api("/api/eval/verifier", { method: "POST" }); alert(`Live verifier benchmark: F1 ${r.overall.f1}, precision ${r.overall.precision}, recall ${r.overall.recall}, false-rejection ${r.overall.false_rejection_rate} (${r.seconds}s, LLM judge ${r.llm_judge ? "on" : "off"})`); }
    catch (e) { alert(e.message); }
    ev.target.disabled = false; ev.target.textContent = "Re-run verifier benchmark live";
  });
}
const stat = (k, v, sub) => `<div class="stat"><div class="k">${k}</div><div class="val">${v == null ? "—" : pct(v)}</div><div class="sub">${sub}</div></div>`;
const cmpRow = (label, v, color) => `<div class="cmp-row"><span>${label}</span><div class="cmp-bar"><i style="width:${(v || 0) * 100}%;background:${color}"></i></div><b class="num">${pct(v)}</b></div>`;

/* ------------------------------------------------------------------ architecture */
let archLoaded = false;
async function loadArch() {
  if (archLoaded) return; archLoaded = true;
  $("#archDiagram").innerHTML = ARCH_SVG;
  const p = await api("/api/prompts");
  $("#prompts").innerHTML = Object.entries(p).map(([k, v]) => `<details class="prompt"><summary><b>${esc(k)}</b></summary><pre>${esc(v)}</pre></details>`).join("");
  const t = await api("/api/tools");
  $("#tools").innerHTML = `<table class="tbl"><thead><tr><th>tool</th><th>endpoint</th><th>risk</th><th>params</th></tr></thead><tbody>${Object.entries(t).map(([n, s]) =>
    `<tr><td class="mono">${esc(n)}</td><td class="mono">${esc(s.endpoint)}</td><td><span class="v ${s.risk === "read_only" ? "pass" : s.risk === "low_write" ? "warn" : "fail"}">${esc(s.risk)}</span></td><td class="mono">${esc(Object.keys(s.params).join(", "))}</td></tr>`).join("")}</tbody></table>`;
  const kb = await api("/api/kb");
  $("#kbList").innerHTML = `<table class="tbl"><tbody>${kb.map((d) => `<tr><td>${esc(d.title)}</td><td class="count">${esc(d.source_type)}</td><td class="num">${d.reliability}</td></tr>`).join("")}</tbody></table>`;
}

const ARCH_SVG = `<svg viewBox="0 0 1000 440" role="img" aria-label="VeriMind architecture">
<defs><marker id="ah" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="currentColor"/></marker></defs>
<g style="color:var(--muted)" stroke="currentColor" fill="none" stroke-width="1.6">
  <path d="M160 72 H180" marker-end="url(#ah)"/><path d="M324 72 H344" marker-end="url(#ah)"/><path d="M488 72 H508" marker-end="url(#ah)"/>
  <path d="M652 72 H672" marker-end="url(#ah)"/><path d="M816 72 H836" marker-end="url(#ah)"/>
  <path d="M746 104 V168" marker-end="url(#ah)"/>
  <path d="M676 200 C 520 200, 440 170, 418 110" stroke-dasharray="5 4" marker-end="url(#ah)"/>
  <path d="M676 216 C 610 216, 590 170, 582 110" stroke-dasharray="5 4" marker-end="url(#ah)"/>
  <path d="M816 200 H910 V110" marker-end="url(#ah)"/>
</g>
<g font-size="13">
  ${box(20, 40, 140, "Planner", "type · ambiguity · premises")}
  ${box(184, 40, 140, "Researcher", "KB · user docs · web")}
  ${box(348, 40, 140, "Draft", "researcher or coder")}
  ${box(512, 40, 140, "Coder / tools", "calc · code · API calls")}
  ${box(676, 40, 140, "Verifier", "independent paths")}
  ${box(840, 40, 140, "Finalizer", "accept · caveat · reject")}
  ${box(676, 172, 140, "Critic", "reasoning review")}
</g>
<g font-size="12" class="muted">
  <text x="30" y="160">↺ self-correction loop (dashed):</text>
  <text x="30" y="180">evidence problems → researcher</text>
  <text x="30" y="200">code / calc / API problems → coder</text>
  <text x="30" y="220">false premise / unsafe → finalizer</text>
</g>
<g font-size="12">
  <rect x="20" y="290" width="960" height="130" rx="12" fill="none" stroke="var(--line)"/>
  <text x="36" y="316" font-weight="600">Independent verification paths (never see the generator's reasoning)</text>
  <text x="36" y="342">A · deterministic: citation integrity · IDF-weighted grounding · quantity/date agreement · polarity &amp; antonyms · source reliability, supersession, quarantine</text>
  <text x="36" y="366">B · model-based: LLM entailment judge on a separate verifier model — its quoted evidence must exist verbatim or the verdict is discarded</text>
  <text x="36" y="390">C · execution: AST calculator + operand grounding · static API analysis + sandboxed tests · tool-call schema &amp; policy validation, approval gating</text>
</g></svg>`;
function box(x, y, w, t, s) {
  return `<rect x="${x}" y="${y}" width="${w}" height="64" rx="10" fill="var(--soft)" stroke="var(--line)"/>
  <text x="${x + w / 2}" y="${y + 28}" text-anchor="middle" font-weight="600">${t}</text>
  <text x="${x + w / 2}" y="${y + 47}" text-anchor="middle" font-size="11" class="muted">${s}</text>`;
}
