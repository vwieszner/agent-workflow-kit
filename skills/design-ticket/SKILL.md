---
name: design-ticket
description: File a story's questions for the external design owner on the story's one tracker ticket — find or create the ticket, post only the questions it does not already carry, add the ticket's row to the design-question tracker and commit only that row, and rewrite the spec's bracketed design-owner markers. Use whenever a story has design-owner questions — spec review (parallel-dev-wave Checkpoint 1), auto-dev-loop's design filing and halts, questions raised at code-review triage — or when the owner asks to file, ask or send a story's questions to the design owner.
---

# Design ticket

**Configured only.** With `config: design.question_tag` (or `config: spec.external_question_tags`) empty, say `design-ticket SKIPPED — no external design owner configured` and stop: every question is the owner's. With `config: tracker.backend = "none"`, say `design-ticket SKIPPED — no issue tracker configured`, list the questions for the owner, and stop.

One ticket per story in the tracker project `config: tracker.project_key`; story and ticket map one to one. The design owner is `config: design.owner_role`; their markers carry the tag `<tag>` = `config: design.question_tag`. Every deterministic step goes through `.workflow/scripts/design_ticket.py` (`uv run --no-project .workflow/scripts/design_ticket.py <subcommand>`, run from the main checkout). Tracker calls go through `.workflow/scripts/tracker.py` (`config: tracker.backend = "jira"`) or, with `config: tracker.backend = "mcp"`, the tracker MCP tools named in `config: tracker.mcp_tools` (load them with the tool-search tool first when the client defers them).

## Inputs

- `story_id`.
- The `(<tag>)` questions. The design owner's: a design value, a documentation-vs-design contradiction, an undrawn state, routing, a click target or other screen behaviour. A question that is both the owner's and the design owner's is the design owner's.
- An **interim** per question: the value the story ships, marked ⚠️, until the design owner rules. It is the value the precedence rule in the design-question tracker (`config: paths.design_questions`) names; an undrawn state derives from the design owner's palette as a named token. When no rule names one, the interim is the owner's call: get it from them before filing. In auto-dev-loop: a value or a state takes the interim story-spec derived from the palette, the design library components (`config: design.library_dir`) or the ticket text; a missing layout or screen is filed with the interim `none — the story waits for the design`.
- `origin`: who raised the questions and where, e.g. `the owner, 3-4 Checkpoint 2` or `auto-dev, the 3-4 Step 4 filing`.
- The spec path, when the questions are `[OPEN-Q <n>] (<tag>)` markers in a spec.

Not asked: a question the design owner's published rules already answer (the specification page's token tables, a ruling recorded in the design-question tracker). It goes under `notes` with the rule and the value it gives.

## Steps

1. **Find the ticket:** `design_ticket.py label <story_id>` prints the label and the search query. Search with it: `tracker.py search --query '<query>' --out <scratch file>` (or the MCP search tool with the description and comments requested), then read the result.

2. **Write the questions file** to the scratchpad: UTF-8 JSON in the shape the script's header documents (`story_id`, `topic`, `builds`, `phase`, `what_this_is`, `questions[]` with `title`, `body`, `nodes`, `sources`, `options`, `ask`, `interim`, and optional `notes`).
   - `what_this_is`: what the story builds, from which library components (node ids).
   - Per question: its node ids, its sources side by side with their values, the options, the ask, the interim.
   - `phase`: the phase number when the story's sprint-status note names one, else `null`.
   - Name no engineering choice: no scope, structure, code names, file paths or review-finding ids. Write in words a designer reads.

3. **Post:**
   - **No ticket:** `design_ticket.py render <file>`, then create the issue with its `summary`, `labels` and `description` (`tracker.py create --summary ... --description-file <file> --label ...`; issue type, priority and assignee default from `config: tracker.*`).
   - **Ticket found:** drop every question its description or comments already carry; post none twice. None left: skip to step 5. Otherwise `design_ticket.py render <file> --comment --start <next question number on the ticket>` and add its `comment` (`tracker.py comment --key <key> --body-file <file>`).
   - **A failed tracker call:** stop and report the error verbatim. Write nothing else. Under auto-dev-loop, follow its §"Design ticket" failure rule.

4. **Tracker row** (with `config: paths.design_questions` set; otherwise the script prints `skipped`):
   - **New ticket:** `design_ticket.py tracker-row --ticket <key> --item "<topic> (<story_id>): Q1 <short>; Q2 <short>; …" --blocks "<story_id> ships the interims, ⚠️: Q1 <interim>; …" --origin "<origin>"`.
   - **Existing ticket with new questions:** extend that row's Item and Blocks cells with an edit.
   - **Commit:** `design_ticket.py commit-tracker --ticket <key> --message "<subject>` + a blank line + the session's commit attribution lines + `"`. It commits only the tracker lines that carry `[<key>]`, only on `config: git.base_branch`, only with an empty index.
   - **Refusal `another session's changes stayed staged`:** another session is mid-commit. Re-run once that index is empty; never commit their staged changes.
   - **Auto-dev-loop halt:** skip this step. `halt_release.py --external-ticket <key> --external-item "<Item cell>"` writes and commits the row.

5. **Spec markers:** `design_ticket.py mark-spec --spec <spec path> --ticket <key> --q <n,…>`, or `--all` for every bracketed marker of the tag; a question filed with the interim `none — the story waits for the design` is left out of `--q` and its marker stays bracketed. It rewrites `[OPEN-Q <n>] (<tag>)` as `OPEN-Q <n> (<tag>, interim ⚠️, <key>)`. A question that is not a spec marker, such as one raised at code-review triage, is recorded with its ticket question number (`<key> Q<n>`) where the decision is recorded: the findings file's `## Triage decisions` section, the deferred-work entry, or the spec's in-flight change.

6. **Report:** the ticket key and link, the questions posted (`Q<n>`), the tracker commit, and the markers rewritten.
