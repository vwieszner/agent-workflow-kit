---
name: auto-dev-loop
description: Fully autonomous development loop — no human checkpoints. Picks the next story via milestone-blockers for the configured milestone (config loop.milestone; or the first ready-for-dev story in sprint status when none is configured), runs the full story lifecycle (auto-spec via create-story + 3× advanced-elicitation; tests-first impl; one 3-pass layered-review per round, verified by findings-verifier and triaged by findings-evaluator; finalize; land window; auto-merge via land-story; cleanup), then moves to the next story. Halt-and-release keeps a failed story's branch, worktree and stack and releases only its slot; the owner resumes it with resume_halted.py. Pre-authorized for unattended execution — every operation it may run without asking is listed in workflow.md "Pre-authorized operations". Use when the user says "auto-dev", "run the autonomous loop", "let it run", or "build the milestone autonomously". With args `driver story=<id|none>` (from auto_dev_driver.py) it runs one phase per session.
---

Follow the instructions in [workflow.md](workflow.md). With args `driver story=<id|none>` (from
`${CLAUDE_PLUGIN_ROOT}/scripts/auto_dev_driver.py`), run one phase only, per workflow.md "Driver mode".

**Pre-authorized operations:** the list in workflow.md "Pre-authorized operations" is the only list.
Do NOT ask for confirmation on anything it names; anything it does not name is not pre-authorized.
