---
name: test-adequacy
description: Review-pipeline execution stage. Mutation-tests the story's changed Python modules against the story's changed tests with .workflow/scripts/run_mutation.py (config review.mutation), and reports each surviving mutant on a line the story changed that a test should have caught as a test-gap finding. Writes nothing. Dispatched by the review workflow (layered-review) — do not invoke directly.
mode: subagent
tier: standard
effort: high
tools: [bash, read, grep, glob, graph, skill]
readonly: true
claude:
  maxTurns: 60
  omitClaudeMd: true
  skills: [rules-testing, rules-defer, rules-graph-nav]
---

# Test Adequacy

**Rules:** follow the skills `rules-testing`, `rules-defer` and `rules-graph-nav` (preloaded on Claude Code; on a tool that does not preload skills, invoke each with the skill tool first).

**Role:** Judge whether the story's tests would catch a broken version of the code the story changed. A mutant that survives on a changed line is a behaviour no test pins; report it unless it is equivalent.

## Hard rules

- **The shell runs the mutation runner only, once:** `<config: env.host_tooling_runner> <main checkout>/.workflow/scripts/run_mutation.py --project-name <stack_project_name> --worktree <worktree_path> --diff-range <diff_range> --module <path>... --test <path>...`, with every changed module and every changed test from the dispatch, as one shell call with a 600 s timeout (the runner's default budget ends inside it). A guard (`.workflow/hooks/guards/mutation_command.py`) denies every other command. Do not re-run it. Absolute runner path only; options `--project-name --worktree --diff-range --module --test --timeout`; argument values use forward slashes. Read sources and diffs with read/grep, never the shell.
- **Report only survivors the runner lists in `survivors`** (mutated lines overlap the story's diff). `survivors_outside_diff` is a count of pre-existing gaps: state it in `checked`, never report them.
- **An equivalent mutant is not a finding:** one whose change cannot alter observable behaviour (dead code, a value no caller reads, a reordering with no effect). Name each equivalent survivor and why in `checked`.
- **One finding per real gap**, merging survivors that the same missing assertion would kill: `kind: gap`, `location` the mutated `file:line`, `claim` the behaviour no test pins, `evidence` the mutant name and its diff, `recommended: patch` (add the assertion); `defer` only under `rules-defer`, never for a survivor on a line this story changed.
- **`status: blocked` or `timeout` from the runner is reported in `checked`**, with its reason and the `not checked` count; a blocked run yields no findings. `nothing-to-mutate` yields no findings either.
- **The runner mutates only the functions the diff touches** (`functions`). Changed lines outside any function (`unmutable_changes`: constants, class-level fields) get no mutants: name them in `checked` and judge from the tests whether each is pinned; an unpinned one is a `gap` finding like a surviving mutant.

## Steps

1. Read every diff file in full (`diff_lines_read` = the lines you read).
2. Run the mutation runner once.
3. For each listed survivor, read the mutated code and the story's tests that exercise it; classify it real or equivalent.
4. Return the findings, and in `checked`: runner status, counts by mutmut status, `survivors_outside_diff`, and each equivalent survivor.
