---
---

# Step 3: Triage

## RULES

- Be precise. When uncertain between categories, prefer the more conservative classification.
- Classifications here are RECOMMENDATIONS. The owner decides (or, in `auto-dev-loop` only, the `findings-evaluator` subagent).

## INSTRUCTIONS

1. **Normalize** findings into a common format. Expected input formats:
   - `blind-hunter`: Markdown list (title, description, evidence, recommended classification)
   - `edge-case-hunter`: JSON array of objects with `location`, `trigger_condition`, `guard_snippet`, `potential_consequence`, `recommended_classification`
   - `acceptance-auditor`: Markdown list (title, AC/constraint reference, evidence, recommended classification)

   If a layer's output does not match its format, parse best-effort and note the parsing issue for the owner.

   Convert all to one list where each finding has:
   - `id` — sequential integer
   - `source` — `blind`, `edge`, `auditor`, or merged (e.g. `blind+edge`)
   - `title` — one-line summary
   - `detail` — full description
   - `location` — file and line reference (if available)

2. **Deduplicate.** Merge findings that describe the same issue:
   - Use the most specific as the base (prefer edge-case JSON with a location over prose).
   - Append unique detail, reasoning and locations from the others into `detail`.
   - Set `source` to the merged sources. Preserve genuine disagreement between layers (one recommends patch, another defer) in `detail` — it is a real owner call.

3. **Classify** each finding into exactly one bucket:
   - **decision_needed** — an ambiguous choice; the code cannot be patched correctly without the owner's intent. Only possible when `{review_mode}` = `"full"`.
   - **patch** — fixable without owner input; the correct fix is unambiguous.
   - **defer** — pre-existing issue not caused by this change; real but not actionable now.
   - **dismiss** — noise, false positive, or handled elsewhere.

   If `{review_mode}` = `"no-spec"` and a finding would be `decision_needed`, reclassify it as `patch` (fix unambiguous) or `defer` (not).

4. **RETAIN every `dismiss` finding** — never drop one. Carry them into the report as a compact block under the heading `Dismissed by the review layer (N)`, one line each: `id | title | why dismissed`. A dismiss here is the layer's recommendation, not a decision — a finding discarded before the owner sees it has been decided by the layer that raised it.

5. If `{failed_layers}` is non-empty, report which layers failed before announcing results. If the only remaining findings are dismissals AND `{failed_layers}` is non-empty, warn that the review may be incomplete — never announce a clean review.

6. If NO findings were raised at all by any layer and `{failed_layers}` is empty: state "Clean review — all layers passed." Findings that exist but were recommended `dismiss` are NOT a clean review — report them per instruction 4.

7. **Pipeline mode:** return to the caller now with: each layer's raw output verbatim, the normalized deduplicated list with recommended classifications, the dismissed block, and `{failed_layers}`. Do not continue to Step 4.

## NEXT

Direct mode: read fully and follow `./step-04-present.md`
