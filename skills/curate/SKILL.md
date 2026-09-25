---
name: curate
description: Review pending curation proposals in .workflow/state/proposals/ and, with explicit per-item owner approval, promote each draft into its live home (skill / agent / rule / script+registry / memory), refresh the artifact catalog, commit, and archive the proposal. Rejections are logged so they are not re-proposed. Use when the owner says /curate or "review proposals".
---

# curate — review & approve curation proposals

The **only** path that moves a proposal into a live location. Every promotion requires explicit
owner approval. Full rule: `.workflow/rules/script-artifact-curation.md`.

## Steps

1. **List pending proposals.** A pending proposal is a `<kind>-<slug>.md` file directly in
   `.workflow/state/proposals/` (not `archive/`, not a `*.draft.*` file, not `README.md`). If none,
   tell the owner the queue is empty and stop.

2. **Present each proposal.** Read the proposal and its `<kind>-<slug>.draft.*` file. Show the
   owner: kind, title, `target`, the evidence (`recurrence` + the cited journal facts), and a short
   preview of the draft. Ask the owner for a decision: **Approve / Reject / Skip** (capture a reason
   on Reject). When batching several proposals, ask about all of them in one round-trip. A terse
   reply that does not unambiguously identify the option (and the proposal) must be re-confirmed
   before acting.

3. **On Approve**, promote per kind:
   - **script** → write the draft to `<script home>/<name>.<ext>` (script home = the directory of
     `config: paths.scripts_index`); add a row to that registry under the right category.
   - **skill** → create `<skills dir>/<name>/SKILL.md` in **every installed skill dir that exists**
     (`.opencode/skills/` and/or `.claude/skills/`). Identical content in each.
   - **agent** → **confirm the `tools`, `readonly` and `tier` frontmatter with the owner before
     writing** (agents carry tool access). Write the draft in kit agent source format to
     `<config: curation.agent_source_dir>/<name>.md` (default `.workflow/agents/`), then render it
     into the installed tool dirs:
     `uv run --no-project .workflow/scripts/install.py render-agent <config: curation.agent_source_dir>/<name>.md`
     Never hand-write the rendered `.opencode/agents/` / `.claude/agents/` files.
   - **rule** → create `.workflow/rules/local/<name>.md`; add a row to
     `.workflow/rules/local/INDEX.md`; if the rule is always-on, add its short form plus a
     "Full rule:" line to `.workflow/AGENTS.local.md` (ask the owner first — it is a
     source-of-truth file). Never write into `.workflow/rules/*.md` or `.workflow/AGENTS.md`:
     those are kit-managed and a reinstall overwrites them.
   - **memory** → write `<memory dir>/<name>.md` and add a one-line pointer to that dir's
     `MEMORY.md` (create if missing). Memory dir = `config: curation.memory_dir`; if empty, the
     tool's native project memory directory when it has one (Claude Code: the auto-memory
     directory named in the session context), otherwise `.workflow/memory/`.

   Then refresh the catalog:
   `uv run --no-project .workflow/scripts/build_artifact_index.py`
   and move the proposal and its draft into `.workflow/state/proposals/archive/`.

4. **On Reject**, append a line to `.workflow/state/curation-learnings.md` (create if missing):
   `- <kind>-<slug> (run <run_id>) | <title> — rejected: <reason> (<YYYY-MM-DD>)`
   then move the proposal and its draft into `.workflow/state/proposals/archive/`.

5. **On Skip**, leave the proposal in place for next time.

6. **Commit the promotions.** A promoted artifact that is not committed is not curated from any
   other context (a fresh clone, another machine, a teammate), while the registry and catalog
   advertise it.

   ```bash
   git add <each promoted path> <each registry/index file touched>
   git diff --cached --numstat        # VERIFY every expected path is listed
   ```

   Commit with a message naming each artifact, its kind, and the evidence that justified it — the
   rationale belongs in history (`.workflow/state/` is git-ignored, so the archived proposal is
   not).

   **Three ways files silently drop here:**
   - **`git add` is case-sensitive against the index, even on case-insensitive filesystems.** A
     file tracked as `skill.md` but presenting as `SKILL.md` stages **nothing** with no error.
     Always confirm with `git diff --cached --numstat`.
   - **Tool dirs may be allowlisted** in `.gitignore` (`.claude/*` then `!.claude/skills/<name>/`).
     A newly created skill or agent dir may need its own allowlist line first. Check with
     `git check-ignore -v <path>`.
   - **Memory files outside the repo** are never committed. Skip them deliberately and say so.

   If the owner prefers to commit themselves, print the exact `git add` line and mark the artifact
   **unshared until committed** in the report.

7. **Report** what was promoted, rejected, and skipped — **and tracked-vs-untracked per artifact**.
   Name any file that could not be staged and why (case mismatch, ignore allowlist, outside the
   repo).

## Guardrails
- Never promote without explicit per-item approval.
- A promotion is not finished until it is committed (Step 6) — or until the report has explicitly
  told the owner the artifact is unshared. The script registry and the artifact catalog must never
  advertise a file git does not have.
- New skills / agents / hooks may need a session restart to register — tell the owner after
  promoting one.
- Match house frontmatter exactly: skills = `name` / `description`; agents = kit agent source
  format (`name`, `description`, `mode`, `tier`, `effort`, `tools`, `readonly`); rules = `name` /
  `description`.
