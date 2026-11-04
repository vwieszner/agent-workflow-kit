---
name: ui-state-reach
description: Writes a spec's "UI states and how each is reached" table and hands each MISSING row to the project's seed skill. Run by the spec agent for a story that changes frontend source.
user-invocable: false
---

# UI state reach

Decide, at spec time, how the ui-verifier reaches every UI state the spec draws, and settle each state no method reaches before the story is implemented.

## Inputs (Skill args)

- `spec`: the spec file.
- `worktree`: the worktree root.
- `graph_project`: every code-graph call passes it (`none`: grep/read only).
- `mode`: `checkpoint` or `auto`.

## The profile

It is the file `config: review.ui_profile` names (the worktree's copy), whose `##` headings equal the first column of the `ui-verifier` agent's `## The project profile` table (`.claude/agents/ui-verifier.md` or `.opencode/agents/ui-verifier.md`; "the core" below is that agent).
- Read the profile in full, the agent's "Actors", "Reaching a state" and "WebSocket and fault states" sections, and the toolkit header the profile's Toolkit section names.
- With `config: review.ui_profile` empty or the file absent, write no table and return `no UI profile`.

## The states

- Include every state the spec's UI ACs name (empty, loading, error, populated and each named variant), every drawn variant the spec names, and every state the tests the spec plans drive.
- Each gets one row and an id `U1`, `U2`, … in spec order. A state named twice is one row.

## Each state's needs

Cite each need as `path:line`. Three sources:
- **The spec:** the AC text, what the screen shows, who acts and what triggers the state.
- **The components** the spec creates or changes for the state, under the profile's mount-point search root: what they render the state from (props, the requests they send, the frames they handle, the flags they read). Structure comes from the code graph (`graph_project`), then a Read of the cited lines.
- **The E2E test that drives the state:** an existing spec under the profile's E2E spec root that drives the screen (the specs importing its page object from the profile's page-object directory), else the E2E test the spec plans. From it read the data its setup creates and through which call, the roles its users act as, the frames it waits for and the requests it routes. A setup step that calls the API is a need: the verifier meets it through the UI as an acting actor, or through seeded or shared data, never through the API.

**Role:** the core's "Which actor" rule, unchanged.

## Method

Take the first method that reaches the state, in the core's order, plus the moves it needs. A row reached by several methods joins them with ` + ` in the core's order.
- `seeded`: a profile Seeded states row shows the state. Name the row and its actor.
- `created`: an acting actor of the role creates the data or client state through the UI within its scope. Name the actor and the UI steps, translated from the E2E setup.
- `shared_data`: the profile's Shared data offers the data to an acting actor of the role. Name the entry and the actor.
- `websocket_or_fault`: a toolkit move: hold (`uivHold`, `uivRelease`); HTTP hold, hold-response, fail or abort (`uivRoute`, with `uivRouteReleaseOne` to deliver one held response of a hold-response route at a time); drop (`uivDrop`); inject (`uivSend`, with a frame per the profile's Frame provenance); rewrite a real frame (`uivRewrite`); an app hook the profile names (`uivHook`); second user (`uivSecondOpen`); the second context's socket moves (`uivSecondHold`, `uivSecondRelease`, `uivSecondDrop`, `uivSecondSend`).
- `rule`: a core rule places the state in `out_of_scope` without reaching it, for example a layout state at a viewport that neither the spec names nor the profile's Viewports requires. Name the rule.
- `MISSING`: nothing above reaches it, or a core rule would place it in `unreached`: no acting actor of its role, the second context's viewport, an undetermined role, a frame or response that only a fabricated body would supply. "How it is reached" then names the needs: the data, the acting role, the frame or the viewport.

## The table

A spec section `## UI states and how each is reached`, placed after `## Acceptance Criteria`, with this header verbatim:

`| # | State | Sources | Role | Method | How it is reached | Resolution |`

- Resolution is `—` on a reached row. On a `MISSING` row it is `AC<n> (seed)` or `[OPEN-Q <n>]`; a new `<n>` continues from the spec's highest existing OPEN-Q number.
- A spec that already has the section gets it rewritten. On a rewrite, a `MISSING` row whose Resolution already names an `AC<n> (seed)` or an `OPEN-Q <n>`, ruled or not, keeps it and is not passed to the seed skill.

## MISSING rows go to the seed skill

The profile's Seed section line `Seed skill: <name>` names the skill.
- Invoke it (the skill tool), passing `spec`, `worktree`, `graph_project`, `mode` and the MISSING row ids. It writes a seed AC or a question for each row and returns which. Record each in the row's Resolution.
- With `Seed skill: none`, each MISSING row becomes an `[OPEN-Q <n>]` (`<n>` continues from the spec's highest existing OPEN-Q number) in the spec's open questions, naming its needs.
- With no `Seed skill:` line, or a named skill that cannot be invoked, handle the rows as `Seed skill: none`, and each row's question says which of the two happened.

## Return

- `missing_states:`, one line per MISSING row: `U<n> <state> — needs <needs> → <resolution>`;
- or `none`, or `no UI profile`.
