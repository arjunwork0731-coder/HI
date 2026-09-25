# Architecture and design decisions

## 1. Generation and verification are kept apart
* **Different inputs.** The verifier never sees drafts or chain-of-thought. It sees only the claims, the answer text, and evidence that it **retrieves again on its own** using each claim as the query. Citations from the generator are checked, never trusted.
* **Different mechanisms.** Most verification is deterministic: lexical and quantity grounding, AST calculation, static analysis, sandbox execution, schema validation. A model cannot talk its way past these checks.
* **A different model.** The LLM judge and critic run on `VERIFIER_MODEL`. Use a different model family from `LLM_MODEL` so the two don't share the same blind spots.
* **The verifier is checked too.** The judge must quote the passage verbatim, and the claim's numbers must appear in that passage. Otherwise the judgement is discarded and logged as a possible verifier hallucination.

## 2. How a claim gets its verdict
```
cited evidence ─┐
                ├─► pool ─► deterministic grounding ─┐
verifier search ┘                                    ├─► combine ─► verdict + confidence
                          LLM judge (quote-checked) ─┘
```
Combining the two paths (deterministic → judge):
* SUPPORTED + SUPPORTED → SUPPORTED (confidence goes up)
* SUPPORTED + CONTRADICTED → UNCERTAIN (flagged, confidence goes down)
* UNSUPPORTED + SUPPORTED (quote verified, reliable source) → SUPPORTED, counted as a paraphrase
* CONTRADICTED + SUPPORTED → UNCERTAIN
* judge unavailable, discarded or errored → the deterministic verdict stands

Deterministic grounding of one claim against one passage:
1. IDF-weighted coverage of the claim's content words, with prefix matching for inflections. A missing **rare** term (IDF ≥ 3) blocks support.
2. Quantities are extracted and normalised: dates, years, FY, percentages, money with crore/lakh/million scaling, and number words. Every claim quantity must match. A differing quantity of the same kind, aligned on the same subject word, is a contradiction. If the claim and source refer to *different periods*, the source counts as silent, not contradictory.
3. Polarity: a negation mismatch or an antonym pair (crash ↔ soft-landed, visible ↔ not visible) is a contradiction.
4. Named entities in the claim must appear in the passage or its title.

Across passages, support and contradiction are weighed by *effective reliability*:
* a quarantined source counts as 0
* a superseded source counts at ×0.5
* a gap of 0.35 or more in reliability decides the verdict
* otherwise the verdict is **CONFLICTING**, which is acceptable only if the answer discloses both values

## 3. Evidence model
Each source carries `reliability`, `date`, `source_type`, `entity` and `aliases`, and may carry `supersedes`.
* Passages are single sentences, indexed with their title using BM25.
* User-attached documents get reliability 0.5 (untrusted by default).
* Wikipedia passages get 0.75.
* Any passage that matches prompt-injection patterns is **quarantined**. It is shown in the audit but can never support a claim, and a claim that only a quarantined passage matches is flagged as `injection_influence`.

Source conflicts are found by comparing passages pairwise, then resolved in this order:
1. an explicit `supersedes` link
2. a reliability gap (not applied when the less reliable source is newer)
3. otherwise **unresolved**, which must be disclosed as a caveat

## 4. Handling difficult inputs
| Input | Mechanism |
|---|---|
| Ambiguous | LLM planner interpretations, plus an entity-alias collision in the knowledge base (e.g. Mercury the planet vs the element) with similar retrieval scores → `NEEDS_CLARIFICATION` with one verified fact per reading |
| Incomplete | The critic's evidence-gap check (names, acronyms and identifiers in the question that no source mentions), the unit check ("in kilograms" but no source gives one), and the rule that no verified claims means refusal → `REJECTED` with reasons |
| Conflicting | Conflict detection and resolution. Unresolved conflicts must appear in the answer (`DISCLOSED_CONFLICT`) and lower confidence |
| Misleading / false premise | Low-reliability or superseded sources lose weight. Presuppositions are verified, and a contradicted premise → `REJECTED` with a correction |
| Prompt injection | Quarantine, the claim-echo check, and generator prompts that treat evidence as data |
| Invalid APIs | Static code analysis (attributes, signatures, modules, undefined names). Tool registry validation with suggestions. Tools named in the task must exist |
| Unsafe actions | Task intent screen (block before any generation), tool risk classes (financial, destructive and external actions → `REQUIRES_APPROVAL`), policy rules, dangerous-code detection, sandbox audit hooks |

## 5. The self-correction loop
* Issues carry `severity` (critical / major / minor) and a `route_to` agent.
* Critical and major issues trigger routing. Minor issues become caveats.
* Researcher issues first trigger *targeted retrieval*: failing claims and missing terms are used as queries, with Wikipedia if enabled.
* Terminal categories (false premise, unsafe action, dangerous output, non-existent tool) go straight to the finalizer.
* The loop stops when the budget is used up or when a revision makes no progress.

## 6. Confidence
Confidence starts as the mean confidence of the verified claims (which reflects source reliability and coverage). It is then multiplied down for:
* each removed claim: ×0.92
* each uncertain claim: ×0.9
* each extra round: ×0.97
* disclosed conflicts: ×0.85

Below `ACCEPT_THRESHOLD` (0.55) the finalizer refuses.

## 7. Metrics recorded for every run
* **Generation:** rounds, revisions, first-draft claim count, first-draft supported ratio, final supported ratio, the generator's self-confidence.
* **Verification:** checks run, issues by category, evidence count, quarantined sources, conflicts, planted faults detected, LLM calls by role.
