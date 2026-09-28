# Parallel Dev Wave — Orchestrator

**Role:** This skill orchestrates one story through three phases — a setup script, then two implementation agents — with three human checkpoints between them. The orchestrator (main session) owns all human interaction. Phase 1 is a deterministic script (its work is purely mechanical); Phases 2–3 each run inside a dedicated subagent with its own focused instructions.

"Parallel" means multiple story slots can be active at once. Within one story the phases run sequentially.

Config keys are cited as `config: <section>.<key>` and live in `.workflow/config.toml`. Read a value with `uv run --no-project .workflow/scripts/wfconfig.py get <key>`.

---

## Pipeline overview

```
[main session]
  │
  ├─ Phase 1: run  .workflow/scripts/story_setup.py   (async — returns immediately)
  │     ↳ slot reservation → ports → worktree → spec lookup → graph indexing (if configured)
  │     ↳ launches  bring_up_stack_async.py  in the background (stack build + up +
  │       service readiness poll + watch start) — skipped when stack.runtime = "none"
  │     ↳ writes  .workflow/state/story-setup/<stack>.json  (starting → up | failed)
  │       + .workflow/state/story-setup/<stack>.graph_project sidecar (graph project name)
  │     ↳ orchestrator then flips sprint status to in-progress
  │
  ├─ Checkpoint 1: SPEC REVIEW (human) — overlaps with the background bring-up
  │     ↳ spec missing in worktree → dispatch the story-spec subagent first
  │       (main-session co-draft on request)
  │     ↳ the owner reviews / edits the spec; explicit confirmation before Phase 2
  │
  ├─ Pre-Phase 2a: wait_for_stack_ready.py  (polls the status file until `up`)
  │
  ├─ Pre-Phase 2b: stack_preflight.py  (the gate; exit 1 → HALT)
  │
  ├─ Phase 2: dispatch the story-impl subagent  (tests-first / Batch TDD)
  │     ↳ read spec → write all tests → batched RED → implement → schema-impact check
  │     ↳ GREEN validation (batch-fix) → IMPL COMMIT → return
  │
  ├─ Post-Phase 2: orchestrator invokes the layered-review skill on the committed diff
  │     ↳ the skill dispatches the review layers (blind-hunter, edge-case-hunter,
  │       + acceptance-auditor) — it lives in the orchestrator because it spawns subagents
  │
  ├─ Post-Phase 2b: findings verification pass (MANDATORY before Checkpoint 2)
  │     ↳ dedupe/merge → dispatch findings-verifier subagent(s)
  │     ↳ REFUTED → auto-dismissed with evidence, never reaches triage
  │     ↳ CONFIRMED + UNVERIFIABLE → Checkpoint 2 with evidence + bar-impact line
  │
  ├─ Checkpoint 2: CODE REVIEW TRIAGE (human — HARD RULE)
  │     ↳ the owner classifies each finding: patch / defer / dismiss
  │     ↳ decisions persisted to the findings file before Phase 3
  │
  ├─ Phase 3: dispatch the story-finalize subagent
  │     ↳ apply approved patches → check stack logs → scoped tests → log defers
  │     ↳ conditional CLOSE-OUT COMMIT (only if patches OR defers landed)
  │
  ├─ Round 2 (conditional + human gate)
  │     ↳ trigger after Phase 3 when  approved_patches >= config: review.round2_min_patches
  │                              OR  unique_files(approved_patches) >= config: review.round2_min_files
  │     ↳ layered-review on the close-out diff → Checkpoint 2b triage →
  │       second Phase 3 run AMENDS the close-out commit (no extra commit)
  │     ↳ the owner may force-skip or force-run regardless of threshold
  │
  ├─ Pre-merge gate (when AGENTS.md's pre-merge full-gate triggers apply)
  │     ↳ a failing leg → story-diagnose → story-finalize applies its
  │       manifest (amends the close-out) → gate re-run
  │
  ├─ Checkpoint 3: MERGE APPROVAL (human)
  │     ↳ branch, commits, recorded test results, deferred items → explicit go-ahead
  │
  └─ Hand off to the land-story skill
        ↳ merge → post-merge-smoke → cleanup → sprint-status flip
```

Multiple stories can run concurrently in different slots. Only land-story (the merge step) is one-at-a-time across all stories.

