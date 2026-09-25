---
name: create-story
description: Create a comprehensive, implementation-ready story spec from the epics (plus PRD / architecture / UX fallbacks, previous-story learnings and git history), validate it against its checklist, and mark it ready-for-dev in sprint status. Picks the first backlog story from sprint status unless a story key or path is given. Use when a story spec needs drafting — the story-spec agent and the parallel-dev-wave / auto-dev-loop spec step invoke it; also on "create story" or "draft the spec for story X".
---

# create-story

Vendored BMad workflow, MIT-licensed (`LICENSE`, `NOTICE.md` in this directory). All paths below are
relative to this skill's directory (`{skill-root}`) unless they start with `.workflow/`.

## Files

| File | Role |
|---|---|
| `workflow-engine.xml` | Execution engine: variable resolution, step/tag semantics, `template-output` checkpoints, `discover_inputs` and `validate_against_checklist` protocols, normal vs #yolo mode |
| `workflow.yaml` | Variables, input file patterns, output path |
| `instructions.xml` | The six workflow steps |
| `template.md` | Story file skeleton |
| `checklist.md` | Post-draft validation procedure |

## Caller inputs (all optional)

- **Story** — a story key (`1-2-user-auth`), `epic.story` (`1.2`), or a story file path. Absent → auto-discover the first `backlog` story in sprint status.
- **Output path** — replaces `default_output_file` (e.g. a spec target inside a story worktree; the spec then lives on the story branch).
- **Mode** — `interactive` (default in a main session) or `non-interactive` (any subagent, or a caller that says it cannot relay questions). Non-interactive runs the engine in #yolo mode from the start: no checkpoint prompts, questions are saved and returned in the completion report, never guessed past silently.
- **Sprint status owned by caller** — when stated, step 6 leaves sprint status untouched and says so.
- **Context block** — epic AC text, story title, or other material the caller already has; use it, and still read the source documents.

## Run

1. Read `workflow-engine.xml` completely. It governs everything below.
2. Read `workflow.yaml` completely and resolve its `config:` values (engine step 1a):
   ```bash
   uv run --no-project .workflow/scripts/wfconfig.py get paths.specs_dir
   ```
   Keys used: `project.owner`, `project.communication_language`, `project.document_output_language`,
   `paths.planning_dir`, `paths.specs_dir`, `paths.epics_dir`, `paths.sprint_status`,
   `paths.project_context`. A missing key takes the default noted in `workflow.yaml`.
3. Read `instructions.xml` and `template.md` completely, then execute the instructions step by step under the engine.
4. Step 6 runs the `validate_against_checklist` protocol with `checklist.md`, then updates sprint status (unless the caller owns it) and reports.

## Environment notes

- **Research subagents.** The instructions ask for parallel research subagents. Use them only when this runtime can dispatch subagents from the current context; inside a subagent, do the reading yourself.
- **Web research (step 4).** Use a documentation lookup tool or web access when available; when neither is available, state in the story that step 4 was skipped and why.
- **Missing inputs.** A pattern in `input_file_patterns` with no matches is reported as "not found" (not an error); the story records which sources were unavailable.
- **Sprint status format.** Step 1 and step 6 expect a `development_status` map keyed `<epic>-<story>-<slug>` with statuses from `config: sprint_status.statuses` (`backlog` → `ready-for-dev`). When the file is absent the workflow asks for a story key instead.
