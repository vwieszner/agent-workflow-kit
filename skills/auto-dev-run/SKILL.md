---
name: auto-dev-run
description: Start a new auto-dev driver run (auto_dev_driver.py start, detached) and observe it live until it ends — one report per phase session (AUTO-DEV-PHASE line, record facts, minutes, cost, denials and errors), stops only on the owner's instruction or an operation that is not pre-authorized, and a run summary at the end. Use when the owner says "start a driver run", "run the driver", "start the auto-dev driver and watch it", "/auto-dev-run".
---

# Auto-dev run: start and observe

The driver runs the phases. This session launches it and watches. It does not take part in the
pipeline. Scripts run as `uv run --no-project .workflow/scripts/<name>.py`.

## Args

Passed through to `auto_dev_driver.py start` unchanged: `--story <id>`, `--max-stories N`,
`--max-cost-usd X`, `--session-timeout-min M`, `--land-stall-min M`, `--orchestrator-model`,
`--orchestrator-effort`. When the owner names no caps, use `--max-stories 3` and a
`--max-cost-usd` the owner confirms; never start a run with no cost cap without the owner saying so.

## Step 1 — Pre-launch

Run all of these in one message:

1. `auto_dev_driver.py list`
2. `git status --short -- .workflow .claude .opencode CLAUDE.md AGENTS.md opencode.json` (harness files;
   uncommitted ones take effect in the driver's sessions)
3. Read `config: paths.slot_registry` and the `## ` headings of `config: paths.auto_dev_failure_log`.

Then check, in order:

- **Other runs `running`, `stopping` or `stuck`:** list each one's id, story, phase and spend in the
  launch report. They are not a blocker; this session never stops or watches them.
- **`--story <id>` continues a story that holds a slot-registry row:**
  - A halted story (a failure-log entry, no slot row) is resumed by `resume_halted.py <id>` and picked
    by auto-dev-loop Step 1; it is not launched with `--story`. Say so.
  - Run `story_ledger.py <id>` and, with the auto-dev-loop workflow.md "Driver mode" routing table,
    name the phase the driver will route the story to.
  - Check that the branch contains every harness commit that touched the step it resumes at
    (`git merge-base --is-ancestor <sha> <branch>`).
  - A routing onto a stale round, or a branch missing such a commit: report it and ask the owner.
- **Uncommitted harness files:** list them in the launch report. Not a blocker.
- **No free slot and no `--story`:** report it and ask the owner.

## Step 2 — Launch

`auto_dev_driver.py start <args> --json`

- Exit 0: record `run_id` and `pid`. The run directory is `.workflow/state/auto-dev/<run-id>`.
- Exit 1 or 2: report the JSON and stop.

Launch report: the run id, pid, caps, `git rev-parse --short HEAD`, and any uncommitted harness files.

## Step 3 — Watch

Watch `driver.log` with a background wait that ends on new lines or on the run's end (Claude Code:
`Monitor` with a 30-minute timeout; otherwise a background shell). `<from>` is the number of
`driver.log` lines already reported (0 on the first watch). The run state comes from the driver's own
`list`; an empty state means `list` failed that poll, and the loop retries.

```bash
id=<run-id>; log=.workflow/state/auto-dev/$id/driver.log; n=<from>; i=0
while :; do
  m=$(wc -l < "$log" 2>/dev/null || echo "$n")
  if [ "$m" -gt "$n" ]; then sed -n "$((n+1)),${m}p" "$log"; n=$m; fi
  st=$(uv run --no-project .workflow/scripts/auto_dev_driver.py list 2>/dev/null | awk -v id="$id" '$1==id {print $4}')
  case "$st" in ended|dead|unknown) echo "RUN $st (driver.log lines: $n)"; exit 0;; esac
  i=$((i+1)); [ $((i % 20)) -eq 0 ] && echo "[$(date +%H:%M:%S)] still ${st:-unread} (driver.log lines: $n)"
  sleep 30
done
```

When the watch times out, start it again with `<from>` set to the last line count reported. Between
events, end the turn: do not poll and do not sleep in the foreground.

## Step 4 — Report each phase

On every `session <n> done in …` line, run in one message:

- `auto_dev_run_report.py <run-id> --session <n>`
- `story_ledger.py <story>` (the story named in the line)

Report to the owner:

- the `AUTO-DEV-PHASE` line
- the record facts the phase added (ledger)
- minutes and `total_cost_usd`, plus the run total against the cap
- every `permission_denials` entry and every `is_error` tool result, first line each
- for `halted`: the failure step and the entry's summary from the failure log

Lines that need no report: `session <n> config:` and `session <n>: /auto-dev-loop …`. Report every
other `driver.log` line at once, quoted:

- `harness changed since session` / `harness changed while it ran`: drift. Record it; the run does
  not stop.
- `waiting for the land lease`, `took the land lease`, `merge train:`, `the train gate for … was red`,
  `land lease stalled`: the land window.
- `watchdog:`, `failed after`, `ended without an AUTO-DEV-PHASE line`, `audit of … failed`,
  `record shows no new fact`, `infrastructure problem`, and any other line ending `stopping`: the
  driver is stopping. Go to Step 6.

## Step 5 — Stops

- **The owner orders a stop:** `auto_dev_driver.py stop <run-id>`. The run ends after the current
  session.
- **The Step 4 report shows a phase session performed an operation not on the auto-dev-loop
  workflow.md "Pre-authorized operations" list:** `auto_dev_driver.py stop <run-id> --now` (it stops
  the running session), then report the operation with its stream line.
- **Never:** kill a process, write a STOP file by hand, edit a story worktree, dispatch a pipeline
  agent, write a record line, or remove a failure entry while the run lives.

## Step 6 — Defects

For every halt and every driver stop that is not a cap or a STOP:

1. Read the session stream around the failing step (`auto_dev_run_report.py <run-id> --session <n> --last 30`),
   then the record or the failure entry.
2. Name the cause as *input → code path → wrong state → observed failure*, citing the stream line and
   the file:line. If you cannot prove it, say so.
3. Classify it: **story question** (the owner's call), **harness gap**, or **environment**.
4. Each harness gap gets a proposed backlog row for the owner to approve. Fixes land after the run,
   never into it.

## Step 7 — Run end

On `RUN ended`, `RUN dead` or `RUN unknown`:

1. Run `auto_dev_run_report.py <run-id>` and `auto_dev_driver.py list`.
2. Report: the end reason and exit code from `run.json` `ended` (for `dead`, that the process is gone
   with no end record); sessions, minutes and spend; stories by outcome; each defect from Step 6 with
   its classification; for every halted story, what unblocks it (`resume_halted.py <id> --ruling '…'`).
3. List any slot, lease or stack the run left held. Release nothing yourself.
