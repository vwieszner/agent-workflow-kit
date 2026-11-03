---
---

# Step 3: Verify and Triage

## RULES

- Be precise. When uncertain between categories, prefer the more conservative classification.
- Classifications here are RECOMMENDATIONS under the `rules-defer` skill. The owner decides.

## INSTRUCTIONS

1. **Normalize** findings into one list; each finding has `id` (`D-<n>`), `source` (`blind`, `edge`, `vgap`, `auditor`, or merged, e.g. `blind+edge`), `title`, `kind`, `location` (`file:line`), `claim`, `evidence`, `recommended`. A layer whose output does not match its format is parsed best-effort, and the parsing issue is noted for the owner.

2. **Deduplicate.** Merge findings on the same file and class (a defect, a verification `gap`) whose line ranges lie within 3 lines and whose texts describe the same issue — never two findings of one layer run. Use the most specific as the base; append the others' evidence with their source. Preserve disagreement between layers (one recommends patch, another defer) — it is a real owner call.

3. **Verify every deduplicated finding.** Dispatch `findings-verifier` subagents (batches of at most 5 findings, grouped by file, in parallel), each prompt leading with the mandatory preamble and carrying each assigned finding inline (id, kind, location, claim, layer evidence), the tree under review, the graph project line, the diff chunk paths, the spec (or none) and the quality-bar doc (`config: paths.quality_bar_doc`, absolute in the tree under review, or "no quality bar configured"). Never pass the verifier a findings file. Each finding gets **CONFIRMED** / **REFUTED** / **UNVERIFIABLE** with evidence and a `bar impact:` line (`rules-bar-impact`). A verifier batch with no report is retried once; a finding still without a verdict is `UNVERIFIED`.

4. **Classify** each non-REFUTED finding into exactly one bucket:
   - **decision_needed** — an ambiguous choice; the code cannot be patched correctly without the owner's intent. Only possible when `{review_mode}` = `"full"`.
   - **patch** — real, with an unambiguous fix (the default for a real finding whose fix lies in scope, whatever its size).
   - **defer** — the fix needs scope outside this change or a design or spec decision (`rules-defer`) and the finding is not bar-blocking.
   - **dismiss** — a false positive, duplicate or already-fixed finding.
   A `bar impact: BLOCKING` finding is never `defer` or `dismiss`. If `{review_mode}` = `"no-spec"` and a finding would be `decision_needed`, reclassify it `patch` (fix unambiguous) or `defer` (not).

5. **RETAIN every REFUTED and `dismiss` finding** — never drop one. Carry them into the report as a compact block `Dismissed (N)`, one line each: `id | title | why`.

6. If `{failed_layers}` is non-empty or any finding is `UNVERIFIED`, report that before the results; never announce a clean review then. With no finding raised by any layer and no failed layer: "Clean review — all layers passed."

## NEXT

Read fully and follow `./step-04-present.md`
