---
name: retro
description: Mine the session activity journal for reusable scripts, recurring tasks, recurring mistakes, and missing skills/agents, and write propose-and-approve proposals to .workflow/state/proposals/. Use when the owner says /retro, "run a retrospective", or "harvest this session" — typically before clearing context. Part of the script-artifact-curation loop.
---

# retro — session retrospective → curation proposals

You (the running agent) do the analysis directly — no nested headless spawn. Be conservative:
quality over quantity. Produce **proposals only**; never write into curated locations. Full rule:
`.workflow/rules/script-artifact-curation.md`.

## Steps

1. **Find unmined sessions.** Run:
   `uv run --no-project .workflow/hooks/session/session_retrospective.py --list`
   Each line is `<session_id>  new=<n>  facts=<n>  covered=<n>  <journal paths>`. If it prints
   `no unmined sessions`, tell the owner there is nothing to harvest and stop.

2. **Read the journals.** Read the listed `.workflow/state/journal/<session>*.jsonl` files. Each
   line is one turn: `tool_calls[]` (`tool`, `input`, `ok`) and `facts[]`. Mine the facts:
   `command_run`, `script_written` / `script_edited`, `tool_error`, `interrupted`,
   `skill_invoked`, `agent_spawned`, `user_message`. A session's first `covered` meaningful
   facts by `ts` were mined by an earlier run: read them for context only; a candidate needs
   evidence from the `new=` facts after them. Start with
   `uv run --no-project .workflow/scripts/retro_digest.py` (clusters over the unmined facts), then
   drill into a cluster with `.workflow/scripts/journal_grep.py` (journal facts) or
   `.workflow/scripts/transcript_calls.py` (full calls and results, Claude Code transcripts); read
   a journal whole only for a session with few new facts.

3. **Read the dedup references** before proposing:
   - the project script registry (`config: paths.scripts_index`) and `.workflow/scripts/INDEX.md`
   - skill names under `.opencode/skills/` and/or `.claude/skills/` (whichever exist)
   - agent names under `config: curation.agent_source_dir` (default `.workflow/agents/`) and the
     rendered agent dirs (`.opencode/agents/`, `.claude/agents/`)
   - `.workflow/rules/`
   - `.workflow/state/curation-learnings.md` — rejected candidates; skip anything listed there.
   - every proposal already in the queue:
     - **Pending** — each `<kind>-<slug>.md` in `.workflow/state/proposals/`: its `kind` and
       `title`. A candidate one of them already proposes is not a new proposal (Step 5 adds its
       evidence there).
     - **Archived** — the file names under `.workflow/state/proposals/archive/`. Skip a candidate
       an archived proposal already carried, unless the journals show its shipped artifact
       failing; read that archived proposal before deciding.

4. **Identify genuine candidates:**
   - `command_run` sequences repeated ≥2–3× (an ad-hoc task that recurs) → **skill** or **script**
   - `script_written` / `script_edited` that look reusable but are not in the script registry →
     **script**
   - `tool_error` / `interrupted` clusters showing a recurring mistake → **rule** (or a guard hook)
     or **memory**
   - a pattern with clear boundaries + a fixed tool set + a self-contained goal → **agent**
   Require real recurrence or clear reuse. Skip single occurrences and anything an existing
   artifact already covers.

5. **Write proposals.** For each candidate write, directly in `.workflow/state/proposals/`
   (run_id = current UTC timestamp `YYYYMMDD-HHMMSS`; if a name exists append `-2`, `-3`, ...):
   - `<kind>-<slug>.md` — the proposal: YAML frontmatter `kind`, `title`, `run_id`, `target`,
     `recurrence`, then sections **What** / **Why (evidence)** / **Impact** / **Draft** /
     **Risks / notes**.
     For the full template run
     `uv run --no-project .workflow/hooks/session/session_retrospective.py --prompt --session <id>`.
   - The **Impact** section gives one line per axis: the direction, then a measured figure with
     its scope and the command that produced it, or the word `unmeasured`:
     ```
     ## Impact
     - Quality: <raises | keeps | lowers> — <figure, scope, command | unmeasured>
     - Speed: <raises | keeps | lowers> — <figure, scope, command | unmeasured>
     - Token cost: <lowers | keeps | raises> — <figure, scope, command | unmeasured>
     ```
     Measure from the journals and transcripts when the data is there; write `unmeasured` only
     when it is not. `transcript_calls.py grep ... --count-by ...` counts calls and results across
     transcripts; `transcript_calls.py replay --hook <draft> --baseline <live hook>` gives a hook
     change's verdict delta per agent type.
   - `<kind>-<slug>.draft.<ext>` — the draft in its house format:
     - script: `.draft.py` / `.draft.sh` / `.draft.ps1`, with the doc header from the rule §2
     - skill: `.draft.SKILL.md`, frontmatter `name` + `description`, tool-neutral prose
     - agent: `.draft.agent.md`, kit agent source format — frontmatter `name`, `description`,
       `mode`, `tier` (deep|standard|fast), `effort`, `tools` (capability list), `readonly`;
       never a concrete model name
     - rule: `.draft.rule.md`, frontmatter `name` + `description`, operative content only
     - memory: `.draft.memory.md`
   For a candidate a pending proposal already carries, write no new file: append one section
   `## Additional evidence (<run_id>)` to that proposal — the facts, counts and sessions this run
   found — and change nothing else in it or its draft.
   If nothing qualifies, write nothing and say so plainly.

6. **Mark sessions mined.** For each analyzed session run:
   `uv run --no-project .workflow/hooks/session/session_retrospective.py --mark --session <session_id>`
   so the session-start nudge stops pointing at it.

7. **Report.** List the proposals written (path + kind + one-line why + its Impact lines) and the
   pending proposals that received evidence, and tell the owner to invoke the `curate` skill to
   review. Do NOT move anything into a live location — that is `curate`'s job.

## Guardrails
- Write ONLY under `.workflow/state/proposals/`. Never touch skill dirs, agent dirs,
  `.workflow/rules/`, any scripts dir, `AGENTS.md` / `CLAUDE.md`, or memory here.
- Cite the exact journal facts (commands, errors) as evidence. Conservative bias — prefer 0
  proposals over speculative ones.
