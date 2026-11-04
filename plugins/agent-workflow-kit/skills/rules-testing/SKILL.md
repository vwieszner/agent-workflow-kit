---
name: rules-testing
description: The project's test rules a diff's tests are judged against — real-signature doubles, no test weakening, parallel isolation, E2E and unit-runner conventions. Preloaded into review agents that run without the always-loaded instructions.
user-invocable: false
---

A test that breaks one of these rules does not verify what it claims. Full rules: `${CLAUDE_PLUGIN_ROOT}/rules/real-signature-tests.md`, `${CLAUDE_PLUGIN_ROOT}/rules/test-guide.md`, `${CLAUDE_PLUGIN_ROOT}/rules/parallel-test-isolation.md`, `${CLAUDE_PLUGIN_ROOT}/rules/root-cause-before-fix.md`, and the project's own rules in `.workflow/rules/local/`.

- **Real-signature doubles at infra boundaries.** Code that talks to a database, graph or vector store, an LLM provider, a queue or an external SDK needs a live-dependency integration test or a contract-faithful fake that mirrors only the real surface. A bare `MagicMock()` / `patch(return_value=...)` standing in for that adapter in the path under test is a defect. LLM-needing tests use the project's deterministic mock provider.
- **No test weakening.** A relaxed assertion, widened tolerance, added retry, `skip` / `xfail`, deleted test or raised timeout that makes a failure go away is a defect.
- **Shared live infrastructure.** A test on a service not cloned per worker inherits the live-infra base (`config: tests_policy.live_infra_test_bases`) and keys every seed, read-back and cleanup on its per-test unique token — never on a bare primary key whose sequence is not proven disjoint per worker. Access to a shared collection goes through the project's isolation wrapper, never a raw client. No cross-test state in module-level dicts or lists.
- **E2E.** Page objects (`config: tests_policy.e2e_page_objects_dir`) and `config: tests_policy.e2e_locator_attr` locators only; accounts come from the worker-scoped fixture (`config: tests_policy.e2e_worker_fixture`), never one account shared across workers.
- **Unit runner.** Worker counts stay capped at the gate's joint worker budget; runner-wide test, hook and wait timeouts are never raised to absorb timeouts. A per-test timeout is the last resort, under `${CLAUDE_PLUGIN_ROOT}/rules/parallel-test-isolation.md`'s per-test-timeout criterion; an unattributed residual goes to the owner.
