---
diff_output: '' # set at runtime
spec_file: '' # set at runtime (path or empty)
review_mode: '' # set at runtime: "full" or "no-spec"
story_key: '' # set at runtime when discovered from sprint status or passed by the pipeline
graph_project: '' # set at runtime (pipeline args) or empty
snapshot_mode: '' # set at runtime: "true" when the worktree is being mutated concurrently
---

# Step 1: Gather Context

## RULES

- The prompt that triggered this workflow IS the intent — not a hint.
- Do not modify any files. This step is read-only.
- Pipeline mode: take the diff range, spec path, story key and graph project from the args; skip every question below and do not HALT at the checkpoint.

## INSTRUCTIONS

1. **Detect review intent from the invocation text.** Map phrases to a source:
   - "staged" / "staged changes" → staged changes only
   - "uncommitted" / "working tree" / "all changes" → uncommitted changes (staged + unstaged)
   - "branch diff" / "vs <branch>" / "against <branch>" / "compared to <branch>" → branch diff (extract the base branch; default `config: git.base_branch`)
   - "commit range" / "last N commits" / "<sha>..<sha>" → specific commit range
   - "this diff" / "provided diff" / "paste" → owner-provided diff (do not match bare "diff" — it appears in other modes)
   - When several phrases match, prefer the most specific ("branch diff" over bare "diff").
   - **Clear match:** announce the detected source (e.g. "Detected intent: review staged changes only"), construct `{diff_output}` per instruction 3, then go to instruction 4.
   - **No match — check sprint tracking.** If `config: paths.sprint_status` exists, scan it for stories with status `review`:
     - **Exactly one:** set `{story_key}` to its key and suggest it: "I found story <key> in `review` status. Review its changes? [Y] Yes / [N] No, let me choose". If confirmed, derive the diff source from the story (its branch `config: git.branch_prefix` + key vs `config: git.base_branch`, or uncommitted changes). If declined, clear `{story_key}` and go to instruction 2.
     - **Several:** present them as numbered options plus a manual-choice option and wait. On a story selection set `{story_key}` and proceed as above to instruction 3; on manual choice clear `{story_key}` and go to instruction 2.
   - **No match and no sprint tracking:** go to instruction 2.

2. HALT. Ask the owner **What do you want to review?** with these options:
   - Uncommitted changes (staged + unstaged)
   - Staged changes only
   - Branch diff vs a base branch (ask which base branch)
   - Specific commit range (ask for the range)
   - Provided diff or file list (owner pastes or gives a path)

3. Construct `{diff_output}` from the chosen source.
   - **Branch diff:** verify the base branch exists before `git diff`. If not, HALT and ask for a valid branch.
   - **Commit range:** verify the range resolves. If not, HALT and ask for a valid range.
   - **Provided diff:** validate it is non-empty and parseable as a unified diff. If not, HALT and ask for a valid diff.
   - **File list:** validate each path exists. Build `{diff_output}` with `git diff HEAD -- <path1> <path2> ...`; for untracked paths use `git diff --no-index /dev/null <path>`. If the result is empty, ask whether to review full file contents or a different baseline.
   - Whatever the source, verify `{diff_output}` is non-empty. If empty, HALT and report there is nothing to review.

4. Ask the owner: **Is there a spec or story file that provides context for these changes?**
   - Yes: set `{spec_file}` to the path, verify it exists and is readable, set `{review_mode}` = `"full"`.
   - No: set `{review_mode}` = `"no-spec"`.

5. If `{review_mode}` = `"full"` and `{spec_file}`'s frontmatter has a `context` field listing additional docs, load each one. Warn about any doc that cannot be found.

6. **Graph project.** If `config: graph.tool` is `"none"`, set `{graph_project}` empty and state "graph navigation disabled (graph.tool = none)". Otherwise use the project named by the caller (a story slot's `graph_project`). Never use `config: graph.main_project` when reviewing a branch worktree — it indexes the main checkout, not the branch. If no project is known, leave it empty (layers fall back to grep/read).

7. **Snapshot mode.** If another agent is concurrently mutating the worktree under review (e.g. `story-finalize` applying patches), set `{snapshot_mode}` = `"true"`: materialize each file under review with `git show <commit>:<path>` (plus the spec at that commit) into the scratch/state dir, and set `{graph_project}` empty — the graph index reflects the mutating tree. The layers have no shell, so the orchestrator does this materialization.

8. Sanity check: if `{diff_output}` exceeds roughly 3000 lines, warn the owner and offer to chunk the review by file group.
   - Chunk: agree on the first group, narrow `{diff_output}`, and list the remaining groups for follow-up runs.
   - Decline: proceed with the full diff.

### CHECKPOINT

Present a summary: diff stats (files changed, lines added/removed), `{review_mode}`, loaded spec/context docs, `{graph_project}` (or "none"), `{snapshot_mode}`. Direct mode: HALT and wait for the owner's confirmation. Pipeline mode: print it and continue.

## NEXT

Read fully and follow `./step-02-review.md`
