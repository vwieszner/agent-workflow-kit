---
name: parallel-test-isolation
description: MANDATORY. New code and new tests must be safe under the parallel full gate (forked worker processes, per-worker databases, shared external services, parallel E2E workers). Deferred connections, no cross-test module state, per-test isolation tokens for shared live infra, per-worker E2E account shards.
---
# Parallel Test Isolation

**Status: HARD RULE.** The full gate (`.workflow/scripts/run_full_suite.py`) runs backend tests in
parallel worker processes (typically forked from a primed parent, each with its own cloned/reused test
database) and E2E tests with multiple workers. Scoped single-process runs cannot catch violations;
only the full gate does.

## Backend / service code
- **Defer connection work to first use.** No import-time database / cache / queue / graph / vector /
  HTTP client construction.
- **No module-level singletons holding live connections.** Services are instantiated per request or
  per test. A connection inherited across `fork()` is shared by every worker.
- **No cross-test state in module-level dicts/lists** in tests.

## Tests against shared live infrastructure
External services that are NOT cloned per worker (a single graph DB, vector store, broker, object
store) are shared by every worker.
- Every seed, read-back and cleanup MUST be keyed on a **per-test unique token** (e.g. a `uuid4` tag)
  supplied by the project's live-infra test base (`config: tests_policy.live_infra_test_bases`).
- **Never key isolation on a bare database primary key** unless that key's sequence is proven
  disjoint per worker (cloned worker DBs restart sequences, so two workers mint the same ids). Check
  the runner's actual banding, do not assume.
- A scope filter applied after the search (post-seek filter) is not isolation.
- When workers share one collection/namespace, access it only through the project's isolation wrapper
  that enforces the per-worker partition on every read — never a raw client. A test needing a raw
  client or its own schema creates its own uniquely named collection/namespace and deletes it.

## E2E
- Tests consume **per-worker account shards** via the worker-scoped fixture
  (`config: tests_policy.e2e_worker_fixture`) — never share an account across workers; bulk-delete
  helpers race.
- Raising worker capacity requires bumping every knob in `config: tests_policy.e2e_capacity_knobs`
  together; one without the other silently under-seeds.

## When to run the full gate before merge
Run it whenever the change can touch the parallel surface:
- Shared infra: settings/config modules, app startup hooks, fork handlers, test runner/fixtures/
  conftest, worker/server/queue config.
- A new connection-holding singleton or module-level client.
- Seed commands, E2E config, shared E2E helpers, or any worker-shard plumbing.
- The end of a wave (combined diffs interact).

Pure feature stories touching only their own module + tests: scoped green + post-merge smoke suffice.
When in doubt, run the full gate.
