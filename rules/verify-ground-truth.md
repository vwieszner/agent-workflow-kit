---
name: verify-ground-truth
description: MANDATORY. A test run's summary line prints BEFORE teardown — check the exit code, never the summary alone. A subagent's report is a claim, not evidence — verify it against ground truth before relaying it as a result.
---
# Verify From Ground Truth, Not From Summaries

Companion to the no-completion-claims rule in `AGENTS.md`.

## 1. A test run's `OK` is not its result — check the exit code
Many test runners print the pass summary **before** teardown. A teardown failure (database still in
use, fixture cleanup error, leaked process) prints *after* `OK` and makes the run exit non-zero.

- Never conclude from `OK`, `Ran N tests`, `N passed`, or a log tail alone.
- **Always capture the exit code** and state it:
  ```bash
  <test command> > "$TMP/t.log" 2>&1; echo "EXIT CODE: $?"
  grep -E "<summary regex>|<teardown error markers>" "$TMP/t.log" | tail -6
  ```
- Tail **past** the summary. Teardown failures print after it.
- A background task reporting a non-zero exit is authoritative even when the visible output ends in
  `OK`. Do not overrule it with the summary line.
- Prefer the kit wrappers (`.workflow/scripts/run_scoped_tests.py`, `.workflow/scripts/run_full_suite.py`),
  which report the exit code with the verdict.

## 2. A subagent's report is a claim, not evidence
Subagents report honestly from a partial view — a scratchpad, a stale container, a pre-amend working
tree. Before relaying an agent's result as fact, spend one cheap check proportional to what the claim
carries:

| Claim | Check |
|---|---|
| A test result | Confirm the running environment holds the code you think (`.workflow/scripts/check_container_sync.py` or a host-vs-container checksum); re-run it yourself if it gates a merge. |
| A git claim | `git log --oneline`, `git status --short`. |
| "X is missing" / "Y does not handle Z" | Read the cited code (the never-invent-values rule, `AGENTS.md`). |

A report and a verification are different artifacts; only the verification can be cited as evidence.

## The shape
Both cases treat a *summary* as a *result*. When the conclusion matters — a merge, a spec claim, a
dispatch premise — go to the thing itself.
