---
name: root-cause-before-fix
description: MANDATORY. Prove the root cause before fixing. No unproved fixes, no test-weakening, no silent deferral, no symptom suppression. Use whenever something fails — a test, a gate, a runtime error, a flake, behaviour that does not match the spec.
---
# Root Cause Before Fix

## Rule
A fix may not be applied until the **mechanism** of the failure has been demonstrated, not theorised.

## The four prohibitions
1. **No unproved fix.** Do not change code because a change *might* help. If you cannot state the
   causal chain — *this input → this code path → this wrong state → this observed failure* — you have
   not found the cause.
2. **No test-weakening.** Never relax an assertion, widen a tolerance, add a retry, mark
   `skip`/`xfail`, or delete a test to make a failure go away. The test encodes the requirement; if the
   implementation disagrees, the implementation changes. Changing a test is legitimate **only** when
   the test itself is proved wrong — and that proof goes in the commit message.
3. **No silent deferral.** "This code is never called", "nothing uses it yet", "we'll wire it later"
   does not close a finding. Unreached code is either finished and wired, or removed. Tech debt is not
   a triage outcome.
4. **No symptom suppression.** Swallowing an exception, adding a sleep, bumping a timeout, pinning
   around a bug, or catching-and-continuing to turn a red signal green is forbidden as a *fix*.

## What counts as proof
At least one of:
- A failing run that flips to passing when — and only when — the identified cause is neutralised.
- Instrumented output (log line, captured value, DB read, packet capture) showing the wrong state at
  the moment it is produced.
- A deterministic reproduction: the seed, ordering, or input that makes the failure happen on demand.

A hypothesis that merely *fits* the symptoms is not proof. Refuting alternatives strengthens a
hypothesis; it does not substitute for demonstrating the mechanism.

## Mitigations — allowed only when labelled
A temporary mitigation may ship when the cause work cannot land now, on three conditions: it is called
a mitigation in the code comment AND the commit message; the cause investigation is written down
(incident doc or story); the owner agreed. An unlabelled mitigation is a workaround.

## When you cannot prove it
Say so plainly: "I have NOT proved the root cause. Ruled out: <…>. Next I would check: <…>." Then ask
the owner. Do not fill the gap with a plausible fix.

## Scope
All failures: backend, frontend, infra, gates, tooling. `e2e-debugging-protocol.md` is the concrete
recipe for E2E failures; the `flaky-test-hunter` and `hang-diagnoser` skills are the recipes for
flakes and hangs. This rule is the general form.
