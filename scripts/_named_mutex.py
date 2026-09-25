"""Cross-process named lock shared by the slot-registry and failure-log scripts.

    with NamedMutex(name, timeout_ms):
        ...critical section...

Takes an exclusive OS file lock on `.workflow/state/locks/<name>.lock`:
`fcntl.flock` on POSIX, `msvcrt.locking` on Windows (the OS difference lives in
`_try_lock` / `_unlock` only). Concurrent terminals and agents serialize on it.

- The OS drops the lock when the holding process dies, so a killed owner never
  deadlocks later callers: the next waiter acquires it (same outcome as an
  abandoned OS mutex being handed to the next waiter).
- The lock file's existence means nothing. Never delete it to "unlock".
- Not re-entrant: nesting two `NamedMutex` blocks on the same name in one
  process waits on itself until the timeout.

Raises TimeoutError when the wait budget expires, OSError on a lock-API failure.

Names: any string. A leading `Global\\` or `Local\\` prefix is dropped and every
character outside [A-Za-z0-9._-] becomes `_` (so `Global\\SlotRegistry` and
`SlotRegistry` are the same lock).

Used by: reserve_slot.py, release_slot.py, slot_registry.py (name `slot-registry`),
log_failure.py (its own name). Importable because the scripts' directory is on
sys.path when they run as `uv run --no-project .workflow/scripts/<script>.py`.
"""
from __future__ import annotations

import errno
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfconfig  # noqa: E402

_POLL_S = 0.05
_BUSY_ERRNOS = {errno.EAGAIN, errno.EACCES, getattr(errno, "EDEADLK", -1),
                getattr(errno, "EDEADLOCK", -1)}

if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as e:
            if e.errno in _BUSY_ERRNOS:
                return False
            raise

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno in _BUSY_ERRNOS:
                return False
            raise

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def lock_file_for(name: str, lock_dir: Path | None = None) -> Path:
    base = re.sub(r"^(Global|Local)\\+", "", name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "default"
    d = lock_dir if lock_dir is not None else wfconfig.state_dir("locks")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe}.lock"


class NamedMutex:
    """Context manager: `with NamedMutex(name, timeout_ms):` — raises
    TimeoutError when the wait budget expires, OSError on API failure."""

    def __init__(self, name: str, timeout_ms: int, lock_dir: Path | None = None):
        self.name = name
        self.timeout_ms = timeout_ms
        self._lock_dir = lock_dir
        self._fd: int | None = None
        self._owned = False

    def __enter__(self) -> "NamedMutex":
        path = lock_file_for(self.name, self._lock_dir)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + max(self.timeout_ms, 0) / 1000.0
        try:
            while True:
                if _try_lock(fd):
                    self._fd = fd
                    self._owned = True
                    return self
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Mutex acquire timeout ({self.timeout_ms}ms) on {path}")
                time.sleep(_POLL_S)
        except BaseException:
            os.close(fd)
            raise

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            try:
                if self._owned:
                    _unlock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None
                self._owned = False