**Resuming a story in a fresh session** (after a context clear, a crash, or a handover):

```
uv run --no-project .workflow/scripts/story_ledger.py <story-id>
```

It reports the position — phases observed, review rounds, `story-finalize` runs, commits, and which artifacts exist — projected from the session journal (`.workflow/state/journal/`) plus git plus disk. Nothing writes it, so it cannot be stale. Read the position, then follow its pointers to ground truth (the findings file for verdicts and triage decisions, git for commits) rather than reconstructing state from the conversation. It reports what it found: a missing phase is not proof the phase did not run.

**Fresh session at every human checkpoint.** At Checkpoints 1, 2, 2b and 3, once the owner's decision is on disk (the `story_record.py` line, or the `## Triage decisions` section of the findings file) and before the next subagent is dispatched, end the message with `State saved — clear the context, then resume parallel-dev-wave <story-id>`. Offer it only when no subagent this session dispatched is still running. The resumed session runs `story_ledger.py <story-id>`, reads only the section the next phase needs (for Phase 3: the `## Triage decisions` section, not the whole findings file), and dispatches. A story session idle for more than an hour resumes the same way — never by continuing the old conversation.

---

## Slot registry

All active slot assignments are tracked in the slot registry (`config: paths.slot_registry`).

- Slots `1..config: slots.count` are assignable. Slot `config: slots.shared_slot` is the shared dev environment — never assigned to a story.
- `story_setup.py` reserves the slot at the start of Phase 1 via the lock-protected `reserve_slot.py`: it picks the first `status=free` row and updates it in place (`status=in_use`, `story_id`, `branch`, `worktree`, `since`).
- land-story resets the row to `free` after cleanup.

---

## Prerequisites — verify before starting

1. **Free slot available** in the registry.
2. When `config: stack.runtime` is not `none`:
   - **Every host port mapping in the compose file is parameterized** — each one reads one of the env vars declared in `config: stack.ports` using `${VAR:-default}` syntax. Check with a grep over `config: stack.compose_file` for each declared var name; any literal host port collides between concurrent slots.
   - **No hard-coded `container_name:`** for any service a slot brings up — concurrent stacks collide on it.

When `config: stack.runtime = "none"`, item 2 does not apply: say "stack prerequisites skipped — stack.runtime = none".

---

## Phase 1 — Run `story_setup.py`

Phase 1 is a deterministic script, not an agent. Every step is mechanical, so it is never delegated to an LLM. Run it from the repo root as a single call:

```
uv run --no-project .workflow/scripts/story_setup.py <story_id>
```

The script does only the **fast, deterministic prep**:

1. Reserves a slot (lock-protected `reserve_slot.py`).
2. Computes the slot's port env vars (every var in `config: stack.ports`, offset by `config: stack.port_stride`).
3. Creates the worktree off `config: git.base_branch` (skips if it already exists), on branch `config: git.branch_prefix` + story id.
4. Locates the story spec under `config: paths.specs_dir`.
5. When `config: graph.tool` is not `none`: indexes the worktree into the code graph and captures the returned `graph_project` name in the `.workflow/state/story-setup/<stack>.graph_project` sidecar (the status JSON is rewritten by the async script and cannot carry extra keys). When it is `none`, the report says graph indexing was skipped.
6. Seeds `.workflow/state/story-setup/<stack>.json` with `status: starting`.
7. When the stack is enabled: launches `.workflow/scripts/bring_up_stack_async.py` as a detached background process. It does the slow work — stack build + up, the readiness poll over `config: stack.services`, and starting the watch when `config: stack.watch` is true — and updates the status file to `up` or `failed`. Output streams into `.workflow/state/story-setup/<stack>.log`.
8. Emits a structured `key: value` report on stdout and exits.

The script returns in seconds — long before the stack is ready. Proceed to Checkpoint 1 while the bring-up runs.

| Exit | Meaning | Orchestrator action |
|---|---|---|
| 0 | Slot reserved, worktree created, async bring-up launched (or stack skipped) | Parse the report, proceed to Checkpoint 1 |
| 1 | Error — reserve / worktree / launch failure | Surface the failure to the owner, halt |
| 2 | No free slot | Halt — the owner decides whether to wait |
| 3 | Multiple candidate spec files | Surface the candidates; the owner disambiguates, then continue |

