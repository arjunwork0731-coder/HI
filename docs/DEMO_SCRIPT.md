# 5-minute demo script

Open the deployed URL a minute early. Free tiers take time to wake up.

1. **Pitch (20 s):** "LLM agents hallucinate. Our generator agents never decide what the user sees. A separate verifier checks every claim, calculation, piece of code and tool call along independent paths, and the system corrects, asks, or refuses."
2. **Clean answer with citations.** Load *control → "When did Chandrayaan-3's Vikram lander land…"*.
   * Point at the live agent trace, the `[E#]` citations and the evidence panel.
   * The blog source has been **quarantined for prompt injection**.
3. **Self-correction (the key moment).** Turn on **Red-team** and run *calculation → Novatek revenue growth*.
   * A wrong result was planted. The calculator catches it, the task is routed back to the coder, the revision diff is shown, and it is re-verified.
   * Then run *conflicting → employees* with red-team on. A changed date and a fabricated "2024 audit [E99]" are both caught.
4. **Refusals are features.**
   * *incomplete → CEO of Novatek*: refused, because no source names a CEO.
   * *false premise → "Why did Chandrayaan-3 crash"*: the premise is contradicted and a correction is given.
   * *ambiguous → Mercury*: the system asks for clarification and shows one verified fact for each reading.
5. **Code and tools.**
   * *code → average* with red-team on: `statistics.average` is flagged by static analysis, fixed to `statistics.mean`, and the tests pass in the sandbox.
   * *invalid_api → forecast for 14 days*: the API maximum is 7, so the call is clamped and a caveat is shown.
   * *unsafe → transfer ₹2,000*: the request is valid but financial, so it is held for approval.
   * *delete all files*: blocked before any agent runs.
6. **Evaluation tab (40 s):**
   * The verifier is measured separately: F1 0.95 on 79 labelled items, with the errors shown openly.
   * End-to-end: 100% vs 53% for the generator-only baseline.
   * 44/44 planted faults caught.
   * Held-out suite: 93%.
7. **Architecture tab:** the diagram and the prompts. Close with the limitations: lexical grounding needs the LLM judge for paraphrases, and the sandbox is a prototype.

Likely questions:
* *How is the verifier independent?* It uses a different retrieval path, deterministic checks, a separate verifier model, and it never sees the generator's reasoning. The judge's quotes are checked.
* *What if the verifier hallucinates?* A judge quote that doesn't appear in the evidence is discarded, and the deterministic path decides.
* *Why not just ask the LLM whether it's right?* A model grading itself shares its own blind spots. Numbers, code and APIs are checked by execution instead.
