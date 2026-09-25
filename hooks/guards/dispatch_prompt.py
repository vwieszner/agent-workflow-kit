#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""PreToolUse guard on subagent dispatch (Claude `Agent`; OpenCode `task`, mapped to
`Agent` by the plugin). Switch: `config: guards.dispatch_prompt`.

Rules (exit 2 + stderr):
  1. Iterate-fix-test is forbidden in a dispatch prompt: "after each ... test",
     "between/per fix ... run", "confirm green before moving on", "run ... after each
     fix", "iterate ... test". A negation cue ("do not", "never", "no iter...")
     neutralizes the 80 chars after it.
  2. A code-review dispatch must forbid auto-applying patches. Classification by
     `tool_input.subagent_type` first:
       * in `config: guards.non_review_agents` -> never a review dispatch;
       * in `config: guards.review_agents`     -> always a review dispatch;
       * otherwise prose detection: an UN-negated review trigger phrase (each match is
         judged on its own immediate left context, so one negated mention does not excuse
         a second, un-negated one).
     A review dispatch passes only if the prompt contains `config: review.required_preamble`
     or one of: "do not (auto-)apply", "no auto-apply", "report findings only",
     "recommend (a) classification", "never (auto-)apply".
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guardlib as g  # noqa: E402

HOOK = "dispatch_prompt.py"

_NEGATION_BEFORE = re.compile(
    r"(?i)\b(do\s*not|do\s?n't|don't|never|without|avoid|refrain\s+from|"
    r"must\s+not|shall\s+not|no\s+need\s+to)\b[\s,:;\-]*$"
)

ITER_PATTERNS = [
    (r"after\s+each\s+[^.]{0,40}(test|run|verify|patch|fix|change)",
     "'after each ... test|run|verify|patch|fix|change'"),
    (r"(between|per)\s+(fix|patch|change|test)[^.]{0,40}(test|run|verify)",
     "'between/per fix|patch|change ... test|run|verify'"),
    (r"confirm\s+green\s+before\s+moving\s+on", "'confirm green before moving on'"),
    (r"(re-run|run|test)[^.]{0,40}(after|between)[^.]{0,20}(each|every)[^.]{0,20}(fix|patch|change)",
     "'run/test after|between each fix|patch|change'"),
    (r"\biterate[^.]{0,40}(test|run|verify|fix)", "'iterate ... test|run|verify|fix'"),
]

CODE_REVIEW_TRIGGERS = [
    r"(run|invoke|dispatch|use|perform|conduct|execute)\s+(the\s+)?(layered-review|code-review)\b",
    r"produce\s+(a\s+|the\s+)?(findings\s+list|findings)",
    r"(run|conduct|perform|execute)\s+(a\s+|the\s+)?code\s+review",
    r"review\s+the\s+(branch\s+)?(diff|branch\s+code)",
    r"blind[\s-]+hunter",
    r"edge[-\s]?case[\s-]+hunter",
    r"acceptance[\s-]+auditor",
    r"adversarial(ly)?\s+review",
    r"review\s+(this|the)\s+(code|diff|changes)\b",
]

NO_AUTO_APPLY = [
    r"do\s+not\s+(auto-?apply|apply)",
    r"no\s+auto-?apply",
    r"report\s+findings\s+only",
    r"recommend\s+a?\s*classification",
    r"never\s+(auto-?apply|apply)",
]

DEFAULT_NON_REVIEW = ["story-spec", "story-impl", "story-finalize", "test-bat-runner",
                      "dev-preflight", "trace-port-traffic", "general-purpose", "Explore", "Plan"]
DEFAULT_REVIEW = ["blind-hunter", "edge-case-hunter", "acceptance-auditor",
                  "findings-verifier", "findings-evaluator"]


def has_unnegated_match(text: str, pattern: str) -> bool:
    for m in re.finditer(pattern, text, re.IGNORECASE):
        if _NEGATION_BEFORE.search(text[max(0, m.start() - 28):m.start()]):
            continue
        return True
    return False


def evaluate(payload: dict) -> int:
    if payload.get("tool_name") != "Agent":
        return 0
    ti = payload.get("tool_input") or {}
    prompt = ti.get("prompt", "")
    if not isinstance(prompt, str) or not prompt:
        return 0
    subagent_type = str(ti.get("subagent_type") or "").strip()

    violations: list[str] = []
    neg_stripped = re.sub(
        r"(?i)\b(do\s*not|don'?t|never|no\s+(?:iter|per-fix|between))\b.{0,80}", " ", prompt,
    )
    for pattern, label in ITER_PATTERNS:
        if re.search(pattern, neg_stripped, re.IGNORECASE):
            violations.append(f"[iterate-fix-test] {label} — batch-fix all failures, then "
                              f"validate once per round.")

    non_review = set(g.cfg_list("guards.non_review_agents", DEFAULT_NON_REVIEW))
    review = set(g.cfg_list("guards.review_agents", DEFAULT_REVIEW))
    if subagent_type in non_review:
        is_review = False
    elif subagent_type in review:
        is_review = True
    else:
        is_review = any(has_unnegated_match(prompt, p) for p in CODE_REVIEW_TRIGGERS)

    if is_review:
        preamble = str(g.cfg("review.required_preamble", "") or "").strip()
        ok = bool(preamble) and preamble in prompt
        ok = ok or any(re.search(p, prompt, re.IGNORECASE) for p in NO_AUTO_APPLY)
        if not ok:
            hint = f" Include the required preamble: \"{preamble}\"" if preamble else ""
            violations.append(
                "[code-review] missing explicit 'do not auto-apply' / 'report findings only' / "
                "'recommend classification' instruction. Review subagents must never apply "
                "patches — the owner triages each finding." + hint
            )

    if violations:
        return g.block("Subagent dispatch prompt violates one or more discipline rules.",
                       violations, HOOK, "Rewrite the prompt and try again.")
    return 0


if __name__ == "__main__":
    g.run("dispatch_prompt", evaluate)
