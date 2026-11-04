---
name: ui-profile
description: Write or update the project's UI profile — the file `config: review.ui_profile` names, which the ui-verifier agent, the ui-state-reach skill and ui-design-compare read (actors, seeded states, shared data, viewports, paths, toolkit, screens and flows, frame provenance, known defects, base URL, seed). Use when the owner asks to set up UI verification, when a story adds an actor, a seeded state, an app hook or a known defect, or when the ui-verifier reports a profile gap.
---

# UI profile

The profile is a project file (`config: review.ui_profile`, committed). Its `##` headings are exactly the first column of the `ui-verifier` agent's `## The project profile` table, in that order; `template.md` beside this file is the skeleton with what each section must hold. The ui-verifier treats every hard rule of its own as an invariant: a profile entry never relaxes one.

## Creating the profile

1. Copy `template.md` (this skill's directory) to the path `config: review.ui_profile` names, or ask the owner for a path and set the key.
2. Fill every section from the code and the seed, citing `path:line` for every route, test id, hook and frame builder. Never invent a value: read the router, the login form, the seed module (`config: ui.seed_module`), the E2E page objects and the socket/frame producers first.
3. Credentials: write where they are defined (`config: ui.credentials_source`), never the secret itself unless the project's test password is already a committed fixture.
4. `## Seed` names the seed command (`config: stack.seed_cmd`, `config: review.ui_seed_cmd`) and the seed skill: `Seed skill: ui-seed` (the kit's), the project's own seed skill, or `Seed skill: none`.

## Updating it

- A story that adds an actor, seeded state, shared-data rule, app hook or known defect updates the profile in the same story (its seed AC's "Profile" line names the rows).
- A seed step adds its Seeded states rows and its seed signal line (which failed login means which seed step is absent).
- A known defect is listed only with the story or ticket that tracks it; remove it when that lands.
- Keep the file operative: rules, rows, and recipes. No history.
