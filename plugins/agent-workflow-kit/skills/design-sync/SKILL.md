---
name: design-sync
description: Reconcile the external design owner's work — their tracker tickets, the external specification page and the design source files — into the design-question tracker, refresh the specification mirror and the design library build, file every documentation-vs-design contradiction to the design owner as one ticket, and route each ruling to the stories it affects. Use when the owner says the design owner answered, closed, commented on or designed something, or asks to check the tracker, the specification page or the design files against the project's design records.
---

# Design sync

**Configured only.** Each source is optional and says SKIPPED when unconfigured: the tracker (`config: tracker.backend`), the specification page (`config: docs_mirror.backend`), the design source (`config: design.library_dir` + `config: design.manifest`). With no design-question tracker (`config: paths.design_questions` empty) say `design-sync SKIPPED — no design-question tracker` and stop.

`<owner>` is `config: design.owner_role`; `<tag>` is `config: design.question_tag`. Every script runs as `uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/<script> ...` from the main checkout.

## Inputs

- The design-question tracker: read the whole file first. Its ticket table has the columns `Ticket | Item | Priority | Blocks | Status (<date>)`; the date in the Status header is the last sync. Answers live in dated `<owner> answers — <date>` sections; the precedence, colour and contradiction rules sit in the section the tracker names for them.
- The tracker project `config: tracker.project_key` (`config: tracker.site`).
- The specification page `config: docs_mirror.page_id` and its read-only mirror `config: docs_mirror.mirror`; the mirror's header names the version it holds.
- The design source: the design manifest (`config: design.manifest`: library and screens file keys, the screen frames) and the library build under `config: design.library_dir` (snapshot date in its `INDEX.md`).

## Steps

1. **Tracked keys.** Collect every ticket key in the table with its recorded status.
2. **Tracker.** One search: `key in (<tracked keys>) OR (project = <project_key> AND updated >= "<last sync>")` (`updated` also catches work on tickets the owner did not file), fields `summary,status,updated,created,comment`: `tracker.py search --query '<query>' --out <scratch file>`, or the MCP search tool. Then `tracker_digest.py <result file> --comments-out <scratch file>` (an MCP result: the tool-result path the harness printed, or `--session <id>`), and read the comments file whole. Read comments with `tracker.py comments --key <key>` only for the issues it marks `FETCH`. On `MORE PAGES`, fetch the next page and digest it too. For each issue whose status changed or that has comments newer than the last sync, read its comments and its history.
3. **Specification page.** `docs_mirror.py --fetch --write` (REST), or call the docs MCP page-content tool (`config: docs_mirror.mcp_tool`, markdown) and run `docs_mirror.py <result path> --write`. Read the whole diff file it names. It re-exports the mirror only when the live version is newer.
   - Exit 1: the mirror body differs at the same or an older live version; stop and ask the owner.
4. **Classify** every changed ticket and every specification change from the design owner's own words only — never infer a ruling they did not state:
   - `answered` — a ruling; quote it;
   - `designed` — points to a design node; record the node id;
   - `agrees` / `removed` — a specification change that matches the repo, or a section deleted (name every spec that cites it);
   - `no reply` — closed or moved without a comment;
   - `conflict` — restates an earlier answer differently; cite both;
   - `reversal` — contradicts a decision the owner made.
   New tickets the design owner opened become new table rows. Run `token_doc_check.py`; every `MISMATCH`, `MISSING in export` and `EXPORT-ONLY` line is a contradiction unless the tracker already records it. Exit 2 means the mirror's or the export's format no longer matches the parser: stop and report the script's message to the owner; never compare by hand instead. Exit 3 (not configured): say SKIPPED.
