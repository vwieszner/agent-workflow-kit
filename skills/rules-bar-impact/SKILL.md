---
name: rules-bar-impact
description: How a finding's bar impact is assessed against the configured quality-bar doc — the question, the line, the bar never a tier, blocking means patched. Preloaded into findings-verifier, findings-evaluator and story-finalize.
user-invocable: false
---

- **A bar-blocking finding is always patched** — never deferred, never dismissed.
- **The source.** The quality-bar doc (`config: paths.quality_bar_doc`) in the code-under-review tree; the dispatch names its absolute path. Read it in full before your first bar-impact line; a line written without reading it is invalid. With no quality bar configured, every non-REFUTED line is `n/a (no quality bar configured)` — the gate is skipped and says so.
- **The question.** With this finding left unfixed, does the product fail a bar or criterion the doc sets? Ground the answer in the code you read, not in the finding's own description.
- **The line.** `bar impact: none` (add ` — <one line why>` wherever your output asks for a reason), or `bar impact: BLOCKING — <bar-id> — <consequence>`, `<bar-id>` the bar or criterion identifier as the doc names it. In a structured report the value goes in `bar_impact` without the `bar impact:` prefix.
- **The bar, never a tier or priority.** A finding carries no tier or priority; those are set on stories by the owner.
