---
name: rules-comment-discipline
description: Comment rule for code-writing story agents — invariant and mechanism only; narrative, dates, finding ids and story tags go to the story history. Preloaded into story-impl and story-finalize.
user-invocable: false
---

Comment what the code cannot state itself: an invariant and what pins it, a non-obvious mechanism, a constraint imposed from outside the file. Do not narrate. This overrides "match existing file style" for comment density — match the content of surrounding comments, not their volume. Before saving a file, delete every comment line that does not change what a reader would DO. Scope: the languages `config: pipeline.comment_rule_suffixes` names (default every source file).

- ✅ "shape is pinned by `tests/test_state_version.py` — a field add without a `STATE_VERSION` bump fails that test"
- ✅ "on a retry the `WHERE` filters out the rows already written, so no duplicate is inserted"
- ✅ documentation a spec AC explicitly orders — e.g. "record the resolved import path in the module docstring". A spec-mandated docstring is always kept, even when it reads as narrative.
- ❌ "added 2026-05-02 during the F-11 audit"
- ❌ "lands in this close-out because the other story is already merged"
- ❌ "patch for finding V12 — reviewer flagged this as a race"
- ❌ story/AC tags — `(Story 7.14a AC1)`, `# AC3: validate the id here`
- ❌ a comment restating what the next line does

Narrative, dates, audit names, finding ids, story/AC tags, and which branch a change landed in go to `config: paths.story_history_dir`/`<key>.md`, the spec's in-flight capture section, or the per-story deferred-work file — never into source. A patch manifest's `notes` column is input to the patch, not text to transcribe as a comment.

**Test files are exempt from the story/AC-tag ban — that ban only.** A test module docstring naming the AC it encodes (`Story 7-51 AC5.1 — …`, `spec AC2`) says what the module is for. Dates, audit names and finding ids stay banned in tests. Never strip such a tag, and never raise one as a finding.
