#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""The single-use token that lets `kill_owned.py` run: the process-kill guard issues it, the helper redeems it.

The guard (`.workflow/hooks/guards/process_kill.py`) rewrites a sanctioned `kill_owned.py` call to carry
`--hook-token <nonce>` and writes `.workflow/state/kill-tokens/<nonce>.json` holding the `worktree` and `since`
the call will carry and an expiry. `kill_owned.py` redeems the file before it reads any process: the first
redeemer of a nonce creates `<nonce>.used` (exclusive creation, so exactly one redeemer can, concurrent or not),
reads the file and deletes it (a missing file or a failed delete refuses), then refuses unless the file is
unexpired and its `worktree` and `since` equal the command-line values. A call that reaches the helper by any
route without a token the guard issued for exactly those values is refused.

This file is the one definition of the directory, the file layout and the redeem rules; both sides import it.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wfconfig  # noqa: E402

TOKEN_DIR_NAME = "kill-tokens"
TTL_SECONDS = 120
PRUNE_SECONDS = 3600
NONCE = re.compile(r"[0-9a-f]{32}")


def token_dir() -> Path:
    """`.workflow/state/kill-tokens` of the main checkout — the same directory for the guard and the helper
    wherever each runs from (wfconfig resolves a linked worktree to its main checkout)."""
    return wfconfig.state_dir(TOKEN_DIR_NAME)


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def _prune(directory: Path, now: float) -> None:
    for old in (*directory.glob("*.json"), *directory.glob("*.used")):
        try:
            if old.stat().st_mtime < now - PRUNE_SECONDS:
                old.unlink()
        except OSError:
            continue


def issue(directory: Path, worktree: str, since: str | None, now: float | None = None) -> str:
    """Write a token for `worktree` and `since` and return its nonce. Token and `.used` files older than
    PRUNE_SECONDS (calls that never ran, spent nonces) are removed first."""
    now = time.time() if now is None else now
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _prune(directory, now)
    nonce = secrets.token_hex(16)
    record = {"worktree": worktree, "since": since, "expires": now + TTL_SECONDS}
    with open(directory / f"{nonce}.json", "x", encoding="utf-8") as fh:
        json.dump(record, fh)
    return nonce


def redeem(directory: Path, nonce: str | None, worktree: str | None, since: str | None,
           now: float | None = None) -> str | None:
    """Why the call must be refused, else None. The nonce must be 32 hex digits before it is joined into a path, so
    no value names a file outside `directory`. The first redeemer creates `<nonce>.used`, which stays until it is
    pruned, so a nonce is never redeemed twice; the token file is deleted whatever it holds."""
    if not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
        return "the hook token is missing or malformed"
    directory = Path(directory)
    path = directory / f"{nonce}.json"
    if not path.is_file():
        return "the hook token does not exist or was already used"
    try:
        # Exclusive creation decides between concurrent redeemers (several concurrent deletes of one file can all
        # succeed on Windows).
        os.close(os.open(directory / f"{nonce}.used", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        raw = path.read_bytes()
        path.unlink()
    except OSError:
        return "the hook token does not exist or was already used"
    try:
        record = json.loads(raw.decode("utf-8"))
        expires, issued_for, issued_since = record["expires"], record["worktree"], record["since"]
    except (ValueError, TypeError, KeyError, RecursionError):
        return "the hook token file is unreadable"
    if not (isinstance(expires, (int, float)) and not isinstance(expires, bool)
            and isinstance(issued_for, str) and (issued_since is None or isinstance(issued_since, str))):
        return "the hook token file is unreadable"
    if (time.time() if now is None else now) > expires:
        return "the hook token has expired"
    if not isinstance(worktree, str) or not _same_path(issued_for, worktree) or issued_since != since:
        return "the hook token was issued for another --worktree or --since"
    return None
