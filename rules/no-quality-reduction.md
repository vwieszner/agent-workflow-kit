---
name: no-quality-reduction
description: MANDATORY. Never reduce product, code, or functional quality to make the task simpler — in specs, tests, fixes, or implementation. Use whenever the easier version of a task is not the same thing as the asked-for version.
---
# No Quality Reduction For Convenience

## Rule
**The scope, coverage, and correctness of the deliverable are set by the requirement — never by what
is convenient to build, test, or verify.** When the full thing is too large, too slow, or genuinely
blocked, say so and let the owner decide. Never silently deliver the narrower thing.

The tell: *the task got easier for me, and nobody chose that.*

## By phase

### Specifications
- ❌ Dropping, narrowing, or softening an AC because it is hard to implement.
- ❌ Writing an AC vaguely so that a weaker implementation would satisfy it.
- ❌ Declaring the hard part "out of scope" without surfacing it as a descope decision.
- ❌ Speccing what is easy to build instead of what the feature requires.
- ✅ The spec states the real requirement. A split or descope is proposed explicitly, with the
  cascade impact, and the owner decides.

### Testing
- ❌ Asserting less than the requirement, so the test is easier to write or keep green.
- ❌ Covering the happy path and skipping the branch that is awkward to set up.
- ❌ A double that manufactures the behaviour under test (see `real-signature-tests.md`).
- ❌ Rewriting a test to match what the implementation currently does (see `root-cause-before-fix.md`).
- ✅ The test encodes the requirement, including the parts that are inconvenient to reach.

### Fixing
- ❌ Fixing the one call-site you can reach when the defect is in the shared path.
- ❌ Fixing the reported instance and leaving the same bug in its siblings.
- ❌ Narrowing a fix's blast radius to avoid re-verifying the broader surface.
- ✅ The fix lands where the defect lives, and covers every site the cause reaches.

### Implementation
- ❌ Implementing a subset and reporting the task as complete.
- ❌ Special-casing where the general case was required.
- ❌ Dropping fields, states, error paths, or edge handling because they complicate the code.
- ❌ Scoping a tool or feature to the easy half of its domain, then noting the gap as a "risk" and
  shipping anyway.
- ✅ What ships does the whole job, or the shortfall is named to the owner before it ships — not after.

## Before a recommendation or decision
Ask, explicitly: **does this reduce the quality of the product, the functionality, or the code?**

If yes, it is not a recommendation — it is a **descope proposal**, surfaced as one with the cost
named. The answer belongs in the reasoning stated up front (`goal-and-reasoning.md`), not in a
footnote after the choice is made.

Applies to: recommending an option, picking what to work on next, proposing to remove a check, test,
rule, or guardrail, choosing the cheaper approach, declaring something out of scope, accepting a
workaround.

- ❌ Recommending the easier option without naming what it gives up.
- ❌ Proposing to delete a rule, test, or check because it is not currently being followed, or is
  inconvenient to satisfy. **Non-enforcement is not evidence that a guard is wrong** — it is evidence
  the guard was never wired up. Fix the wiring or narrow the scope; do not remove the protection.
- ❌ Presenting a narrowed version as if it were the asked-for thing.
- ✅ "Option A is cheaper; it gives up X. Option B costs more and keeps X. Which do you want?"

## Surface, don't shrink
Too big, too slow, blocked, or not worth it are all legitimate. Each is a **conversation**, not a
unilateral trim:

> "The full version needs X. That is <cost/risk>. I can do <reduced version> instead — that leaves
> <specific gap>. Which do you want?"

Scaling the work down is the owner's call. Delivering the reduced version and describing the gap in a
footnote is taking that call, not asking for it.

## What this is NOT
- **Not the opposite of "flag over-engineering smells"** (`AGENTS.md`). That rule is about not
  building machinery nobody asked for. This rule is about not degrading a thing that *was* asked for.
- **Not a mandate to gold-plate.** The bar is the requirement.
- **Not a reason to expand scope.** Surgical changes still hold: do the asked-for thing completely,
  and nothing adjacent.
