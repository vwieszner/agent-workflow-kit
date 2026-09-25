"""Tests for guards/phase_dispatch.py: routing with a stubbed record, plus the real
subprocess path against a fake story_record.py (fail-closed when the checker is absent)."""
from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _hookenv import GUARDS, HookTestCase

HOOK = GUARDS / "phase_dispatch.py"


def load_module():
    spec = importlib.util.spec_from_file_location("phase_dispatch_mod", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PhaseDispatchRouting(HookTestCase):
    def test_selftest_passes(self):
        p = self.run_hook(HOOK, "", ["--selftest"])
        self.assertEqual(0, p.returncode, p.stdout.decode(errors="replace"))

    def test_extract_story_id(self):
        mod = load_module()
        self.assertEqual("7-21", mod.extract_story_id("story_id: 7-21\nworktree_path: x"))
        self.assertEqual("8-3", mod.extract_story_id("branch_name: story/8-3"))
        self.assertEqual("9-1", mod.extract_story_id("work on story/9-1 now"))
        self.assertIsNone(mod.extract_story_id("no id here"))

    def test_opencode_skill_name_field(self):
        mod = load_module()
        mod.fact_recorded = lambda s, k: False
        buf = io.StringIO()
        with redirect_stderr(buf), redirect_stdout(io.StringIO()):
            got = mod.evaluate({"tool_name": "Skill", "tool_input": {"name": "land-story", "args": "st-x"}})
        self.assertEqual(2, got)


class PhaseDispatchRecordCheck(HookTestCase):
    """Real subprocess path. The kit's story_record.py may not exist when this runs, so the
    checker's absence must keep the gate CLOSED."""

    def test_gated_dispatch_blocked_when_fact_unrecorded(self):
        payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "story-impl",
                                                        "prompt": "story_id: zz-nonexistent-1"}}
        self.assertEqual(2, self.exit_code(HOOK, payload))

    def test_story_impl_without_id_blocked(self):
        payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "story-impl", "prompt": "go"}}
        self.assertEqual(2, self.exit_code(HOOK, payload))

    def test_ungated_passes(self):
        payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore", "prompt": "x"}}
        self.assertEqual(0, self.exit_code(HOOK, payload))

    def test_switch_off(self):
        self.write_config("[guards]\nphase_dispatch = false\n")
        payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "story-impl", "prompt": "go"}}
        self.assertEqual(0, self.exit_code(HOOK, payload))
