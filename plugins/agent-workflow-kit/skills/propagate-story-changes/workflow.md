# Propagate Story Changes

Inside a parallel-dev-wave story this runs automatically (the Propagation section before Checkpoint 3; land-story's Step 4b applies). Run this skill only for a story outside that flow, or to redo a pass.

## Steps

1. **Resolve inputs.** `story=<id>` if given, else the slot registry row of the current worktree. Spec: the story's `<config: paths.specs_dir>/<id>-*.md` in its worktree (not `-record`, not a findings file). Ask the owner if either is ambiguous.
2. **Propose.** Dispatch the `story-propagate` subagent with `mode: propose`, `story_id`, `worktree_path`, `spec_path`, `base: <config: git.base_branch>`, `docs_root: <the main checkout>`. Write its checklist verbatim to `.workflow/state/story-setup/propagation-<id>.md` while the story has a worktree; for a story already on the base branch, to `<config: paths.story_history_dir>/<id>/propagation-<id>.md`, where land-story's cleanup leaves a landed story's checklist.
3. **Walk it with the owner.** Present every row, cascade and memory candidate; the owner decides each (update as proposed or reworded / follow-up / skip; cascades: revise now / follow-up story / sprint replan). Never decide a row yourself. Write the rulings under `## Decisions` in the file. An undecided cascade means the story does not merge. Save the memory candidates the owner keeps.
4. **Apply.** After the story merges (or now, if the story is already on the base branch), dispatch `story-propagate` with `mode: apply` exactly as land-story's Step 4b describes, and commit the `- updated:` paths by explicit path (`${CLAUDE_PLUGIN_ROOT}/scripts/stage_own_hunks.py` for files other sessions may also edit), with the checklist when it is the story-history copy.
