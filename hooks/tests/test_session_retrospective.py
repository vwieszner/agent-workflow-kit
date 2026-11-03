"""session_retrospective.py: analyst outcome classification, the failed-run marker reset, the
launch log, and the prompt's covered-facts / pending / archived blocks."""
import importlib.util
import json
import os
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hookenv import SESSION, HookTestCase  # noqa: E402

RETRO = SESSION / "session_retrospective.py"


def load():
    sys.path.insert(0, str(SESSION))
    spec = importlib.util.spec_from_file_location("retro_mod", RETRO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.mod = load()

    def test_ok_result(self):
        out = self.mod._outcome(0, json.dumps({"result": "done", "session_id": "x"}), False)
        self.assertTrue(out["ok"])
        self.assertEqual("x", out["analyst_session"])

    def test_is_error_fails(self):
        out = self.mod._outcome(0, json.dumps({"is_error": True, "result": "login expired"}), True)
        self.assertEqual((False, "login expired"), (out["ok"], out["error"]))

    def test_no_json_fails(self):
        self.assertFalse(self.mod._outcome(1, "boom", False)["ok"])

    def test_denials_fail_only_without_proposal_or_no_proposals(self):
        res = {"result": "x", "permission_denials": [{"tool_name": "Write"}]}
        self.assertFalse(self.mod._outcome(0, json.dumps(res), False)["ok"])
        self.assertTrue(self.mod._outcome(0, json.dumps(res), True)["ok"])
        res["result"] = "NO_PROPOSALS"
        out = self.mod._outcome(0, json.dumps(res), False)
        self.assertTrue(out["ok"])
        self.assertIn("1 permission denial(s): Write", out["denials"])


class AnalystRunTests(HookTestCase):
    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def fake_claude(self, body: str) -> None:
        exe = self.bin / "claude"
        exe.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)

    def env(self):
        e = super().env()
        e["PATH"] = str(self.bin) + os.pathsep + e.get("PATH", "")
        return e

    def state(self) -> Path:
        return self.root / ".workflow" / "state"

    def analyst(self, facts=10, prior=2):
        markers = self.state() / "journal" / ".markers"
        markers.mkdir(parents=True, exist_ok=True)
        (markers / "s1.json").write_text(json.dumps({"analyzed_facts": 0, "launched_facts": facts}),
                                         encoding="utf-8")
        p = self.run_hook(RETRO, "", ["--analyst", "--session", "s1", "--run-id", "r1", "--facts",
                                      str(facts), "--prior", str(prior), "--covered", "0",
                                      "--tool", "claude"])
        self.assertEqual(0, p.returncode, p.stderr)
        rec = json.loads((self.state() / "retro-launches.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        marker = json.loads((markers / "s1.json").read_text(encoding="utf-8"))
        return rec, marker

    @unittest.skipIf(os.name == "nt", "fake CLI uses a shebang")
    def test_failed_run_resets_launched_facts(self):
        self.fake_claude('import json; print(json.dumps({"is_error": True, "result": "expired"}))')
        rec, marker = self.analyst()
        self.assertEqual((False, "expired", "claude"), (rec["ok"], rec["error"], rec["tool"]))
        self.assertEqual(2, marker["launched_facts"])

    @unittest.skipIf(os.name == "nt", "fake CLI uses a shebang")
    def test_successful_run_keeps_marker(self):
        self.fake_claude('import json; print(json.dumps({"result": "NO_PROPOSALS", "total_cost_usd": 0.1}))')
        rec, marker = self.analyst()
        self.assertTrue(rec["ok"])
        self.assertEqual(10, marker["launched_facts"])

    def test_missing_cli_is_a_failure(self):
        e = self.env()
        e["PATH"] = str(self.bin)
        self.env = lambda: e  # noqa: E731
        rec, marker = self.analyst()
        self.assertIn("not found on PATH", rec["error"])
        self.assertEqual(2, marker["launched_facts"])


class PromptTests(HookTestCase):
    def test_prompt_blocks(self):
        jdir = self.root / ".workflow" / "state" / "journal"
        jdir.mkdir(parents=True)
        facts = [{"ts": f"2026-01-01T00:00:0{i}", "kind": "command_run", "command": f"c{i}"} for i in range(3)]
        (jdir / "s1.jsonl").write_text(json.dumps({"ts": "x", "facts": facts}) + "\n", encoding="utf-8")
        pdir = self.root / ".workflow" / "state" / "proposals"
        (pdir / "archive").mkdir(parents=True)
        (pdir / "script-foo.md").write_text("---\nkind: script\ntitle: Foo tool\nrun_id: r0\n---\n",
                                            encoding="utf-8")
        (pdir / "archive" / "rule-bar.md").write_text("---\nkind: rule\n---\n", encoding="utf-8")
        out = self.run_hook(RETRO, "", ["--prompt", "--session", "s1"]).stdout.decode("utf-8")
        self.assertIn("Every fact in these journals is new.", out)
        self.assertIn("script-foo.md | script | Foo tool", out)
        self.assertIn("rule-bar.md", out)
        self.assertIn("## Impact", out)
        markers = jdir / ".markers"
        markers.mkdir()
        (markers / "s1.json").write_text('{"analyzed_facts": 2, "launched_facts": 0}', encoding="utf-8")
        out = self.run_hook(RETRO, "", ["--prompt", "--session", "s1"]).stdout.decode("utf-8")
        self.assertIn("The first 2 meaningful facts (ts before 2026-01-01T00:00:02)", out)


if __name__ == "__main__":
    unittest.main()
