---
run_dir: '' # set at runtime: .workflow/state/review-runs/direct-<date>-<HHMMSS> (absolute)
failed_layers: '' # set at runtime: comma-separated list of layers that failed or returned empty
---

# Step 2: Review

## RULES

- Dispatch each layer as its typed subagent (`blind-hunter`, `edge-case-hunter`, `verification-gap`, `acceptance-auditor`) — never as a generic agent. The typed definitions enforce read-only tool sets, their tier and their preloaded rules.
- `blind-hunter` receives NO project context — the diff file only. A guard (`review_run_paths.py`) confines its reads to the run's diff files; never pass it a spec, context docs, other paths or a graph project.
- `edge-case-hunter` and `verification-gap` receive the diff file, their instruction file and project read access (graph access when `{graph_project}` is set). `acceptance-auditor` receives the diff file, the spec and the context docs plus the same access.
- Every grep/glob of these agents passes an absolute path under the tree under review (guard: `review_code_paths.py`).

## MANDATORY DISPATCH PREAMBLE (guard-enforced)

EVERY subagent dispatch in this step begins with the verbatim text of `config: review.required_preamble`, followed by the `rules-defer` classification sentence: "Classify under the rules-defer rule: patch by default; defer only when the fix needs scope outside this story or a design or spec decision."

## INSTRUCTIONS

1. **Stage the diff.** Set `{run_dir}` to the absolute `<repo>/.workflow/state/review-runs/direct-<date>-<HHMMSS>` (the worktree's `.workflow/state/` when the review targets a story worktree). Write `{diff_output}` to `{run_dir}/diff.patch` (unified diff with `a/` `b/` prefixes), then:

   ```bash
   uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/review_diff_chunks.py --diff {run_dir}/diff.patch
   ```

   It writes `diff-<k>.patch` chunk files beside it (each hunk line prefixed with its source line number) and prints `{"total_lines", "chunks": [{"path", "lines", "pages"}]}`. In snapshot mode, the materialized files from Step 1 stay where they are.

2. If `{review_mode}` = `"no-spec"`, tell the owner: "acceptance-auditor skipped — no spec file provided."

3. Launch the layers in parallel (one message, several dispatches), one dispatch per layer per chunk, without conversation context. Every prompt begins with the mandatory preamble, then names the chunk: "Diff file: `<path>` (chunk k of N, `<lines>` lines). Read ALL of it with exactly these Read calls (offset/limit): `<pages>`. Report `diff_lines_read`. Cite locations as `file:<the source line number printed at the start of each hunk line>`."

   - **blind-hunter** — the preamble and the chunk note. Nothing else.
   - **edge-case-hunter** — the preamble, `Instruction file: <installed layered-review skill>/review-prompts/edge-case-hunter.md`, the chunk note, the code-under-review tree (absolute), and `graph project: <{graph_project}>` or `graph project: none`. In snapshot mode add "snapshot mode" and the snapshot dir.
   - **verification-gap** — as edge-case-hunter, with `review-prompts/verification-gap.md`.
   - **acceptance-auditor** (only when `{review_mode}` = `"full"`) — the preamble, `Spec file: <{spec_file}>`, the chunk note, the tree and the graph project line.

   If subagents are unavailable, write one prompt file per layer and chunk into `{run_dir}/prompts/` and HALT: ask the owner to run each in a separate session and paste back the findings; when they are pasted, resume at Step 3.

4. **Failure handling** ("Silence is a failure, not a pass" in workflow.md): a layer whose dispatch errors, times out, returns no report, or reports `diff_lines_read` below its chunk's line count is retried once. If the retry also fails, append `<layer>@<chunk>` to `{failed_layers}` with the reason and continue with the remaining layers.

5. Collect every completed layer's findings verbatim.

## NEXT

Read fully and follow `./step-03-triage.md`
