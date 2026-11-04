# Rules Index

Detailed rules referenced from `AGENTS.md`. Installed at `.workflow/rules/`. Project-specific values
come from `.workflow/config.toml` (`config: <section>.<key>`). A rule marked *conditional* is active
only when its config switch is on; when off, it does not apply and nothing replaces it.

## Always active — working method
| Rule | File | Summary |
|---|---|---|
| Answer Before Action | `answer-before-action.md` | A question from the owner is answered directly before any action or code. |
| Goal and Reasoning | `goal-and-reasoning.md` | State goal + evidence (+ what was rejected) before any consequential action and before any recommendation or verdict. |
| No Quality Reduction | `no-quality-reduction.md` | Scope is set by the requirement, never by convenience. A reduction is a descope proposal the owner decides. |
| Root Cause Before Fix | `root-cause-before-fix.md` | Prove the mechanism before fixing. No unproved fixes, test-weakening, silent deferral, or symptom suppression. Labelled mitigations only. |
| Read Full Docs | `read-full-docs.md` | Read a whole doc/spec/planning file before any conclusion about it; targeted search only to locate. |
| Verify Ground Truth | `verify-ground-truth.md` | Check the exit code, not the summary line. A subagent report is a claim; verify before relaying. |
| Off-Task Work | `off-task-work.md` | Off-task work is never reverted: move your own hunks to a new branch (`stage_own_hunks.py pick` → apply there → reverse here), carve a story, tell the owner, resume the task. Discarding takes the owner's explicit discard. |
| Use Harness Readers | `use-harness-readers.md` | Past calls, prior subagent reports and journal commands/errors are looked up with `transcript_calls.py` / `journal_grep.py` / `retro_digest.py`, never raw transcript greps or whole journal reads. |

## Always active — testing
| Rule | File | Summary |
|---|---|---|
| Test Guide | `test-guide.md` | Scoped vs full-gate entry points, required flags, sequential runs, code freeze, batch-fix iteration, failure analysis. |
| Real-Signature Tests | `real-signature-tests.md` | Infra-boundary code needs a live-dependency test or a contract-faithful fake. Bare auto-mocks for that boundary are banned; no real LLM in tests. |
| Parallel Test Isolation | `parallel-test-isolation.md` | Deferred connections, no module-level connection singletons, per-test tokens on shared live infra, per-worker E2E shards; one joint worker budget across legs; per-test timeout criterion; when the full gate is required pre-merge. |
| E2E Debugging Protocol | `e2e-debugging-protocol.md` | Lifecycle mandate, reproduce first, tri-log search, visual evidence, strike-limit stop, test-id-only locators. |
| Browser Interaction | `browser-interaction.md` | Inspect → clear → verify-empty before typing; use `config: ui.base_url`; never guess credentials. |

## Always active — process
| Rule | File | Summary |
|---|---|---|
| Story Checkbox Enforcement | `story-checkbox-enforcement.md` | Mark completed tasks `[x]` + update the spec record before committing. |
| Dependency Sync | `dependency-sync.md` | Update manifest + lockfile via the ecosystem tool, rebuild images, commit both together. |
| Script & Tooling Artifact Curation | `script-artifact-curation.md` | Reusable scripts get a doc header + registry row; recurring work becomes propose-and-approve artifacts via `retro` → `curate`. Nothing auto-writes into curated dirs. |

## Conditional
| Rule | File | Active when |
|---|---|---|
| Host-Passive Environment | `host-passive-environment.md` | `config: env.app_runs_in_container = true` — app-side commands only in the container; host tooling paths; forbidden installs; Windows path note. |
| Live Sync | `live-sync.md` | `config: stack.runtime != "none"` and `config: stack.watch = true` — `compose watch` only, no source volume mounts; verify sync; per-stack code freeze. |
| Container Truthfulness | `container-truthfulness.md` | `config: stack.runtime != "none"` — orphaned in-container processes (`/proc`, not `ps`), half-up stacks (`ps -a`, `start` not `up`). |
| Pre-Production Schema | `preprod-schema.md` | `config: schema.preprod = true` — no new migrations, no back-compat tests, reset stale test/live DBs. |

## Skill-form rules
Preloaded into agents through their `skills:` frontmatter (Claude Code); elsewhere the agent invokes
them with the skill tool first.
| Skill | Scope |
|---|---|
| `rules-core` | Code rules a diff is judged against (project rules + scope/quality). |
| `rules-defer` | When a finding is patched, deferred or dismissed; loop exit, round cap, new-code round. |
| `rules-bar-impact` | A finding's bar-impact line against `config: paths.quality_bar_doc`. |
| `rules-graph-nav` | The graph for structure, Read before editing or judging, the slot's graph project only. |
| `rules-comment-discipline` | Code comments carry invariant and mechanism only. |
| `rules-test-runs` | How story agents run tests (one scoped invocation per round, recorded green). |
| `rules-testing` | Test-writing rules for agents that write tests. |
| `ui-seed` | A MISSING UI state becomes a seed AC or a question; seed conventions (`config: ui.seed_*`). |

## Use
- `AGENTS.md` carries the short form of every rule; the file here is the full rule. When a rule blocks
  an action, cite its file.
- Rule files hold operative content only (see `AGENTS.md`).