**After exit 0**, flip the sprint status — key-addressed, never a line-number edit, because another session may be editing the file concurrently:

```
uv run --no-project .workflow/scripts/sprint_status.py set <story-key> in-progress --note "STARTED <date> slot <N> (<branch>); <phase marker>; dep: <...>"
uv run --no-project .workflow/scripts/sprint_status.py touch --note "<one sentence>"
```

Notes are capped at `config: sprint_status.note_max_chars`; `set` archives the replaced note to `config: paths.story_history_dir`/`<key>.md`; use `--history "..."` for long-form detail. The setup script does NOT touch the sprint-status file — that edit stays with the orchestrator.

The report carries `slot`, `branch`, `worktree_path`, `stack_project_name`, `graph_project`, the slot `ports`, `stack_status_file`, `stack_log_file`, `background_pid`, `spec_path`, and `spec_present` — used for Checkpoint 1, the Pre-Phase 2 wait, and the Phase 2/3 dispatch inputs.

---

## Model policy (pipeline-wide)

Each agent declares a **tier** and an **effort** in its frontmatter; the concrete model per tool comes from `config: models.<tool>.<tier>`. A row here and its `agents/<name>.md` frontmatter MUST agree — change both together.

| Role (agent) | Tier | Effort | Fallback |
|---|---|---|---|
| spec creation (`story-spec`, wrapping `create-story`) | `deep` | `xhigh` | `config: models.fallback.<tool>.deep` |
| review layers (`blind-hunter`, `edge-case-hunter`, `acceptance-auditor`) | `deep` | `high` | `config: models.fallback.<tool>.deep` |
| Post-Phase 2b findings verification (`findings-verifier`) | `standard` | `max` | none |
| `story-impl` | `standard` | `max` | — |
| `story-finalize` | `standard` | `max` | — |
| `story-rebase` | `standard` | `max` | — |
| `story-diagnose` | `standard` | `max` | — |
| `dev-preflight` (diagnosis-only; never the gate) | `standard` | `high` | — |
| `test-bat-runner` (not dispatched by this pipeline) | `standard` | `medium` | — |
| `findings-evaluator` (auto-dev-loop only — replaces human triage there) | `deep` | `xhigh` | `config: models.fallback.<tool>.deep` |

**Fallback.** Where a row names a fallback and that key is non-empty: if a dispatch fails (model unavailable / dispatch error / spend limit), retry that role once with the fallback model as a dispatch-time override. Only after both attempts fail does it count as failed. An empty fallback key means one retry on the same model. When the primary model is known-exhausted for the billing period, dispatch the fallback directly and record the deviation.

**Never override `findings-verifier` to a different model.** It has no fallback.

**Do not lower an effort** except on a mechanical role, via a one-story trial with a stated revert trigger, recorded in that role's row.

**`story-finalize` — if a Phase 3 run needs a second batch-fix round:** re-check every patch against the manifest before accepting it, and report the round.

**Post-merge smoke has no agent** — it is `.workflow/scripts/post_merge_smoke.py` via the `post-merge-smoke` skill. Never dispatch an agent for it.

---

## Checkpoint 1 — Spec review (human)

**If `spec_present: false`** — dispatch the `story-spec` subagent (apply the model-policy fallback on a failed dispatch). Pass in the prompt:

- The story ID and title (from the epic under `config: paths.epics_dir`)
- The full epic AC block for the story
- The worktree path
- The slot's `graph_project` (from the Phase 1 report; omit when `config: graph.tool = "none"`)
- `spec_target_path`: `<worktree_path>/<config: paths.specs_dir>/<story-key>.md`
- `mode: checkpoint`

The spec MUST be written to the worktree (not the main checkout) — it is committed as part of the story branch.

**Interactive override:** when the owner asks to co-draft the spec, skip the dispatch and invoke the `create-story` skill in the main session — the subagent is the default, not the only path.

**Then** present for review: the spec's **path**, a short summary (story goal, AC count with one-line AC titles, the files it will touch), and the subagent's batched `open_questions` list. **Do not paste the spec body into the conversation** — every pasted line is re-read on every subsequent call for the rest of the session. The owner reviews the file in an editor and may edit it directly in the worktree; paste a specific section only if asked. Collect all answers in one round-trip, apply them to the spec, and wait for explicit confirmation before Phase 2.

