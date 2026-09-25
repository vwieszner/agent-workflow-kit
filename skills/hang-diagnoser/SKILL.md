---
name: hang-diagnoser
description: Diagnose a hanging or stalled test or process — receive/wait timeouts, frozen test workers, stuck gate runs, event-loop stalls, "message never arrived" failures. Use when a test times out waiting on something, a worker seems frozen, the gate wall-time explodes, or someone says "it's hanging", "stuck", "timed out waiting", "never arrived". Encodes configuration-check first, a fork-aware in-worker stack-dump watchdog, live stack attach, and blocked-vs-starved classification.
---

# Hang Diagnoser — stall/hang playbook

**Discipline (non-negotiable):** observe before theorizing. Hangs are timing/scheduling bugs — the
class where instrumentation beats reasoning. Do NOT start reading code for candidate causes until
you hold a stack dump of the stall moment. Never fix the symptom: the deliverable is the mechanism,
demonstrated by evidence, then the minimal fix (`.workflow/rules/root-cause-before-fix.md`).

## Phase 0 — Environment truth (ALWAYS first)

1. **Configuration of the failing run.** Establish the settings module / profile / env it used.
   The gate passes the test-isolated configuration (`config: tests_policy.required_flags`); an
   ad-hoc in-container run may silently use the dev configuration (shared broker / channel layer /
   cache keyspace). **Any ad-hoc repro MUST carry the gate's flags** or its failures may be
   environmental artifacts.
2. **Which stack.** If `config: stack.runtime` is not `none`:
   `<config: stack.compose_cmd> -p <stack> ps -a` — every service in `config: stack.services`
   running? Run `uv run --no-project .workflow/scripts/stack_preflight.py --project-name <stack>
   --worktree <path>` — a dead or out-of-sync container fakes hangs. With `stack.runtime = "none"`
   this step is skipped; say so.
3. **Same-machine load.** Record what else ran concurrently (the full gate deliberately overlaps
   its legs). Do not yet blame it — contention is a hypothesis until Phase 2 corroborates it.

## Phase 1 — Capture the stall in the act

Pick by situation:

- **Reproducible in a test run → in-worker stack-dump watchdog.** Arm it INSIDE the worker process
  that runs the test — a watchdog thread armed in the parent does NOT survive `fork()`, so arming it
  in global settings under a forking parallel runner is silently dead. Arm in the suspect test's
  setup, cancel in teardown, with a period shorter than the wait timeout so the dump lands
  mid-stall. Examples: Python `faulthandler.dump_traceback_later(<s>, repeat=True)` /
  `faulthandler.cancel_dump_traceback_later()`; Node a `setInterval` that writes
  `process.report.writeReport()`; JVM a timer calling `Thread.getAllStackTraces()`. Dumps go to
  stderr → the relevant stream of the gate log. Check whether the test base already carries this
  wiring before adding it.
- **Hung RIGHT NOW → attach a stack sampler to the live PID.** Find the PID inside the container
  (`<compose> -p <stack> exec -T <service> ps aux`), then dump all threads without restarting:
  Python `py-spy dump --pid <PID> --locals`; JVM `jstack <PID>`; Go `kill -QUIT <PID>`; Node
  `kill -USR1 <PID>` + inspector. In a container, ptrace-based samplers need the `SYS_PTRACE`
  capability on the service (`cap_add: [SYS_PTRACE]`); the sampler must be installed in the image.
- **Suspected blocking-sync-in-async → a blocking-call detector.** Make any sync I/O on the event
  loop fail LOUDLY at the call site: Python `blockbuster` scoped to the project's packages, or
  asyncio debug mode (`PYTHONASYNCIODEBUG=1`, slow-callback warnings); Node `--trace-sync-io`.
  Blind spots: native-extension calls; only calls on the loop thread.
- **Backing-service-adjacent → watch the wire.** Live command stream / slow log / client list of
  the service (e.g. Redis `MONITOR` — stop it fast, it is expensive — `SLOWLOG GET`, `CLIENT LIST`;
  Postgres `pg_stat_activity` + `pg_locks`). Shows key-type collisions, who writes what, stalled
  clients, lock waits.

## Phase 2 — Classify from the stacks

Place the stall in exactly one bucket:

| Stack signature | Class | Typical cause |
|---|---|---|
| A thread inside socket `recv`/`connect` on a backing service, loop thread idle | **Blocked I/O off-loop** | slow/contended backing service; check timeouts exist |
| The LOOP thread inside a sync call (socket, sleep, file) | **Blocking-in-async** | sync client call not wrapped for async; the blocking-call detector confirms |
| Loop thread healthy, a coroutine/promise parked on a queue get / await forever | **Lost wakeup / missing message** | message never sent (crashed producer — look for a swallowed exception), sent to the wrong group/channel (ID collision), or wiped (a flush from another test — only possible on a SHARED layer, i.e. wrong configuration) |
| All threads runnable, nothing progressing for seconds | **Starvation** | host oversubscription — corroborate with a resource timeline (`docker stats`, `top`) before claiming it |
| A thread waiting on a lock/executor whose owner is also waiting | **Deadlock** | executor / lock entanglement; rare — prove it with both stacks |

## Phase 3 — Mechanism → minimal fix → verify by re-driving

State the mechanism in one sentence with the stack as evidence. Fix the CAUSE minimally. Verify by
re-running the exact reproduction that failed — same configuration, same load shape. For failures
that only occur under parallel load, the validation is the full gate
(`uv run --no-project .workflow/scripts/run_full_suite.py`), not a quiet scoped run — an isolated
green cannot confirm a contention fix.

## Phase 4 — Durable capture

Mechanism + evidence + fix go into an incident note under `config: paths.planning_dir` (or extend
the existing one), a memory entry if it is a recurring class, and a commit message that names the
CAUSE, not the symptom.
