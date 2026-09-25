---
name: e2e-debugging-protocol
description: MANDATORY whenever an E2E test fails or E2E-related infrastructure (containers, proxy, test hooks) is modified. Lifecycle mandate, tri-log root-cause search, visual evidence, strike-limit stop policy, strict test-id locator policy.
---
# E2E Debugging and Lifecycle Protocol

## 1. Lifecycle mandate (keep the stack in sync with the change)
| Change | Required action |
|---|---|
| Source edit | Confirm live sync picked it up (`live-sync.md`); restart the service if it does not hot-reload |
| Dependency manifest / lockfile | Rebuild images (`config: env.dependency_rebuild_cmd`) and bring the stack up again |
| Environment file / settings module | Recreate the affected services (`down` + `up`, with every port env var) |

## 2. Reproduce first
Before fixing any E2E failure, reproduce it in the live environment with the browser tools (at
`config: ui.base_url`). Prove the bug exists before touching code.

## 3. Tri-log root-cause search
Before attempting a fix, examine and cite:
1. **App/backend log:** `{compose} -p <stack> logs --tail=100 <app service>` (5xx, tracebacks).
2. **Proxy log:** `{compose} -p <stack> logs <proxy service>` (gateway / websocket-upgrade errors).
3. **Browser console:** all console messages via the browser MCP (JS errors, refused connections,
   hot-reload failures). If the browser tools are deferred, load their schemas first.

## 4. Visual and state evidence
- Snapshot or screenshot AT the failure point.
- Use any in-app debug surface the project exposes (connection status, state dump) to confirm the
  real state.
- Compare the captured UI with the component code before guessing selectors.

## 5. Strike-limit stop policy
If a fix fails `config: tests_policy.e2e_strike_limit` times (default 3): STOP, revert all guessed
changes, and present a **failure packet** (logs + screenshot + state evidence) to the owner.
Never delete or skip tests to "fix" a failure.

## 6. Locator policy (when `config: tests_policy.e2e_locator_attr` is set)
1. The test-id attribute (`config: tests_policy.e2e_locator_attr`) is the ONLY permitted locator.
2. To verify content (text, label, role, visibility), locate by test id first, then assert the
   content afterwards.
3. ❌ Locating by role, label, text, generated CSS classes, or DOM structure.
4. Interactions go through page-object classes (`config: tests_policy.e2e_page_objects_dir`); no raw
   selectors inside spec files.
5. Assert state (enabled/visible) before interacting.
6. Wait on the network (response / websocket events), not arbitrary timeouts or bare DOM rendering.
7. Test setup is API-driven in the before-each hook; only the behaviour under test is UI-driven.
