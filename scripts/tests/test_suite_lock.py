"""Cross-process exclusion tests for the gate lock in run_full_suite.py.

Three properties, all of which fail SILENTLY if broken:

1. The lock excludes a second OS process (a thread-only lock would let two sessions
   run the gate while both believe they are serialized).
2. A queued session says so, repeatedly (a silent hour-long wait reads as a hang).
3. Budget exhaustion ABORTS — never falls through to running the suite unlocked.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _state_testenv as te  # noqa: E402
import run_full_suite  # noqa: E402

# A second OS PROCESS is the only faithful holder.
_HOLDER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from _named_mutex import NamedMutex\n"
    "lock_dir, name, ready_path, hold_s = Path(sys.argv[2]), sys.argv[3], sys.argv[4], float(sys.argv[5])\n"
    "with NamedMutex(name, 60000, lock_dir=lock_dir):\n"
    "    open(ready_path, 'w').close()\n"
    "    time.sleep(hold_s)\n"
)


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


class SuiteLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = te.enter(self, "[stack]\nruntime = \"none\"\n[tests]\nfull_cmd = \"echo unused\"\n")
        self._tmp = tempfile.TemporaryDirectory(prefix="suite_lock_", ignore_cleanup_errors=True)
        tmp = Path(self._tmp.name)
        self.lock_dir = tmp / "locks"
        self.ready_path = tmp / "holder-ready"
        # A throwaway lock dir: the REAL gate lock may be held by a gate running these tests.
        self._real = run_full_suite.LOCK_DIR
        run_full_suite.LOCK_DIR = self.lock_dir
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        run_full_suite.LOCK_DIR = self._real
        self._tmp.cleanup()

    def _start_holder(self, hold_seconds: float) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(te.SCRIPTS), str(self.lock_dir),
             run_full_suite.LOCK_NAME, str(self.ready_path), str(hold_seconds)])
        # Registered after _restore so it runs BEFORE it (cleanups are LIFO).
        self.addCleanup(_reap, proc)
        deadline = time.monotonic() + 30
        while not self.ready_path.exists():
            if proc.poll() is not None:
                raise AssertionError(f"holder exited before taking the lock (exit {proc.returncode})")
            if time.monotonic() > deadline:
                raise AssertionError("holder never took the lock in 30s")
            time.sleep(0.02)
        return proc

    def test_a_second_process_cannot_hold_the_lock_at_the_same_time(self) -> None:
        self._start_holder(hold_seconds=30)
        with self.assertRaises(run_full_suite.LockTimeout,
                               msg="the gate lock did not exclude a second process"):
            with redirect_stdout(io.StringIO()):
                run_full_suite._acquire_suite_lock(budget_minutes=1.5 / 60, heartbeat_seconds=0.2)

    def test_a_queued_run_announces_the_wait_and_then_proceeds(self) -> None:
        self._start_holder(hold_seconds=1.5)
        out = io.StringIO()
        with redirect_stdout(out):
            lock = run_full_suite._acquire_suite_lock(budget_minutes=1.0, heartbeat_seconds=0.25)
        self.addCleanup(lock.release)
        text = out.getvalue()
        self.assertIn("queueing", text, f"a queued run never said it was queued:\n{text}")
        self.assertIn("still waiting on the gate lock", text,
                      f"a queued run printed no heartbeat:\n{text}")
        self.assertIn("gate lock acquired", text, f"the run never reported taking the lock:\n{text}")

    def test_a_lock_it_cannot_take_aborts_the_run(self) -> None:
        self._start_holder(hold_seconds=30)
        ran: list[int] = []
        real = run_full_suite._run_gate
        self.addCleanup(setattr, run_full_suite, "_run_gate", real)
        run_full_suite._run_gate = lambda *_a, **_k: (ran.append(1), 0)[1]
        with redirect_stdout(io.StringIO()):
            code = run_full_suite.main(["--lock-timeout-minutes", "0"])
        self.assertEqual(code, 1, "a gate lock that could not be taken did not fail the run")
        self.assertEqual(ran, [], "the suite RAN without holding the gate lock")

    def test_release_lets_the_next_caller_in(self) -> None:
        with redirect_stdout(io.StringIO()):
            first = run_full_suite._acquire_suite_lock(budget_minutes=0.01, heartbeat_seconds=0.1)
            first.release()
            second = run_full_suite._acquire_suite_lock(budget_minutes=0.01, heartbeat_seconds=0.1)
        second.release()


if __name__ == "__main__":
    unittest.main()
