# UI profile — <project>

<!-- The project's UI profile (config: review.ui_profile). Section headings must stay exactly as below:
     the ui-verifier agent, the ui-state-reach skill and ui-design-compare look them up by name.
     Replace every <placeholder>; cite path:line for routes, test ids, hooks and frame builders. -->

## Login and actors

**Login.** The route is `<login route>`; the form is `<username test id>`, `<password test id>` and `<submit test id>`; a logged-in page shows `<landing test id>`. Passwords: `<where defined — config: ui.credentials_source>`.

| Actor | Role | Capability | Scope | Reset |
|---|---|---|---|---|
| `<user>` | <role> | acting | <the data it may create or change through the UI, and where> | <what restores it: re-up, cleanup> |
| `<user>` | <role> | observe-only | — | the next re-up fails on altered seeded data |
| `visitor` | visitor (signed out) | observe-only | — | none: signing out writes no data |

**Visitor.** <how a signed-out state is reached on your own page: which page renders the sign-out control>.

## Seeded states

<Seeded data summary: the seeded records and their ids.>

| Seeded state | Log in as | Route | Action | Expect |
|---|---|---|---|---|
| <state> | `<user>` | `<route>` | <none, or the UI steps> | `<test id>` and what it shows |

**Unseeded stacks.** <the seed signals: "When `<user>` cannot log in but `<user of the previous step>` can, the seed predates <step>: every <step> row goes to `unreached` with the reason ...">

The stack's seed status line is in <where: the bring-up log, the `--mode up` output>.

## Shared data

### <shared record>

<Who runs it, which actors may write what, what must never change (each change fails the next re-up), and how a round finds it left by an earlier round.>

## Viewports

Default: <W>x<H>, set with the browser resize tool. The second context opens at it.

Required: <the viewports every UI story is checked at>.

## Paths

- **Fixture roots:** <upload fixtures>
- **Screenshot root:** `<worktree_path>/.playwright-mcp/round-<round>/`, absolute, forward slashes (the browser MCP's allowed output root; `.workflow/hooks/guards/playwright_paths.py` denies any other save path).
- **Page-object directory:** `<worktree_path>/<E2E page objects>`
- **Mount-point search root:** `<frontend source root>`
- **E2E spec root:** `<worktree_path>/<E2E specs>`

## Toolkit

- **Toolkit:** `<path of the ui-verifier toolkit file, relative to the browser workspace root>`
- **Toolkit configuration:** `<path of its configuration file>`

**App hooks** (`window.uivHooks`, registered by the development build only; `uivHook(name)` calls one):
- `<hook>` (<read|write>): <what it starts and when it is used>.

## Screens and flows

**Navigation.** <shells without a global navbar, where links are clicked from>.

**Load signal.** <the attribute each screen sets when it is ready, and its error state>.

**Hidden markers.** <test ids that are aria-hidden state markers>.

<Per-flow notes: documents, sessions, real frames, and worked examples of the moves (hold, drop, inject, press, routed responses).>

## Frame provenance

<The producer code an injected socket frame is built from: builder functions with path:line, and the rule that an injected frame is either captured from this stack's traffic in this run or built from that code.>

## Known defects

- <what the screen shows that is not a deviation, with the story or ticket that tracks it>

## Base URL

`ui_base_url` comes from `config: review.ui_base_url`, rendered for the story stack by `.workflow/scripts/review_stage.py`.

## Seed

Story stacks are seeded at bring-up (`config: stack.seed_cmd`); the review seeds again before every round with a UI check (`config: review.ui_seed_cmd`); the verifier itself never seeds.
Seed skill: ui-seed
