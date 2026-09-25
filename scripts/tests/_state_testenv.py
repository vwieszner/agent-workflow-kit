"""Test isolation for the scripts-state tests: a throwaway repo root with its own
`.workflow/config.toml`, selected through $WORKFLOW_REPO_ROOT, with wfconfig's caches
cleared on entry and exit so no test reads the real project's config or state.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import wfconfig  # noqa: E402


def clear_caches() -> None:
    wfconfig.repo_root.cache_clear()
    wfconfig.load.cache_clear()


class IsolatedRepo:
    """Context manager → Path of a temp repo root whose config is `config_toml`."""

    def __init__(self, config_toml: str = ""):
        self.config_toml = config_toml
        self.root: Path | None = None
        self._old: str | None = None

    def __enter__(self) -> Path:
        self.root = Path(tempfile.mkdtemp(prefix="wf-state-test-")).resolve()
        (self.root / ".workflow").mkdir()
        (self.root / ".workflow" / "config.toml").write_text(self.config_toml, encoding="utf-8")
        self._old = os.environ.get("WORKFLOW_REPO_ROOT")
        os.environ["WORKFLOW_REPO_ROOT"] = str(self.root)
        clear_caches()
        return self.root

    def __exit__(self, *exc) -> None:
        if self._old is None:
            os.environ.pop("WORKFLOW_REPO_ROOT", None)
        else:
            os.environ["WORKFLOW_REPO_ROOT"] = self._old
        clear_caches()
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)


def enter(testcase, config_toml: str = "") -> Path:
    """Enter an IsolatedRepo for one test; exits via addCleanup."""
    env = IsolatedRepo(config_toml)
    root = env.__enter__()
    testcase.addCleanup(env.__exit__, None, None, None)
    return root


def toml_str(s: str) -> str:
    """A TOML literal string (no escape processing) for a path or command."""
    if "'" in s:
        raise ValueError(f"cannot embed a single quote in a TOML literal string: {s!r}")
    return f"'{s}'"
