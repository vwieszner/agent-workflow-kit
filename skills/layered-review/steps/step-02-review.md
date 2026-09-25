---
failed_layers: '' # set at runtime: comma-separated list of layers that failed or returned empty
---

# Step 2: Review

## RULES

- Dispatch each layer as its typed subagent (`blind-hunter`, `edge-case-hunter`, `acceptance-auditor`) — never as a generic/general-purpose agent. The typed definitions enforce read-only tool sets and the `deep` tier.
- `blind-hunter` receives NO project context — the diff only. Its agent has no project tools; blindness is the mechanism. Never pass it a spec, context docs, file paths to open, or a graph project.
- `edge-case-hunter` receives the diff plus project read access (and graph access when `{graph_project}` is set).
- `acceptance-auditor` receives the diff, the spec, the context docs, plus project read access (and graph access when `{graph_project}` is set) to verify finding premises in code.

## MANDATORY DISPATCH PREAMBLE (hook-enforced)

EVERY subagent dispatch in this step — whatever the delegation mechanism — MUST begin its prompt with the verbatim text of `config: review.required_preamble` (see workflow.md "Required dispatch preamble"). No paraphrase, no abbreviation. It must be the literal first text the subagent receives. Dispatches without it are blocked by `.workflow/hooks/guards/dispatch_prompt.py`.

## INSTRUCTIONS

1. If `{review_mode}` = `"no-spec"`, tell the owner: "acceptance-auditor skipped — no spec file provided."

2. Launch the layers in parallel (one message, several dispatches), without conversation context. Every prompt begins with the mandatory preamble.

   If subagents are unavailable, write one prompt file per layer below into `.workflow/state/review-prompts/` and HALT: ask the owner to run each in a separate session (ideally a different model) and paste back the findings. When they are pasted, resume here and go to Step 3.

   - **blind-hunter** — dispatch the `blind-hunter` subagent. Prompt = preamble, then `{diff_output}`. Nothing else.
   - **edge-case-hunter** — dispatch the `edge-case-hunter` subagent. Prompt = preamble, then `{diff_output}`, then `graph project: <{graph_project}>` or `graph project: none`. In snapshot mode add "snapshot mode" and the list of materialized snapshot paths.
   - **acceptance-auditor** (only when `{review_mode}` = `"full"`) — dispatch the `acceptance-auditor` subagent. Prompt = preamble, then `{diff_output}`, the content of `{spec_file}`, every loaded context doc, and the graph project line (as above). In snapshot mode, pass the snapshot paths (including the spec at that commit) and say "snapshot mode".

3. **Failure handling** (workflow.md "Model policy"): a layer whose dispatch errors, times out, or returns no report is retried once. If the retry also errors, times out, or returns nothing, append the layer name to `{failed_layers}` and continue with the remaining layers. Silence is a failure, not a pass.

4. Collect every completed layer's findings verbatim.

## NEXT

Read fully and follow `./step-03-triage.md`
