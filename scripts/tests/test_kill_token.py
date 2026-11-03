"""kill_token.py: the single-use token the process-kill hook issues and kill_owned.py redeems.

Pins the file layout, the single use (the first redeemer of a nonce creates `<nonce>.used` exclusively, so two
concurrent redeemers cannot both pass; a missing file or a failed delete refuses), the expiry, the check of
`--worktree` and `--since` against the issued values, the traversal-proof nonce, the pruning of stale files and the
directory both sides share.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import kill_token  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402

WORKTREE = "D:/work/proj-worktrees/tq-59"
SINCE = "2026-10-16T08:35:57.131Z"
T0 = 1_760_600_000.0
EXPIRED = "the hook token has expired"
UNKNOWN = "the hook token does not exist or was already used"
UNREADABLE = "the hook token file is unreadable"
OTHER_FACTS = "the hook token was issued for another --worktree or --since"


class TokenCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kill-token-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir = self.tmp / "tokens"

    def issue(self, worktree=WORKTREE, since=SINCE, now=T0):
        return kill_token.issue(self.dir, worktree, since, now)

    def redeem(self, nonce, worktree=WORKTREE, since=SINCE, now=T0):
        return kill_token.redeem(self.dir, nonce, worktree, since, now)

    def plant(self, nonce, content, suffix=".json"):
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{nonce}{suffix}"
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path


class IssueTests(TokenCase):
    def test_a_token_is_a_json_file_named_by_a_32_digit_hex_nonce_holding_the_facts_and_an_expiry(self):
        nonce = self.issue()
        self.assertRegex(nonce, r"^[0-9a-f]{32}$")
        record = json.loads((self.dir / f"{nonce}.json").read_text(encoding="utf-8"))
        self.assertEqual(record, {"worktree": WORKTREE, "since": SINCE, "expires": T0 + kill_token.TTL_SECONDS})

    def test_a_watch_call_has_no_since_and_the_token_holds_null(self):
        nonce = self.issue(since=None)
        self.assertIsNone(json.loads((self.dir / f"{nonce}.json").read_text(encoding="utf-8"))["since"])

    def test_every_token_has_its_own_nonce_and_file(self):
        nonces = {self.issue() for _ in range(20)}
        self.assertEqual(len(nonces), 20)
        self.assertEqual(len(list(self.dir.glob("*.json"))), 20)

    def test_the_directory_is_created_on_the_first_issue(self):
        self.assertFalse(self.dir.exists())
        self.issue()
        self.assertTrue(self.dir.is_dir())

    def test_an_existing_token_file_is_never_overwritten(self):
        with mock.patch.object(kill_token.secrets, "token_hex", return_value="0" * 32):
            self.issue()
            with self.assertRaises(FileExistsError):
                self.issue()

    def test_a_file_older_than_the_prune_age_is_removed_at_issue_time_and_a_recent_one_stays(self):
        self.assertGreater(kill_token.PRUNE_SECONDS, kill_token.TTL_SECONDS, "pruning must never remove a live token")
        now = time.time()
        old, recent = self.plant("a" * 32, "{}"), self.plant("b" * 32, "{}")
        old_used, recent_used = self.plant("a" * 32, "", ".used"), self.plant("b" * 32, "", ".used")
        os.utime(old, (now - kill_token.PRUNE_SECONDS - 60, now - kill_token.PRUNE_SECONDS - 60))
        os.utime(old_used, (now - kill_token.PRUNE_SECONDS - 60, now - kill_token.PRUNE_SECONDS - 60))
        os.utime(recent, (now - 10, now - 10))
        os.utime(recent_used, (now - 10, now - 10))
        self.issue(now=now)
        self.assertFalse(old.exists())
        self.assertFalse(old_used.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(recent_used.exists())


class RedeemTests(TokenCase):
    def test_redeeming_with_the_issued_facts_succeeds_and_deletes_the_file(self):
        nonce = self.issue()
        self.assertIsNone(self.redeem(nonce))
        self.assertFalse((self.dir / f"{nonce}.json").exists())

    def test_a_token_is_single_use(self):
        nonce = self.issue()
        self.assertIsNone(self.redeem(nonce))
        self.assertEqual(self.redeem(nonce), UNKNOWN)

    def test_a_token_is_valid_through_its_expiry_and_refused_after_it_and_is_deleted_either_way(self):
        edge = T0 + kill_token.TTL_SECONDS
        at_edge = self.issue()
        self.assertIsNone(self.redeem(at_edge, now=edge))
        self.assertFalse((self.dir / f"{at_edge}.json").exists())
        expired = self.issue()
        self.assertEqual(self.redeem(expired, now=edge + 0.001), EXPIRED)
        self.assertFalse((self.dir / f"{expired}.json").exists())
        self.assertTrue((self.dir / f"{expired}.used").exists())
        self.plant(expired, json.dumps({"worktree": WORKTREE, "since": SINCE, "expires": edge + 1000}))
        self.assertEqual(self.redeem(expired, now=edge + 0.001), UNKNOWN)

    def test_facts_other_than_the_issued_ones_refuse_and_burn_the_token(self):
        for label, worktree, since in (("worktree", "D:/work/proj-worktrees/tq-5", SINCE),
                                       ("since", WORKTREE, "2026-10-16T08:35:58.000Z"),
                                       ("no since", WORKTREE, None),
                                       ("no worktree", None, SINCE)):
            with self.subTest(label=label):
                nonce = self.issue()
                self.assertEqual(self.redeem(nonce, worktree=worktree, since=since), OTHER_FACTS)
                self.assertEqual(self.redeem(nonce), UNKNOWN)

    def test_a_watch_token_redeems_only_without_a_since(self):
        self.assertIsNone(self.redeem(self.issue(since=None), since=None))
        self.assertEqual(self.redeem(self.issue(since=None), since=SINCE), OTHER_FACTS)

    def test_the_worktree_is_compared_as_a_normalized_path(self):
        nonce = self.issue(worktree="/work/proj-worktrees/tq-59/")
        self.assertIsNone(self.redeem(nonce, worktree="/work/proj-worktrees/./tq-59"))

    @unittest.skipUnless(os.name == "nt", "case-insensitive, backslash-separated paths are Windows-only")
    def test_the_worktree_is_compared_as_a_windows_path(self):
        nonce = self.issue(worktree="D:/work/Proj-Worktrees/tq-59/")
        self.assertIsNone(self.redeem(nonce, worktree="d:\\work\\proj-worktrees\\tq-59"))

    def test_a_missing_or_malformed_nonce_is_refused_and_names_no_path_outside_the_directory(self):
        decoy = self.tmp / "decoy.json"
        decoy.write_text(json.dumps({"worktree": WORKTREE, "since": SINCE, "expires": T0 + 100}), encoding="utf-8")
        self.dir.mkdir()
        for nonce in (None, 5, "", "abc", "A" * 32, "0" * 31, "0" * 33, "g" * 32, "../decoy", "..\\decoy",
                      "../decoy" + "0" * 32, str(decoy.with_suffix("")), "0" * 32 + ".json", "0" * 32 + "\n"):
            with self.subTest(nonce=nonce):
                self.assertEqual(self.redeem(nonce), "the hook token is missing or malformed")
        self.assertTrue(decoy.exists())

    def test_a_file_that_is_not_a_valid_record_is_refused_and_deleted(self):
        good = {"worktree": WORKTREE, "since": SINCE, "expires": T0 + 100}
        for number, (label, content) in enumerate((
            ("not json", "{"), ("a list", "[]"), ("a string", '"x"'), ("empty", ""),
            ("no expires", json.dumps({k: v for k, v in good.items() if k != "expires"})),
            ("no worktree", json.dumps({k: v for k, v in good.items() if k != "worktree"})),
            ("no since", json.dumps({k: v for k, v in good.items() if k != "since"})),
            ("expires text", json.dumps({**good, "expires": "soon"})),
            ("expires bool", json.dumps({**good, "expires": True})),
            ("worktree number", json.dumps({**good, "worktree": 5})),
            ("since number", json.dumps({**good, "since": 5})),
            ("not utf-8", b"\xff\xfe"),
            ("nested past the recursion limit", "[" * 100000 + "]" * 100000),
        )):
            with self.subTest(label=label):
                nonce = f"{number:032x}"
                path = self.plant(nonce, content)
                self.assertEqual(self.redeem(nonce), UNREADABLE)
                self.assertFalse(path.exists())

    def test_a_token_whose_file_cannot_be_deleted_is_refused_and_stays_burnt(self):
        nonce = self.issue()
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            self.assertEqual(self.redeem(nonce), UNKNOWN)
        self.assertTrue((self.dir / f"{nonce}.json").exists())
        self.assertEqual(self.redeem(nonce), UNKNOWN)

    def test_a_redeemed_nonce_is_burnt_even_if_a_file_for_it_appears_again(self):
        nonce = self.issue()
        self.assertIsNone(self.redeem(nonce))
        self.assertTrue((self.dir / f"{nonce}.used").exists())
        self.plant(nonce, json.dumps({"worktree": WORKTREE, "since": SINCE, "expires": T0 + 100}))
        self.assertEqual(self.redeem(nonce), UNKNOWN)

    def test_a_nonce_that_was_never_issued_leaves_no_marker(self):
        self.dir.mkdir()
        self.assertEqual(self.redeem("e" * 32), UNKNOWN)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_a_directory_that_does_not_exist_refuses(self):
        self.assertEqual(self.redeem("d" * 32), UNKNOWN)

    def test_two_redeemers_of_one_token_cannot_both_pass(self):
        for _ in range(10):
            nonce = self.issue()
            barrier, results, errors = threading.Barrier(6, timeout=30), [], []

            def redeem():
                try:
                    barrier.wait()
                    results.append(self.redeem(nonce))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=redeem) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(errors, [])
            self.assertEqual([thread for thread in threads if thread.is_alive()], [])
            self.assertEqual(len(results), 6, results)
            self.assertEqual(results.count(None), 1, results)
            self.assertEqual([result for result in results if result is not None], [UNKNOWN] * 5, results)


class DirectoryTests(unittest.TestCase):
    def test_the_directory_is_the_state_dirs_token_folder(self):
        root = te.enter(self, "")
        self.assertEqual(kill_token.token_dir(), root / ".workflow" / "state" / kill_token.TOKEN_DIR_NAME)
        self.assertEqual(kill_token.TOKEN_DIR_NAME, "kill-tokens")


if __name__ == "__main__":
    unittest.main()
