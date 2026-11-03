---
name: ui-design-compare
description: Compare the live app against the design's screen frames, one batch subagent at a time, with the verdict vocabulary, the owning-story lookup and park-and-resume. Use when the owner asks whether the screens match the designs, asks for a full comparison across screens, or asks what the app is missing against the design.
---

# ui-design-compare

**Configured only.** Needs a design library build (`config: design.library_dir` with `frame-map/`), a UI profile (`config: review.ui_profile`) and the browser MCP (`config: browser.mcp_server`). With any missing, say `ui-design-compare SKIPPED — <what is missing>` and stop.

## Inputs
- Frame list: `<config: design.library_dir>/frame-map/<slug>.md`, one per frame. The node id and frame name come from the file.
- Design image: `<config: design.intake_dir>/assets/<slug>.png`. If absent, use `<config: design.library_dir>/renders/_screens/<slug>.png` and label the verdict `reference: local render` with the file name. If neither exists, the verdict is MISSING-DESIGN.
- Base URL: the shared stack is `config: ui.base_url`. A story stack is that URL on the slot's proxy port (the `ports:` line of its `story_setup.py` report; the same value `review_stage.py` derives as `ui_base_url` from `config: review.ui_base_url`).
- Users, routes and seeded data: the UI profile's §"Login and actors" and §"Seeded states", and the seed the profile's §Seed names. Read the username and password from those files (`config: ui.credentials_source`) before typing them. Never type a credential from memory and never write a credential into a file. A frame outside that table: find its route in the app's router.
- Seed state: story stacks are seeded at bring-up (`config: stack.seed_cmd`). Only the main session re-seeds a stack, and only with the owner's approval. Agents never seed or create data.

## Batches
- Split the frames into batches of at most eight. Dispatch one `general-purpose` subagent per batch; its prompt names the batch's frame slugs, the base URL, and this file's "Per frame", "Park rule", "Owner" and "Result" sections.
- Run the batches sequentially: dispatch the next batch only after the previous one has returned. One agent drives the browser at a time.
- Return findings in the final message. Do not write a report file to the repo or the scratchpad.

## Per frame
1. Log in as the user the frame needs. Confirm the signed-in user (avatar or welcome banner) before each capture. If it is the wrong user, log out and log in again.
2. Take a fresh browser snapshot before each click or type. Never reuse an element ref from an earlier snapshot.
3. Save screenshots under the browser MCP's allowed output root (the profile's §Paths screenshot root, a `ui-compare/` subfolder). The browser tools refuse paths outside their allowed roots. Copy the file out afterwards if needed.
4. Compare the screenshot with the design image and give one verdict:
   - MATCH: only after the screenshot comparison shows no difference.
   - DEVIATIONS: list each one. Missing element, extra element, layout or position, colour or token, copy.
   - MISSING: no route or no element in the app for what the frame shows.
   - UNVERIFIED: the state the design shows cannot be reached with the seed data, or needs a write that was not approved. Give the reason.
   - MISSING-DESIGN: no design image exists.
5. Do not end a session, start voice input, grant a permission, or confirm a destructive dialog. Do not open a control that writes to the database unless the owner approved it. Otherwise the verdict is UNVERIFIED with that reason.

## Park rule
If the data a frame needs is missing, mark it UNVERIFIED and stop that frame. Do not create the data. Whether to seed is the owner's decision.

## Owner
After the batch's last frame, run one call with the visible design strings of every DEVIATIONS, MISSING and UNVERIFIED gap (the copy of the missing or differing element):
`uv run --no-project ${CLAUDE_PLUGIN_ROOT}/scripts/ui_gap_owner.py "<visible string>" "<visible string>" ...`
- The owner is each hit's story key with its sprint-status value, e.g. `3-4 (ready-for-dev)`. No hit: `no owner`.
- A hit marked `REGRESSION-CANDIDATE` (a `done` story's spec names the string) is reported as a regression candidate with its `file:line`, not as a regression.

## Result
One table per batch: frame (node) | verdict | owner (status) | gap. The final message is the table plus any screenshot paths. The main session triages the table.
