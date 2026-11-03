---
name: rules-core
description: The code rules a diff is judged against — the project's own rules plus scope, symptom suppression, shipped functionality, over-engineering and mirrored values. Preloaded into review agents that run without the project instructions.
user-invocable: false
---

A change that breaks one of these rules is a finding. Before judging a finding that rests on a rule, read the full rule in the code-under-review tree.

**The project's rules** (read them in the code-under-review tree before the first finding that rests on one): `.workflow/AGENTS.local.md` and `.workflow/rules/local/` (the project's own code rules: layer boundaries, schema, parsing, framework conventions, styling, auth), `.workflow/AGENTS.md` and `.workflow/rules/` (the kit's working rules). A rule from these files that a changed line breaks is a finding citing the file and rule.

**Schema and compatibility** (when `config: schema.preprod` is true): no new migration files — a model change edits the initial migration; no backward-compatibility code or tests (migration-preservation tests, deprecation warnings, env-var aliases, dual-read fallbacks, `_v2` paths).

**Scope and quality**
- Every changed line traces to the story's spec; an unrelated edit is a finding.
- No symptom suppression: a swallowed exception, sleep, bumped timeout or added retry that makes a failure go away (`.workflow/rules/root-cause-before-fix.md`).
- Hand-rolling what an installed dependency already ships is a defect.
- Over-engineering smells are findings: an abstract base or factory with one implementation, a flag or env toggle with no A/B need, a helper for one caller, a retry or timeout decorator on a local call that has never failed, a defensive check for a case the types or call boundary already rule out. Tests and real security or tenant-isolation boundaries are exempt.
- No module-level connection work: no client or singleton holding a live connection created at import time (`.workflow/rules/parallel-test-isolation.md`).
- Structured output from an LLM is parsed with a schema-validated parser, never with regex or string matching.
- A value that mirrors a definition elsewhere (constant, enum, range, field list) must match that definition; read it before judging.
