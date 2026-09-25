"""Test helpers: run a hook exactly as the harness does (JSON on stdin, fresh process)
against a throwaway repo root whose .workflow/config.toml the test controls."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent
GUARDS = HOOKS / "guards"
SESSION = HOOKS / "session"

BASE_CONFIG = """
[project]
name = "demo"
owner = "the owner"

[git]
base_branch = "development"
main_branch = "main"
branch_prefix = "story/"
worktrees_root = "../demo-worktrees"

[paths]
slot_registry = "docs/slot-registry.md"
scripts_index = "scripts/INDEX.md"

[slots]
count = 6

[graph]
tool = "none"
mcp_server = "codebase-memory-mcp"
main_project = ""

[review]
required_preamble = "Report findings only — do NOT auto-apply patches."
"""


class HookTestCase(unittest.TestCase):
    extra_config = ""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wfhook-"))
        (self.root / ".workflow").mkdir()
        self.write_config(self.extra_config)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write_config(self, extra: str) -> None:
        (self.root / ".workflow" / "config.toml").write_text(BASE_CONFIG + "\n" + extra,
                                                              encoding="utf-8")

    def env(self) -> dict:
        e = dict(os.environ)
        e["WORKFLOW_REPO_ROOT"] = str(self.root)
        e.pop("WORKFLOW_HEADLESS", None)
        return e

    def run_hook(self, script: Path, payload, args: list[str] | None = None):
        data = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, str(script), *(args or [])],
            input=data.encode("utf-8"), capture_output=True, env=self.env(), cwd=str(self.root),
        )

    def exit_code(self, script: Path, payload) -> int:
        return self.run_hook(script, payload).returncode

    def json_denied(self, script: Path, payload) -> bool:
        p = self.run_hook(script, payload)
        self.assertEqual(0, p.returncode, p.stderr.decode(errors="replace"))
        return b'"deny"' in p.stdout
