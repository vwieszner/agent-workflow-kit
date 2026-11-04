"""Static pins on workflows/review.js and the agents it dispatches:
  * DEFER_CLAUSE occurs, case-insensitively, in skills/rules-defer/SKILL.md;
  * every agentType the workflow dispatches has an agents/<id>.md, and its EFFORT entry equals that agent's
    frontmatter `effort` (workflow spawns pass effort explicitly);
  * every review layer and execution stage, plus findings-evaluator, preloads `rules-defer`;
  * the bar-impact field is named `bar_impact` in the verifier schema and the carried keys;
  * the workflow name matches review_findings_render.WORKFLOW_NAME."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "scripts"))
sys.path.insert(0, str(KIT))
JS = (KIT / "workflows" / "review.js").read_text(encoding="utf-8")


def frontmatter(name: str) -> dict:
    import install  # noqa: E402
    meta, _ = install.parse_agent((KIT / "agents" / f"{name}.md").read_text(encoding="utf-8"))
    return meta


class ReviewWorkflowPins(unittest.TestCase):
    def test_defer_clause_is_in_the_skill(self):
        clause = re.search(r"const DEFER_CLAUSE = '([^']+)'", JS).group(1)
        self.assertIn(clause.lower(), (KIT / "skills" / "rules-defer" / "SKILL.md").read_text(encoding="utf-8").lower())

    def test_effort_map_matches_agent_frontmatter(self):
        block = re.search(r"const EFFORT = \{(.*?)\n\}", JS, re.S).group(1)
        effort = dict(re.findall(r"'([\w-]+)': '(\w+)'", block))
        dispatched = set(re.findall(r"agentType: '([\w-]+)'", JS)) | set(re.findall(r"id: '([\w-]+)'", JS))
        self.assertTrue(dispatched <= set(effort), dispatched - set(effort))
        for name, level in effort.items():
            self.assertEqual(level, str(frontmatter(name).get("effort")), name)

    def test_layers_stages_and_evaluator_preload_rules_defer(self):
        ids = set(re.findall(r"(?m)^    id: '([\w-]+)'", JS)) | {"findings-evaluator"}
        for name in sorted(ids):
            skills = (frontmatter(name).get("claude") or {}).get("skills") or []
            self.assertIn("rules-defer", skills, name)
            self.assertIn("rules-defer", (KIT / "agents" / f"{name}.md").read_text(encoding="utf-8"), name)

    def test_bar_impact_naming(self):
        self.assertNotIn("recording", JS.lower())
        self.assertIn("required: ['id', 'verdict', 'evidence', 'bar_impact']", JS)

    def test_workflow_name(self):
        import review_findings_render
        self.assertIn(f"name: '{review_findings_render.WORKFLOW_NAME}'", JS)


if __name__ == "__main__":
    unittest.main()
