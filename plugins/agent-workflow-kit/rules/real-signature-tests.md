---
name: real-signature-tests
description: MANDATORY. Any code talking to a real dependency (database, graph/vector store, LLM provider, task queue, message broker, external SDK) needs a live-dependency integration test or a contract-faithful fake. A bare auto-mocking double for that boundary is BANNED.
---
# Real-Signature Tests at Infra Boundaries

**Status: HARD RULE.**

## The failure mode
A test double (`MagicMock()`, `patch(..., return_value=...)`, `jest.fn()`, a hand fake) stands in for
a real dependency and **manufactures the method / signature / return shape** the real dependency may
not have — so a runtime mismatch passes green. Auto-mocks auto-vivify any attribute; making every call
succeed is their purpose, which is exactly what hides "the real API changed / never matched".
Typical escapes: a keyword argument every real provider rejects; a method the real adapter does not
have; a property type the real store refuses; a scope/tenant filter the real query engine interprets
differently.

## The rule
**Any code that talks to a real dependency at a boundary MUST have a real-signature test:**

1. An **integration test against the real dependency**, isolated per test (unique per-test token on
   every seed, read-back and cleanup; no import-time connections — see `parallel-test-isolation.md`),
   **OR**
2. A **contract-faithful fake** that mirrors **only** the real surface — or the real client object
   constructed and asserted for its call arguments without a network call.

**BANNED:** a bare auto-mocking double (`MagicMock()` / `patch(return_value=...)` / equivalent) as the
double for the database/graph adapter, the LLM client, or the vector/search client in the path under
test.

## LLMs in tests
- A real or paid LLM is **forbidden** in tests.
- Use the project's deterministic mock provider (pattern-matched canned responses) that returns the
  same object types as a real provider, so it composes into retry/fallback/structured-output chains
  exactly like the real one. Do not stub structured output with an auto-mock.

## In review
Mock-only coverage of an infra boundary is a **gap to flag**, not sufficient coverage. Be especially
suspicious of a `try/except` around an infra call that returns an empty/default object — it converts a
hard runtime failure into a silent no-op that no mock test will catch.