**When the owner confirms, record it durably:**

```
uv run --no-project .workflow/scripts/story_record.py append <story-id> "checkpoint-1 spec approved"
```

The script resolves the worktree from the slot registry, creates the record file (`<config: paths.specs_dir>/<story-id>-record.md` in the worktree) with its heading, dates the line, and refuses duplicates. Do not hand-write the markdown.

Without this line a cleared session cannot tell an approved spec from an unreviewed draft. **`.workflow/hooks/guards/phase_dispatch.py` blocks the Phase 2 `story-impl` dispatch until this line exists.**

---

## Pre-Phase 2a — Wait for the background bring-up (orchestrator)

**If `config: stack.runtime = "none"`:** skip Pre-Phase 2a and state "stack wait skipped — stack.runtime = none". Still run Pre-Phase 2b: `stack_preflight.py` prints SKIPPED for its stack checks and still gates on the branch check.

Otherwise confirm the background bring-up reached `up` before running the preflight:

```
uv run --no-project .workflow/scripts/wait_for_stack_ready.py <stack_project_name>
```

The poller emits one timestamped heartbeat line per iteration. It returns:

- **Exit 0** — status reached `up` **and the services are confirmed running now**. Proceed to Pre-Phase 2b. Idempotent — returns instantly if the status file already says `up`.
- **Exit 1** — status `failed` (or no status file). Surface the error + log tail (the script prints both) and halt.
- **Exit 2** — timeout (default 300s, `--timeout-sec`). Surface diagnosis options (stuck build / container runtime hung / mismatched ports) and decide whether to retry.
- **Exit 3** — **stale `up`**: the bring-up finished, but the stack is not running any more (runtime restart, host reboot, manual `down`). The script names the missing services and the remedy. Re-run `uv run --no-project .workflow/scripts/bring_up_story_stack.py --slot <N> --project-name <stack> --worktree <worktree_path> --mode up` (it computes and passes every port env var), then re-run the poller. Do NOT proceed to the preflight, and never bring the stack up with a bare compose `up` — without the slot's port vars it silently maps onto the shared slot's ports.

## Pre-Phase 2b — Stack preflight (orchestrator, script)

After `wait_for_stack_ready.py` exits 0, run the deterministic preflight from the main session (read-only):

```
uv run --no-project .workflow/scripts/stack_preflight.py --project-name <stack_project_name> --worktree <worktree_path>
```

It checks the services in `config: stack.services`, every `config: stack.health.checks` command, watch liveness (when `config: stack.watch`), worktree-to-container code sync (when `config: stack.health.container_src` is set — read-only hash compare; a write probe would trip a sync+restart watch action), and branch. **Exit 0** → Phase 2. **Exit 1** → surface the failing checks to the owner and HALT — never continue on a broken stack. For diagnosis beyond the script's detail lines, dispatch the `dev-preflight` subagent (diagnosis-only, report-only — it never runs stack lifecycle commands).

---

## Phase 2 — Dispatch the `story-impl` subagent

Pass in the prompt:

- `story_id`
- `branch_name`
- `worktree_path`
- `stack_project_name`
- `slot`
- `graph_project` (from the Phase 1 report / sidecar; omit when `config: graph.tool = "none"`)
- `spec_path` (absolute, in the worktree)

The subagent implements all ACs **tests-first (Batch TDD)**: write every test the story needs before any implementation code, run the scoped suite once to confirm a batched RED (no test passes — a passing test is vacuous), implement all ACs, then run the GREEN validation under the batch-fix rule. It then runs the schema-impact check, makes the impl commit, and returns. It does NOT run code review — `layered-review` spawns its own subagents, which a subagent cannot do.

Batch TDD is batched, not interleaved, so the dispatch prompt needs no special marker for `.workflow/hooks/guards/dispatch_prompt.py`. Phrase the prompt with the batched shape ("write all tests → one red run → implement → one green run → batch-fix") and avoid the forbidden iterate phrases ("after each", "per fix", "iterate", "between fixes", "confirm green before moving on").

---

## Post-Phase 2 — Invoke `layered-review` (orchestrator, main session)

After `story-impl` returns, invoke the `layered-review` skill on `<merge-base>..HEAD` from the main session. It dispatches the review layers (blind-hunter, edge-case-hunter, optionally acceptance-auditor) in parallel — that is why the call lives in the orchestrator.

