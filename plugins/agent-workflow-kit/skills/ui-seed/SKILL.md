---
name: ui-seed
description: The kit's seed skill for the ui-state-reach skill — turns each MISSING UI state into a seed AC in the same story (a step in the project's UI-state seed plus its UI profile entry) or an open question, under the seed's conventions. Named by the UI profile's `Seed skill: ui-seed` line.
user-invocable: false
---

# UI seed

**Configured only.** The project's UI-state seed is `config: ui.seed_module` (the module that creates the data UI states need, run by `config: stack.seed_cmd`), its tests `config: ui.seed_tests`, its project-specific conventions `config: ui.seed_rules` (a doc; optional). With `config: ui.seed_module` empty, every MISSING row becomes a question (below) whose condition is "the project has no UI-state seed".

Inputs (skill args): `spec`, `worktree`, `graph_project`, `mode` and the MISSING row ids. Read each row's needs and sources from the spec's "UI states and how each is reached" table. Read the seed module's header docstring and `config: ui.seed_rules` in full first. For each row write a seed AC into the spec's `## Acceptance Criteria`, or a question, and return which.

## A seed AC, when all four hold

- A production code path on the story's branch writes the data the way a real user session does (a service, or the model write the production path makes), with checked-in fixture text in place of any LLM output and no fabricated response body.
- The data is new rows of the seed's own (its own users and records, at identifiers the seed reserves), so no seeded or shared data or rule changes.
- An actor the UI profile already lists reaches the state on that data.
- The AC fits the story: a seed AC is a standard section and does not count toward `config: pipeline.max_behaviour_acs`.

A state behind a UI flag that no story stack turns on follows the project's flag rule in `config: ui.seed_rules`.

## Otherwise a question

`[OPEN-Q <n>]` (`<n>` continues from the spec's highest existing OPEN-Q number), naming the need, the condition above that fails, and the options. Cases:
- a new actor or a wider scope;
- a change to existing seeded or shared data or its rules;
- data that only an LLM call or a fabricated response produces;
- a state the story's own code cannot reach yet.

A question is the owner's call in both modes. A ruling that picks a seed option is written as a seed AC with the template below by whoever applies the ruling, and the reach-table row's Resolution becomes `AC<n> (seed)`.

## The seed's conventions

- **Step contract** (the seed module's docstring is authoritative where it is more specific):
  - one step per state's data, appended to the seed's ordered step list;
  - it checks its own state first: absent → `created`, complete → `complete`, present but altered → a failure naming the step, what is altered and the remedy;
  - read-only data is never repaired;
  - an acting actor's data is restored at re-up, reported `restored`.
- **Writes:**
  - through the production path the module docstring lists, inside whatever write scope the project's rules require;
  - no LLM call (the seed's tests assert it);
  - a dedicated user takes the seed's shared test password and the profile fields the seed gives every seeded user;
  - users the stack bootstrap creates are never created or changed by the seed.
- **Reserved identifiers:** every explicit primary key or id comes from the range the seed reserves; a new step takes the next free offset or sub-range and pins it in the seed's range test. Never reuse a taken offset.
- **AC number:** never reuse one. A replaced seed AC, or one whose seed steps change, takes a new number: the ledger identifies a seed AC by its number alone.
- **Idempotent re-up:** a second run reports `complete` for every non-acting step and changes no count.
- **Tests**, in `config: ui.seed_tests`: the step order, the created shape, the second run, one failure test per altered state (named by step and what is altered), the identifier pin and, for acting data, the restore.
- **Profile entry** (`config: review.ui_profile`):
  - a Seeded states row per state (actor, route, action, expect);
  - the seed signal ("When `<user>` cannot log in but `<user of the previous step>` can, the seed predates …");
  - for data an acting actor may change, its Shared data rules.
- **Gate:** a story with a seed AC changes app code, so the pre-merge full gate applies, and it never records `pre-merge gate not required`.
- **Parallel stories:** two in-flight stories that add steps conflict in the step list and the order test. The second to land rebases and re-checks its identifier range against `config: git.base_branch`.

## The seed AC template

```
### AC<n> — Seed: <state> (standard section)
- Step: `<step name>` in `<config: ui.seed_module>`, after `<previous step>`; ids `<offset or sub-range>`.
- Writes: <what>, through <service/function, file:line>; no LLM call.
- Contract: absent → created; complete → nothing; <each altered state> → FAILED naming it with the remedy | restored at re-up (acting data).
- Profile: §Seeded states row(s) <actor, route, action, expect>; seed signal <line>; §Shared data rules <if acting>.
- Tests: <the cases of the conventions above>.
- Gate: the pre-merge full gate applies.
- Reaches: U<n> (the table row).
```
