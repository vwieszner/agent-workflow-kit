---
name: script-artifact-curation
description: MANDATORY. When you write or run a host script for a purpose, judge reusability; if reusable, save it to the project's script home with a doc header and register it. Recurring tasks and mistakes are harvested by the session retrospective into propose-and-approve artifacts. Nothing auto-writes into curated locations.
---
# Script & Tooling Artifact Curation

**Goal:** never lose a reusable script, never let the curated skill / agent / rule locations fill
with auto-generated noise, and turn recurring tasks and mistakes into durable tooling deliberately.

Two halves: a **manual discipline** (you, in the moment) and an **automated retrospective loop**
(hooks + propose-and-approve).

## 1. Manual discipline — when you write or run a host script

A "host script" = any `.py` / `.ps1` / `.sh` / `.bat` you author or invoke on the host for a
purpose (validation, a doc migration, a data fix, a health probe, a parser). This is tooling, not
application code.

**The script home** is the directory holding the project's script registry
(`config: paths.scripts_index`, e.g. `scripts/INDEX.md` → `scripts/`). Kit scripts under
`.workflow/scripts/` are managed by the kit installer — never add project scripts there.

**Decide when you finish using it — is it reusable?**

Reusable if ANY holds:
- You can imagine running it again next week (a probe, a generator, a checker).
- It encodes non-obvious knowledge (a port formula, a schema walk, a parsing quirk).
- It took more than a couple of minutes to get right.
- It belongs to a recurring workflow (setup, smoke, index build).

One-off if ALL hold:
- It answered a single question you won't ask again, AND
- It encodes nothing non-obvious, AND
- Re-deriving it later would be trivial.

**If reusable:**
1. Save it in the script home — not elsewhere, not inline-only in the transcript.
2. Add a **doc header** (§2).
3. Register it with a row in the script registry (`config: paths.scripts_index`), under the right
   category.

**If one-off:** leave it or clean it up. Do not register it.

**Do not reorganize the script home into subdirectories.** Script paths are referenced from hook
commands, permission allowlists, and skill files; categorize in the registry instead.

If unsure whether something is reusable, leave it unsaved — the retrospective (§3) flags it if the
task recurs.

## 2. Doc-header conventions

Every registered script carries a header that says what it is without running it.

- **Python** — module docstring with Purpose / Usage / When-to-use:
  ```python
  #!/usr/bin/env python3
  """<one-line purpose>.

  Usage
  -----
      uv run --no-project scripts/<name>.py [args]

  When to use
  -----------
      <the situation that calls for this script>
  """
  ```
- **Shell** — a leading comment block with the same three parts (`# Purpose:`, `# Usage:`,
  `# When to use:`).
- **Windows / PowerShell** — comment-based help:
  ```powershell
  <#
  .SYNOPSIS  <one-line purpose>
  .DESCRIPTION  <what it does, when to use it>
  .PARAMETER <Name>  <meaning>
  .OUTPUTS  <what it prints / exit codes>
  #>
  ```

Invoke host Python per the project's host-tooling rule (`config: env.host_tooling_runner`, default
`uv run --no-project`).

## 3. Automated retrospective loop (propose-and-approve)

- **Capture (every turn).** `.workflow/hooks/session/session_journal.py` (Claude Code `Stop` /
  `SubagentStop`; OpenCode `session.idle` via the workflow plugin) appends one line per turn —
  tool calls plus derived facts (scripts written/edited, commands run, tool errors, interrupts,
  skills invoked, agents spawned, user messages) — to `.workflow/state/journal/<session>.jsonl`.
  No LLM; persists across context clears.
- **Analyze (at compaction and on demand).** `.workflow/hooks/session/session_retrospective.py`
  (pre-compaction hook) may launch a detached headless analyst (`config: curation.analyst`); the
  manual **`retro`** skill does the same analysis in-session. Both write **proposals only**, to
  `.workflow/state/proposals/<kind>-<slug>.md` + `<kind>-<slug>.draft.<ext>`, each classified as
  **script / skill / agent / rule / memory** with evidence and a draft.
- **Nudge (session start).** `.workflow/hooks/session/check_pending_proposals.py` reports pending
  proposals (→ `curate`) or unmined journals (→ `retro`) and prunes journals older than
  `config: curation.journal_retention_days`.
- **Approve (deliberate).** The **`curate`** skill presents each proposal; on explicit approval it
  promotes the draft to its live home, refreshes the artifact catalog, commits, and archives the
  proposal. On rejection the reason goes to `.workflow/state/curation-learnings.md` so it is never
  re-proposed.

**HARD guardrail — nothing auto-writes into curated locations.** The retrospective writes only
under `.workflow/state/proposals/`. Skills, agents, rules, scripts and memories reach their live
homes **only** through explicit per-item approval in `curate`. Auto-generated **agents** (they
carry tool permissions) never activate without the owner's OK.

The unified "find it fast" index across skills, agents, rules, hooks and scripts is
`config: paths.artifact_catalog`, regenerated by
`uv run --no-project .workflow/scripts/build_artifact_index.py` (run by `curate` after each
approval).

## 4. What the retrospective proposes

- **script** — a reusable script was written ad hoc and is not registered.
- **skill** — a multi-step task (branching, owner input, several tool calls) recurred ≥2–3× and has
  no skill.
- **agent** — a task with clear boundaries, a fixed tool set and a self-contained goal recurred and
  would run better as a delegated subagent.
- **rule** — a mistake recurred that a short always-on protocol would prevent (consider also a
  guard hook under `.workflow/hooks/guards/` for hard-enforceable mistakes).
- **memory** — a durable preference, decision or correction surfaced that belongs in the agent's
  persistent memory.

Propose on real recurrence or clear reuse, never on a single occurrence.
