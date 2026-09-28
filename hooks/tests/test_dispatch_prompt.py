"""Corpus tests for guards/dispatch_prompt.py."""
from __future__ import annotations

from _hookenv import GUARDS, HookTestCase

HOOK = GUARDS / "dispatch_prompt.py"


def agent(prompt: str, subagent_type: str | None = None) -> dict:
    ti = {"prompt": prompt}
    if subagent_type:
        ti["subagent_type"] = subagent_type
    return {"tool_name": "Agent", "tool_input": ti}


CASES = [
    ("iterate-fix-test", 2, agent("After each patch, run the relevant tests.")),
    ("confirm green", 2, agent("Fix it and confirm green before moving on.")),
    ("review missing no-auto-apply", 2, agent("Run the layered-review skill on this branch and save findings.")),
    ("review with no-auto-apply", 0, agent("Run the layered-review skill. DO NOT auto-apply patches. Report findings only.")),
    ("review with the configured preamble", 0,
     agent("Report findings only — do NOT auto-apply patches.\nReview the diff of story/x.")),
    ("batch-fix-validate-once", 0, agent("Apply all fixes in one pass, then validate once at the end.")),
    ("negation: do NOT iterate", 0, agent("Do NOT iterate fix-test-fix-test. Apply all patches in one pass.")),
    ("negation: never test between fixes", 0, agent("Never run tests between individual fixes within a round.")),
    ("triage application referencing review", 0,
     agent("Apply owner-approved patches for findings F1.1, F3.1. This is NOT a code review dispatch.")),
    ("status sweep referencing review history", 0,
     agent("Read-only status check: does the story file have a Review Findings section? Report what you find.")),
    ("typed story-impl saying do NOT run code review", 0,
     agent("Implement story 7-40. Do NOT run code review and do NOT merge.", "story-impl")),
    ("unrelated 'do not' before a real trigger still blocks", 2,
     agent("Do not edit files. Run a code review of the diff and report what you find.")),
    ("layer name only, untyped", 2, agent("You are the Blind Hunter. Examine the committed diff.")),
    ("review-typed agent without trigger words", 2,
     agent("Classify each item in the union list as patch, defer, or dismiss.", "findings-evaluator")),
    ("typed blind-hunter with preamble", 0,
     agent("Report findings only — do NOT auto-apply patches. Diff follows.", "blind-hunter")),
    ("negated mention and a second un-negated one", 2,
     agent("Do not run code review at the end. Instead, review the diff now and list findings.")),
    ("purely negated review mention", 0,
     agent("Implement the ACs. Never run a code review yourself; the orchestrator owns that step.")),
    ("general-purpose review dispatch without preamble", 2,
     agent("Run a code review of the branch diff and fix what you find.", "general-purpose")),
    ("general-purpose non-review dispatch", 0, agent("Summarise the README.", "general-purpose")),
    ("non-Agent tool ignored", 0, {"tool_name": "Bash", "tool_input": {"prompt": "After each patch run tests"}}),
]


class DispatchPromptCorpus(HookTestCase):
    def test_cases(self):
        for name, expect, payload in CASES:
            with self.subTest(case=name):
                self.assertEqual(expect, self.exit_code(HOOK, payload))

    def test_review_agent_list_from_config(self):
        self.write_config('[guards]\nreview_agents = ["my-reviewer"]\nnon_review_agents = []\n')
        self.assertEqual(2, self.exit_code(HOOK, agent("Look at this.", "my-reviewer")))
        self.assertEqual(0, self.exit_code(HOOK, agent("Look at this.", "blind-hunter")))

    def test_switch_off(self):
        self.write_config("[guards]\ndispatch_prompt = false\n")
        self.assertEqual(0, self.exit_code(HOOK, agent("After each patch, run the tests.")))