Pass in the args:

1. **The required preamble.** Every subagent prompt dispatched by this review run MUST lead with the sentence in `config: review.required_preamble`. This is hook-enforced by `.workflow/hooks/guards/dispatch_prompt.py`.
2. **The graph project.** When `config: graph.tool` is not `none`: review subagents reading worktree code through the graph must use `project=<graph_project>` — `config: graph.main_project` is the main checkout's graph, not this branch's.
3. **The model directive** — the review skill carries no model policy of its own; the invoker owns it:

   > Dispatch the layers as their typed subagents — `blind-hunter`, `edge-case-hunter`, `acceptance-auditor` (their definitions carry the tier and read-only tool sets — pass no model override). If a dispatch fails (model unavailable / dispatch error), retry that layer once per the model-policy fallback. Only after both attempts fail does a layer count as failed.
   >
   > **A layer counts as FAILED if the dispatch errors OR the subagent returns no report. Silence is a failure, not a pass.** A subagent that runs to completion and emits nothing is retried exactly like a crashed one, then marked failed and recorded in `{failed_layers}`. Never let an empty layer pass silently.

**Snapshot mode.** When a review layer must read the worktree while another agent is concurrently mutating it (e.g. `story-finalize` applying patches), do NOT let it read the working tree. The typed layers have no shell, so the orchestrator materializes the snapshot: `git show <commit>:<path>` for each file under review (plus the spec at that commit) into a scratch directory, passes those snapshot paths in the prompt, marks the dispatch as snapshot mode, and names no graph project — the graph index reflects the mutating tree.

Collect the full findings list verbatim. It feeds the verification pass below — NOT Checkpoint 2 directly.

## Post-Phase 2b — Findings verification pass (MANDATORY before Checkpoint 2)

Raw review output is never presented directly — it contains false positives, and human triage time is spent only on verified items.

