---
name: auto-dev-loop
description: Fully autonomous development loop — no human checkpoints. Picks the next story via milestone-blockers for the configured milestone (config loop.milestone; or the first ready-for-dev story in sprint status when none is configured), runs the full story lifecycle (auto-spec via create-story + 3× advanced-elicitation; tests-first impl; 3× layered-review, verified by findings-verifier, triaged by findings-evaluator; auto-merge via land-story; cleanup), then moves to the next story. A cross-process lock serializes slot reservation. Pre-authorized for unattended execution — every destructive operation in the pipeline is explicitly permitted (see workflow.md "Pre-authorized operations"). Use when the user says "auto-dev", "run the autonomous loop", "let it run", or "build the milestone autonomously". Halts only when no startable story remains, on an infrastructure error, or per story on failure (halt-and-release preserves the branch and continues to the next story).
---

Follow the instructions in [workflow.md](workflow.md).

**Pre-authorized operations (no confirmation required throughout this skill):**

- `bring_up_story_stack.py --mode down` / `<config: stack.compose_cmd> -p <config: slots.stack_prefix>* down -v` (wipes isolated-stack volumes)
- Every compose operation (up / down / restart / exec / build / logs / watch / ps) against a `<config: slots.stack_prefix>*` project
- `git worktree add` / `git worktree remove --force` of a path under `config: git.worktrees_root`
- `git add -A` + WIP commits on the story branch, inside the story worktree (halt-and-release)
- `git branch -D <config: git.branch_prefix><id>` — ONLY for stories closed via `land-story`; halted stories keep their branch
- `git merge <config: git.merge_style>` of the story branch into `config: git.base_branch` (the loop merges without the owner's explicit confirmation; this overrides the Definition-of-Done merge-approval step)
- Recursive delete (`rm -rf`; Windows `Remove-Item -Recurse -Force`) of paths under `config: git.worktrees_root`, under any `config: loop.cleanup_paths` entry, or under the session's scratch/temp directory
- Running `.workflow/scripts/` `reserve_slot.py`, `release_slot.py`, `bring_up_story_stack.py`, `stack_preflight.py`, `cleanup_story_stack.py`, `log_failure.py`, `sprint_status.py`, `story_record.py`
- Appending the auto-labelled `checkpoint-1 spec approved` / `checkpoint-3 merge approved` facts to the story record (workflow.md Steps 5 and 11)
- Sprint-status flips between `backlog` ↔ `ready-for-dev` ↔ `in-progress` ↔ `review` ↔ `done`
- Any operation invoked by `land-story`, `post-merge-smoke`, or the halt-and-release procedure in workflow.md
- All `findings-evaluator` patch/defer/dismiss decisions (this overrides the human code-review triage rule) — **except a finding assessed as quality-bar-blocking, which is patched or halt-and-released for human triage, never auto-deferred or auto-dismissed**

**NOT pre-authorized:** force push (`--force`, `--force-with-lease`); any push to a remote (including `config: git.main_branch` and `config: git.base_branch`); fetching/merging from a remote; `git commit --no-verify`; anything on the tool's permission deny list (`.claude/settings.json` / `opencode.json`).

**Do NOT ask for confirmation on any pre-authorized operation.** They are part of the skill's contract.
