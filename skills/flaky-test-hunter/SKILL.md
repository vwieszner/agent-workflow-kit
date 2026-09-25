---
name: flaky-test-hunter
description: Root-cause a flaky test — one that passes in isolation but fails intermittently, fails only under the parallel gate, fails only in combination with other tests, or "fails sometimes". Use when someone says "flaky", "fails randomly", "only fails in the gate", "passes alone but fails in the suite", or a gate trips a test that was green before. Encodes a flake classification taxonomy (stale shared state / timing windows / shared backing service / fork-unsafe module state / blocking-in-async / environment mismatch / teardown) and the shuffle-seed reproduction discipline.
---

# Flaky-Test Hunter — flake playbook

**Prime rule:** "flaky" is a description, not a diagnosis. Nearly every flake is a real bug in test
isolation or in the product — uncontrolled shared state exposed only under parallelism is the
canonical cause. "Re-run until green" is never the closing move; at most it measures a rate.
Root-cause discipline: `.workflow/rules/root-cause-before-fix.md`. Isolation requirements:
`.workflow/rules/parallel-test-isolation.md`.

Runner commands come from config: scoped runs `config: tests.scoped_cmd` (or
`uv run --no-project .workflow/scripts/run_scoped_tests.py`), the full gate `config: tests.full_cmd`
via `uv run --no-project .workflow/scripts/run_full_suite.py`. Parallel runners this playbook
covers include pytest-xdist (`-n`), Django `manage.py test --parallel`, Jest / Vitest workers,
Playwright `workers`, Go `-p` / `t.Parallel()`.

## Phase 0 — Environment truth (ALWAYS first)

1. **Configuration of the failing run.** Establish the exact settings module / profile / env the
   failing run used. The gate passes the test-isolated configuration (`config:
   tests_policy.required_flags`, e.g. a test settings module); an ad-hoc in-container run may
   silently pick up the dev configuration (shared cache, shared message broker, shared registry
   keyspace — a whole class of fake flakes). Every reproduction MUST carry the same flags as the
   gate. If the original failure came from an ad-hoc run, FIRST re-run under the gate
   configuration — the flake may evaporate (it was environmental, which is its own finding).
2. **Read the actual failure output** — the error text, not the test name. Content mismatch,
   timeout, and setup error are different hunts.
3. **Baseline check.** Is this failure a documented known baseline condition (sprint-status notes
   at `config: paths.sprint_status`, deferred work at `config: paths.deferred_work_dir`, an incident
   note under `config: paths.planning_dir`)? Don't re-diagnose a documented condition.

## Phase 1 — Reproduce deliberately (in order of cost)

1. **Isolation:** run the test alone (gate configuration). Green alone + red in company ⇒
   cross-test interaction; go to Phase 2 with the ORDER as the variable.
2. **Same-worker company:** parallel runners shard work (by class, file, or module) across worker
   processes. Rebuild the plausible worker set — the failing unit plus the units that share its
   shard (same module/file/class group) — and run that combination single-process.
3. **Order dependence:** run the label set under the runner's shuffle mode with a LOGGED seed and
   re-run with the same seed to preserve order (e.g. Django `--shuffle [seed]`, pytest-randomly
   `-p randomly -p "randomly_seed=<seed>"`, Jest `--randomize --seed=<seed>`, Go
   `-shuffle=<seed>`). Bisect by shrinking the label set under a fixed failing seed.
4. **Parallel-only:** if it only fails under the parallel runner, it is fork / shared-backing-state
   territory (Phase 2, classes C/D) — reproduce via the full gate or a scripted N× loop, and arm
   the `hang-diagnoser` capture instrumentation (in-worker stack-dump watchdog, blocking-call
   detector) BEFORE looping so the failing run carries evidence.

## Phase 2 — Classify

| Class | Signature | Fix pattern |
|---|---|---|
| **A. Stale shared in-process state** | wrong/extra value, message or event from an earlier test in the SAME worker | reset the shared structure at test start + positive accounting of expected items (never timing-based drains) |
| **B. Timing windows too tight** | receive/wait timeouts under load, passes when the machine is quiet | widen windows that bound a WAIT for something that must arrive (they don't bound the pass); keep "assert nothing arrives" windows short |
| **C. Shared backing service across workers** | impossible cross-test data; key collisions; type errors on keys; rows from another test | per-worker key prefix / namespace / partition; isolation tokens derived from a per-test random tag (never from auto-increment IDs that restart per cloned worker DB) |
| **D. Fork-unsafe module state** | first-use failures or corruption only under a forking parallel runner | module-level client singletons → lazy per-call / per-process construction; check the library's own fork guard first |
| **E. Blocking sync-in-async** | stalls/timeouts with idle-looking stacks | wrap sync calls for the async context; add a blocking-call detector to the test base so the class fails loudly |
| **F. Environment mismatch** | fails ad-hoc, passes in the gate (or vice versa) | wrong settings/profile; missing env vars; run with the gate's exact flags and env |
| **G. Teardown / apparatus** | tests pass, process exits non-zero | resources opened in worker threads never closed → try/finally close in the worker |

If none fits you may hold a genuinely new class: capture evidence per `hang-diagnoser` Phase 1 and
treat it as an incident, not a nuisance.

## Phase 3 — Fix the class, not the member

Fixing individual tests does not stop a family. When the mechanism is class-shaped (a pattern other
tests share), sweep the module for siblings in the same pass — with explicit owner authorization if
it widens story scope. Test-only fixes still follow batch-fix discipline: all fixes, then ONE
validation run.

## Phase 4 — Prove it

- Interaction flakes: the reproducing combination N× green (state N).
- Parallel flakes: the full gate green — a quiet scoped run proves nothing here.
- Rates, honestly: "3× green" after a 1-in-2 failure is signal; after a 1-in-20 failure it is
  noise. Match sample size to the observed rate before declaring victory.

## Phase 5 — Durable capture

An incident note under `config: paths.planning_dir` or a deferred-work entry (mechanism + evidence
+ sweep scope), a memory entry if the class is new, and a commit message that names the cause. If
the test stays suspect, record it as a known baseline condition (sprint-status note) so the next
gate reader doesn't re-diagnose it; that note is the quarantine ledger unless the project has a
per-test result history.