5. **Design source.** Refresh the library build, re-extracting only what changed:
   - `design_library.py --changes`: 2 REST list calls; it refreshes the published-component metadata and prints each NEW, REMOVED or CHANGED set and component;
   - add each new screen frame (a `designed` item, a specification change) to `design_source.frames` in the design manifest;
   - `design_library.py --plan` prints STEP 1: one `fingerprints` call per file. Run each through the design MCP's plugin-code tool with the whole `${CLAUDE_PLUGIN_ROOT}/scripts/design_extract.js` as `code` plus the printed main line, and call again with `part + 1` until `part == parts - 1`. Import with `design_library.py --from-transcripts --session <id>`;
   - `--plan` again prints STEP 2: only the dump, frame-map, flow, list and style calls and the page renders whose content changed, batched, with a call estimate. Run exactly those. Save each render with the screenshot tool (the printed nodeId and maxDimension). Right after those calls (the asset URLs expire), write one JSON map `{"<name>.png": "<asset url>"}` to the scratchpad, never the repo, keyed by the file name after `renders/_pages/` in each printed line, and run `design_page_renders.py --map <map.json>`;
   - rebuild with `design_library.py --from-transcripts --session <id>`. A refresh is complete when `--plan` lists nothing;
   - when `raw/hashes.json` is missing (first refresh, or a full re-extraction), run STEP 1 right after the complete extraction and import with `--from-transcripts --session <id> --accept-fingerprints`;
   - a changed token set: re-export it (`design_tokens.py export --tag <tag> --session <id>` after a `stylesDump` call) and rerun `token_doc_check.py`;
   - compare each new frame and master with the values in the tracker, the ticket answers and the specification. Every difference is a contradiction.
6. **Record.** Update each changed row's Status cell and the Status header date, and add one `<owner> answers — <YYYY-MM-DD>` section with the quotes, node ids, the specification changes and the contradictions. All edits in one pass.
7. **Ticket.** File one ticket for the design owner (the `design-ticket` skill's create step: `tracker.py create`, assignee `config: tracker.assignee`). It lists every documentation-vs-design contradiction found in this sync, plus every contradiction in the tracker that no ticket carries yet. For each one, give the sources and values side by side and the stories it touches. Add the ticket to the table; its Blocks cell names each touched story and the interim it ships, labelled ⚠️ (the value the precedence rule names). Nothing is settled on the project's side; the answer replaces the interim in a follow-up change.
8. **Route.** Grep the specs (`config: paths.specs_dir`) and sprint status (`config: paths.sprint_status`) for each item id, ticket key and removed specification section (`${CLAUDE_PLUGIN_ROOT}/scripts/story_refs.py` for story ids). For each in-flight story list the ruling and the phase that folds it in: its next phase, or the next review round's manifest if it is past close-out. A story whose spec is on `config: git.base_branch` and not started: add the item to the spec with its interim as `OPEN-Q <n> (<tag>, interim ⚠️, <ticket key>)`, and replace it with the ruling when it comes. A halted story: list the ruling for its spec on the story branch; the marker is replaced before the story resumes.
9. **Library components.** A ruling that names component variants means the affected specs must cite the value sheets under `<config: design.library_dir>/sets/`, every variant, not only the screen (`design_instances.py <component>` lists every drawn instance).
10. **Resume due.** Read the failure log (`config: paths.auto_dev_failure_log`): for each story's latest entry whose `- asked_of:` is `external` or `both` and whose `- external_ticket:` row in the tracker now starts with `config: design.done_marker`, list it in the report as **resume due**, with its story id and ticket. Run no command: the owner runs `${CLAUDE_PLUGIN_ROOT}/scripts/resume_halted.py <story id>`. For `both`, print the command with the owner's ruling placeholder, `--ruling '<the ruling>'` (each single quote inside written `'\''`); an `external`-only halt takes `--no-ruling`.

## Stop and ask the owner

- every `reversal` and `conflict`, before any spec changes;
- a contradiction whose answer could reverse an owner decision, before the ticket is filed;
- a ruling whose colour or value would be applied to a page other than the one it answered;
- the design MCP is not connected, or its account check (`whoami` or the server's equivalent, run before the first plugin-code call) shows a seat with a small monthly call quota: ask the owner to reconnect on a seat with quota. REST mode (`design_library.py` without flags) needs the owner's OK first when the REST token's quota is small.

## Report

Changed tickets and specification changes by class, the mirror version and library snapshot written, the ticket filed, and the per-story routing list. End with the reversals and conflicts as one batched question.
