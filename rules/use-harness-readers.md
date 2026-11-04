---
name: use-harness-readers
description: MANDATORY. Read past tool calls and journal commands, and locate prior subagent reports, through transcript_calls.py and journal_grep.py; never grep raw transcripts or read whole journal files.
---
# Use the harness readers for lookups

## Rule
| Need | Use | Not |
|------|-----|-----|
| A call a past agent or session made, or what it got back (a record line, a scoped run, a result text) | `uv run --no-project .workflow/scripts/transcript_calls.py grep --input "<regex>" [--tool T] [--agent-type T] [--since <date, ISO time or commit>] [--until <date, ISO time or commit>]` | `grep` over the tool's raw transcript files (Claude Code: `~/.claude/projects/**/*.jsonl`, `subagents/`) |
| A shell command (`command`) or error snippet (`error`) the journal kept | `uv run --no-project .workflow/scripts/journal_grep.py command "<regex>" [--since YYYY-MM-DD] [--agent-type T]` | reading whole files under `.workflow/state/journal/` |
| A prior subagent's final report: find it, or read it in full | `uv run --no-project .workflow/scripts/transcript_calls.py grep --tool SubagentHandback --agent-type <role> --input "<regex>" --since <date> [--samples 1 --full]` | grepping `*/subagents/*.meta.json` and listing by time |
| The new activity of every unmined session, clustered | `uv run --no-project .workflow/scripts/retro_digest.py` | reading each journal whole |

- Pass `--since` to `transcript_calls.py`: without it every transcript is read. `--since` and
  `--until` each take a date (`YYYY-MM-DD`), an ISO time (`YYYY-MM-DDTHH:MM[:SS]`, UTC without an
  offset) or a commit (its committer time); `--since` is inclusive, `--until` exclusive.
- `transcript_calls.py` reads Claude Code transcripts. Under OpenCode only the journal readers apply.

## Examples
- ❌ `grep -oh 'run_scoped_tests\.py.\{0,400\}--record 7-92' ~/.claude/projects/<dir>/<session>/subagents/*.jsonl`
- ✅ `uv run --no-project .workflow/scripts/transcript_calls.py grep --input "record 7-92" --since 2026-10-15 --agent-type story-impl`
- ❌ Reading `.workflow/state/journal/<session>.jsonl` to find a command
- ✅ `uv run --no-project .workflow/scripts/journal_grep.py command 'pytest .* -k slow' --since 2026-10-11`

## Scope
Every agent with a shell, and the main session.
