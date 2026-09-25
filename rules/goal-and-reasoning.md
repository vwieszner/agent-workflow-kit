---
name: goal-and-reasoning
description: MANDATORY. State the goal and the evidence-based reasoning BEFORE any consequential action, and before presenting any recommendation, verdict, or conclusion.
---
# Goal and Reasoning

## Rule
Before a **consequential action** or a **stated conclusion**, give the goal and the reasoning
first — in the same message, ahead of the thing itself. Not after. Not when challenged.

## When it applies

**Consequential actions**
- Writing, editing, or deleting any file
- git state changes — commit, merge, branch/worktree delete, reset, push
- Migrations, schema edits, DDL, dropping databases
- Stack up/down, container removal, killing processes
- Anything destructive, irreversible, or outward-facing

**Recommendations, verdicts, and conclusions**
- Recommending what to work on, which option to take, which story goes in which slot
- Any verdict — "X is the root cause", "this is done", "the doc is stale", "that's a flake",
  "this is startable"
- Any answer presented as the result of a check, audit, or review

## What to state
1. **Goal** — what this action or conclusion is meant to achieve.
2. **Reasoning** — the specific evidence that produced it: the file and line read, the command
   output, the rule cited, the dependency the story declares. Name the source.
3. **What was rejected** — when choosing among real alternatives, what you did not pick and why.

## Forbidden
- ❌ A recommendation without the reasoning that produced it. If the owner has to ask
  "why did you pick that?", this rule was broken.
- ❌ Substituting a different list, scope, or option than the one asked for without naming the
  substitution and the reason for it.
- ❌ Reasoning supplied only after a challenge. It ships with the output or it does not count.
- ❌ "Recommended" as a label with no stated basis.
- ✅ Read-only exploration (grep, read, ls, status checks) needs no preamble — state the goal when
  the findings are presented.

## Scope
Orchestrator and subagents alike; a subagent returning findings states its reasoning in its report.
Related: `answer-before-action.md`, `root-cause-before-fix.md`, `no-quality-reduction.md`.