1. **Dedupe/merge** the raw findings (the same issue from multiple layers becomes one finding with merged sources/evidence).
2. **Verify every deduped finding.** Dispatch `findings-verifier` subagent(s) (tier `standard`, read-only — never override its model). Prompts lead with `config: review.required_preamble`, carry each assigned finding verbatim (id, claim, cited file:line, layer evidence) — never the findings-file path, which the verifier does not read — and name the graph `project` (the slot's `graph_project`, when a graph is configured). The verifier adversarially checks each finding's premise against the actual code — reads the cited file:line, traces the claimed caller/behavior, actively attempts to REFUTE. Verdict per finding: **CONFIRMED** (evidence), **REFUTED** (evidence), or **UNVERIFIABLE** (why). Per CONFIRMED/UNVERIFIABLE finding it also emits a bar-impact line judged against `config: paths.quality_bar_doc`: `bar impact: none` or `bar impact: BLOCKING — <bar-id> — <consequence>`; when that key is empty it emits `bar impact: n/a (no quality bar configured)`. For trivially checkable claims (one grep, one file read) the orchestrator may verify the *premise* inline — evidence recorded either way — but **never the bar impact**: a finding whose premise was verified inline still needs a verifier-produced bar-impact line before it can be deferred or dismissed.
3. **Silence is a failure, not a pass.** A verifier dispatch counts as FAILED if it errors, returns no report, or covers fewer findings than assigned — an async dispatch that never reports is the same thing. Verification did NOT happen for those findings. On a failed dispatch: retry once; if it fails again, either HALT, or verify the outstanding findings inline with cited evidence and tell the owner explicitly that the pass was a deviation, naming which findings it covered. Never present an unverified finding at Checkpoint 2 without saying so. The verifier has no fallback model, so spend-limit exhaustion is a normal failure mode here — confirm every assigned finding came back before treating the pass as complete.
4. **REFUTED findings never reach the triage walkthrough.** They are auto-classified dismissed-with-evidence in the findings record and appear at Checkpoint 2 only as one compact "dismissed as false positive (N)" block of one-liners.
5. **CONFIRMED and UNVERIFIABLE findings proceed to Checkpoint 2** with their verification evidence attached. Genuine layer disagreements (one recommends patch, another defer) are preserved and shown.
6. The full record (raw layer reports verbatim + verdicts + evidence) is saved to the story's findings file — `<config: paths.specs_dir>/<story-key>-code-review-findings-<date>.md` in the worktree — so nothing is lost to compaction.

---

## Checkpoint 2 — Code review triage (HARD RULE)

**Triage authority belongs to the owner. Never auto-apply patches based on recommended classifications.**

Each finding is classified as:

- **patch** — apply now (close-out commit)
- **defer** — log to the per-story deferred-work file for later
- **dismiss** — false positive / out of scope, no action

**Quality-bar gate (HARD).** The bar assessment is produced by `findings-verifier`, which read each finding's cited code. **The orchestrator does NOT assess bar impact.** It relays the verifier's `bar impact:` line verbatim next to each finding and enforces the rule mechanically:

- A finding marked `BLOCKING` is **patched, always** — `defer` and `dismiss` are not offered for it, at any scope.
- If it cannot be patched surgically within the close-out, HALT and surface it rather than deferring, quoting the verifier's named `<bar-id>` and consequence.
- REFUTED findings carry no bar line and need none.
- When `config: paths.quality_bar_doc` is empty the gate is skipped; say so once at the top of the triage ("quality-bar gate skipped — no quality bar configured").

If a finding arrives at triage with no `bar impact:` line (verifier partial failure, or a finding added outside the verification pass), do not invent one — surface the gap and route that finding back through verification before it can be deferred or dismissed.

**The classification call is the owner's.** Present all findings verbatim. Walk through them one at a time. Wait for the owner's call on each before recording it. Do NOT apply any patch or write any defer entry until the full triage list is settled.

**Enforcement.** `.workflow/hooks/guards/dispatch_prompt.py` blocks review dispatches whose prompts lack the required preamble.

Record the decisions into three lists: `approved_patches`, `deferred_findings`, `dismissed_findings`.

**`approved_patches` MUST be a patch manifest, not prose.** One entry per approved finding:

```
finding_id | file (repo-relative) | line (from the verification evidence) | change (one imperative sentence describing the exact edit) | notes (optional constraints)
```

Assemble it from the verified findings plus the triage decisions — verification already established each finding's file:line with evidence, so Phase 3 re-derives no location. A prose finding blob is not an acceptable Phase 3 input.

**Persist the decisions to disk BEFORE dispatching Phase 3.** Append a `## Triage decisions — <YYYY-MM-DD>` section to the story's findings file carrying all three lists verbatim: the full `approved_patches` manifest, the `deferred_findings`, and the `dismissed_findings` with their reasons. **`check_phase_dispatch.py` blocks the Phase 3 `story-finalize` dispatch until this section exists** (and until a GREEN `phase-2 tests` record line exists).

Once persisted, Phase 3 can be dispatched from a fresh session using the findings file's `## Triage decisions` section alone — a safe context-clear boundary. Until persisted, triage exists only in the conversation.

---

## Phase 3 — Dispatch the `story-finalize` subagent

Pass in the prompt:

- `story_id`
- `branch_name`
- `worktree_path`
- `stack_project_name`
- `graph_project` (same value threaded since Phase 1; omit when no graph)
- `approved_patches` (the patch manifest)
- `deferred_findings`
- `dismissed_findings` (the subagent ignores these)
- `round` (`1`; `2` for the Round 2 re-run; `gate-fix` for a pre-merge gate fix — both amend the close-out commit)

The subagent applies patches in one pass, checks stack logs, re-runs scoped tests (batch-fix), writes deferred items to `<config: paths.deferred_work_dir>/story-<id>.md`, and adds AT MOST ONE close-out commit (only if patches OR defers landed). The impl commit was already made by `story-impl`. It does NOT merge.

When Round 2 triggers, Phase 3 runs a **second time** with round 2's approved patches and defers only — round 1's are already committed — and uses `git commit --amend` to fold the changes into the existing close-out commit. The commit cap (`config: git.max_commits_per_story`) counts commits, not amends.

---

## Round 2 (conditional code review)

After Phase 3 lands the close-out commit, inspect the round-1 patch count.

**Trigger** (either): `approved_patches >= config: review.round2_min_patches`, OR `unique_files(approved_patches) >= config: review.round2_min_files`. The owner may override either direction ("skip round 2" / "force round 2"). Below threshold and without an override, go to Checkpoint 3.

When triggered:

1. **Re-invoke `layered-review`** on the close-out diff only (`<close-out>~1..<close-out>`), not the full branch — focused on "did the patches work, did they regress anything". The required preamble, graph-project note and model directive from Post-Phase 2 still apply, as does the Post-Phase 2b verification pass.
2. **Checkpoint 2b — round-2 triage** (human, HARD RULE). Same discipline and quality-bar gate as Checkpoint 2; persist under a new `## Triage decisions — <date> (round 2)` section.
3. **Second Phase 3 run.** Re-dispatch `story-finalize` with the round-2 `approved_patches` + `deferred_findings`. It writes new defers under a `## Round 2 — <YYYY-MM-DD>` heading in the existing per-story file, runs scoped tests, and **amends** the close-out commit.

Round 2 runs after Phase 3 because its value — "did the patches work" — requires the patches applied and tested.

---

## Rebase / re-stack — `story-rebase` subagent

Dispatch `story-rebase` whenever the branch must move onto a newer base: onto `config: git.base_branch` (before a review round or the pre-merge gate, or when a file-coupled story merged first), or onto another story's branch (stacking). Never hand a rebase to `story-finalize` or `story-impl`.

Re-check the target tip immediately before dispatching — another story may have merged. Record the dispatch with a `NEXT:` line naming the step the rebase is for (`story_record.py append <story-id> "rebase dispatched onto <target>; NEXT: <step>"`). Pass in the prompt:

- `story_id`, `branch_name`, `worktree_path`, `graph_project`
- `slot`, `stack_project_name` and its state (up / parked / down)
- `target` — the ref to rebase onto; for a stack also the old base (`git rebase --onto <target> <old-base>`)
- the branch's current commits (impl + close-out hashes, base)
- per-file conflict rules for the files both sides changed (predict them with `git merge-tree`), and the spec sections that govern them
- `validate: yes | no` — `no` when the stack is parked/down or the orchestrator rebuilds it itself; with `yes`, the scoped test labels
- `range_diff_path` — where to save the range-diff

The subagent returns the new hashes, every conflict with its resolution, and the stack-affecting files the rebase brought in (dependencies, schema, compose, `config: pipeline.stack_affecting_globs`). A resolution needing a design or scope call comes back as a HALT with file:line and options — relay it to the owner.

---

## Pre-merge gate failure — `story-diagnose` subagent

When `uv run --no-project .workflow/scripts/run_full_suite.py --story-id <id>` reports a failing leg on a story branch, dispatch `story-diagnose` with every failing test of that run in one prompt. Never hand a gate failure to `story-finalize` or `story-impl`. Pass:

- `story_id`, `branch_name`, `worktree_path`, `graph_project`, `stack_project_name`
- `base` — `git merge-base <config: git.base_branch> <branch_name>`
- the gate summary: each failing test id per leg with its tail, and any artifact paths it names

Record at dispatch: `uv run --no-project .workflow/scripts/story_record.py append <story-id> "gate diagnosis dispatched: <N> tests in <legs>; NEXT: write the ## Gate fix section, then story-finalize applies it"`.

On return:

- **Every failure `branch`, no HALT:** append the subagent's failures and manifest verbatim to the findings file as `## Gate fix — <YYYY-MM-DD>`. Then dispatch `story-finalize` with `approved_patches` = the manifest rows (state the row count), `deferred_findings` and `dismissed_findings` empty, the failing test labels as the Step 3 scoped tests, and `round: gate-fix`. Record `gate-fix finalize dispatched: G1–G<n>; NEXT: re-run the pre-merge gate`, and re-run the gate once finalize returns.
- **Any `pre-existing`, `environment` or `unproven` failure, or a HALT:** relay the whole report — mechanisms, evidence, questions — to the owner and wait. Dispatch nothing for that gate run until the owner rules.

Gate-fix patches do not count toward the Round 2 trigger; the gate re-run verifies them.

When the gate passes: `uv run --no-project .workflow/scripts/story_record.py append <story-id> "pre-merge gate GREEN — <legs and counts>, exit 0; NEXT: checkpoint-3 (merge approval)"`.

---

## Checkpoint 3 — Merge approval (human)

Present to the owner:

- Branch name
- Commit(s) — hash + subject
- Test results — from the story's `<story-id>-record.md` (`phase-2 tests` / `phase-3 tests` lines the subagents appended). If the record carries no test line, tests have no durable evidence: say so and re-run the scoped suite rather than presenting an unevidenced "green".
- Deferred items appended this round (count + summary)

Ask explicitly: **"Ready to merge `<branch>` to `<config: git.base_branch>`?"** Do not merge until the owner confirms.

**When the owner confirms, record it:**

```
uv run --no-project .workflow/scripts/story_record.py append <story-id> "checkpoint-3 merge approved"
```

**`check_phase_dispatch.py` blocks the land-story skill until this line exists** — it is the only durable evidence that the merge, the one irreversible step, was authorised by a human.

This line is written after the close-out commit, so no branch commit carries it. land-story's `cleanup_story_stack.py` copies the record into the main checkout before deleting the worktree, and land-story's bookkeeping commit lands it.

---

## Hand off to land-story

Once the owner confirms the merge, invoke the `land-story` skill with the story id.

It owns the back half of the lifecycle: the merge (`config: git.merge_style`) into `config: git.base_branch`, the post-merge smoke on the shared slot (the `post-merge-smoke` skill), cleanup (orphan watcher kill + stack down + worktree remove + branch delete + slot release + graph project delete), and the sprint-status `done` flip.

**Do NOT inline the merge in this skill.** Orphan watcher processes started during the story can pin the worktree directory and defeat `git worktree remove`; `.workflow/scripts/cleanup_story_stack.py` (invoked by land-story) handles it.

---

## Definition of Done

A story is complete only when:

1. Code + tests written in the worktree (`story-impl`)
2. `layered-review` run and findings triaged by the owner across **all rounds** that fired (Checkpoint 2 always; 2b if Round 2 triggered)
3. Approved patches applied + stack logs clean (`story-finalize`, all rounds)
4. Scoped tests pass in the story's slot (`story-finalize`, after the final round)
5. Impl commit landed (`story-impl`); close-out commit landed if patches OR defers — possibly amended by Round 2. The branch ends with at most `config: git.max_commits_per_story` commits.
6. Merged to `config: git.base_branch` with the owner's explicit confirmation (land-story)
7. Sprint status updated to `done` (land-story)
8. Branch + worktree + stack removed + slot registry reset to `free` (land-story)

---

## Forbidden

- Bringing up a slot stack without ALL port env vars from `config: stack.ports` (use `bring_up_story_stack.py`, never a bare compose `up`)
- Auto-applying code review patches (HARD RULE — Checkpoint 2)
- Deferring or dismissing a finding the verifier marked `bar impact: BLOCKING`
- Merging without the owner's explicit confirmation (HARD RULE — Checkpoint 3)
- Skipping the slot registry update
- Skipping branch/worktree/stack cleanup
- Letting a subagent return success while a service is exited/restarting or logs show fresh errors
- Dispatching `layered-review` (or its subagents) without the required preamble — hook-blocked

---

## Running multiple stories concurrently

Each story occupies its own slot. Stories can be in different phases at once — e.g. story A waiting on Checkpoint 2 triage while story B is in Phase 2. Only Checkpoint 3 → land-story is strictly one-at-a-time.

To switch between in-progress stories: note the current story's phase and slot, switch, and pick up the other story from its current checkpoint (`story_ledger.py <story-id>`). Each story's state is durable on disk (worktree + stack + slot registry + record + findings file).

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `port is already allocated` | Slot collision, or a port var not passed | Check the registry; bring the stack up only via `bring_up_story_stack.py`, which passes every var in `config: stack.ports` |
| `container name already in use` | Hard-coded `container_name:` in the compose file | Remove the `container_name:` line |
| Service exited after `up` | App crashed on startup | `<config: stack.compose_cmd> -p <stack> logs --tail=30 <service>` |
| Test label not found | Test path drift after a refactor | Scope to the directory or use the runner's name filter |
| `git worktree remove` fails with "busy" / "permission denied" after stack down | An orphan watcher process is pinning the worktree directory | Hand off to land-story, or run `uv run --no-project --with psutil .workflow/scripts/cleanup_story_stack.py <id>` directly |
| Subagent dispatch blocked by a hook | Review prompt missing `config: review.required_preamble`, OR prompt contains forbidden iterate-and-test phrases, OR a phase precondition is unrecorded | Read the hook's stderr; add the preamble / remove the iterate phrasing / write the missing `story_record.py` fact or triage section |
