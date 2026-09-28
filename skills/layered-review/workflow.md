# Layered Code Review Workflow

**Goal:** Review code changes adversarially using parallel review layers and structured triage.

**Your role:** Elite code reviewer. Gather context, launch the parallel adversarial layers, triage findings precisely, present actionable results. No noise, no filler.

## Triage authority belongs to the owner

Each finding is classified **patch** (apply now), **defer** (log to the story's deferred-work file), or **dismiss** (false positive / out of scope). **The classification call is the owner's, not yours.**

Your job ends at: produce findings, recommend a classification per finding, present them. **Never auto-apply patches** on the strength of your own triage, however reasonable the recommendations look. The owner gets the final word on every finding.

The single exception is `auto-dev-loop`, where the `findings-evaluator` subagent substitutes for the owner's triage. It substitutes nowhere else — not in `parallel-dev-wave`, not on a direct invocation.

## Invocation modes

Determine the mode before Step 1 and keep it for the whole run.

- **Direct mode** — the owner invoked this skill. Every checkpoint HALTs for the owner. Steps 1 → 4 run in full.
- **Pipeline mode** — invoked by the `parallel-dev-wave` or `auto-dev-loop` orchestrator, whose args carry: the diff range, the spec path (or "no spec"), the graph project (or none), and the instruction that every layer dispatch leads with the required preamble. In pipeline mode:
  - Step 1's questions are answered by the args; do not ask them. Do not HALT at the Step 1 checkpoint — print the summary and continue.
  - Steps 2 and 3 run in full.
  - **Step 4 does not run.** Return the Step 3 findings report (every layer's raw output verbatim + the normalized, deduplicated list with recommended classifications + the dismissed block + `failed_layers`) to the caller. Write nothing to the story file, the deferred-work file or sprint status; apply nothing. The caller owns verification, triage, patching and status.

## Required dispatch preamble (hook-enforced)

Every review-layer dispatch MUST begin its prompt with the text of `config: review.required_preamble`, verbatim:

```bash
uv run --no-project .workflow/scripts/wfconfig.py get review.required_preamble
```

The guard `.workflow/hooks/guards/dispatch_prompt.py` blocks review-agent dispatches without it. The sentence must be the literal text the subagent receives, not an instruction to yourself. In `auto-dev-loop`, follow the verbatim preamble with the sentence "In this run the findings-evaluator agent performs triage." — never edit the preamble itself.

## Model policy (tier `deep`)

The review layers are `deep`-tier agents (`config: models.<tool>.deep`, set by their frontmatter). The invoker does not pick models per layer; it enforces:

- **Retry once.** A layer that fails is re-dispatched exactly once. If `config: models.fallback.<tool>.deep` is non-empty, the retry passes that model as a dispatch-time model override (Claude Code: the `model` parameter of the Agent dispatch). An empty key — or a tool without a per-dispatch model override — means one retry on the agent's configured model. This is the `parallel-dev-wave` model-fallback policy; keep the two consistent.
- **Silence is a failure, not a pass.** A layer counts as FAILED if the dispatch errors OR the agent returns no report. An agent that completes and emits nothing is retried exactly like a crashed one, then recorded in `{failed_layers}`. An empty layer never passes silently.
- When the primary model is known-unavailable for the rest of the session (e.g. a hard spend limit hit earlier): with a fallback configured, dispatch the fallback directly and record the deviation in the report; with none configured, do not re-attempt the layer — mark it failed in `{failed_layers}` and tell the owner.

## Workflow architecture

Step-file architecture:

1. **Read completely** — read the entire step file before acting.
2. **Follow sequence** — execute sections in order; never skip or reorder steps.
3. **Wait for input** — in direct mode, HALT at checkpoints and wait for the owner.
4. **Load next** — when directed, read fully and follow the next step file. Never load several step files at once.

## Initialization

Load `.workflow/config.toml` and resolve:

- `specs_dir` = `config: paths.specs_dir`
- `sprint_status` = `config: paths.sprint_status`
- `deferred_work_dir` = `config: paths.deferred_work_dir`
- `graph_tool` = `config: graph.tool`; `graph_main_project` = `config: graph.main_project`
- `quality_bar_doc` = `config: paths.quality_bar_doc`
- `date` = current system date
- `communication_language` = `config: project.communication_language` — speak to the owner in this language in every step.
- The project's always-loaded instructions (`AGENTS.md` / `CLAUDE.md`) and `config: paths.lessons` if present.
- The project-context doc: the first match of `config: paths.project_context` (empty → `**/project-context.md`) if present — load it as project implementation rules for the review. No match is reported, not an error.

Then read fully and follow `./steps/step-01-gather-context.md`.
