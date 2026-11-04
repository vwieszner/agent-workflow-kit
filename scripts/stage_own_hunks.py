#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Stage only THIS session's hunks into a shared git index, and gate the commit on content.

The main checkout's git index is shared by every concurrently running session: `git commit --only <file>`
commits the WORKING-TREE copy of that file, which includes any other session's uncommitted edits in the same
file. Staging exactly your own hunks and committing normally is the safe path.

Splits a file's `git diff -U0` into numbered hunks, lists them, builds a patch of the chosen (sub)hunks, and
applies it with `git apply --cached --unidiff-zero` -- the working tree is never touched, so other sessions'
uncommitted edits in the same file stay unstaged.

Usage
-----
    # numbered hunks (bodies truncated to maxlines, default 12)
    uv run --no-project .workflow/scripts/stage_own_hunks.py list <file> [maxlines]

    # write a -U0 patch holding only the chosen (sub)hunks, without staging
    uv run --no-project .workflow/scripts/stage_own_hunks.py pick <file> <out> <spec>

    # pick + apply --cached --unidiff-zero + print `git diff --cached --stat`
    uv run --no-project .workflow/scripts/stage_own_hunks.py stage <file> <spec>

    # gated commit on the shared index -- one shell command:
    T=$(uv run --no-project .workflow/scripts/stage_own_hunks.py gate \
          [--hunks <file> <spec>]... [--add <file>]...) \
      && git commit -F <msgfile> \
      && uv run --no-project .workflow/scripts/stage_own_hunks.py verify "$T"

    # target a worktree explicitly (default: `git rev-parse --show-toplevel` of cwd)
    uv run --no-project .workflow/scripts/stage_own_hunks.py stage <file> <spec> --repo <worktree>

spec is comma-separated:
    N        whole hunk N
    N:k      k-th removed line paired with k-th added line of hunk N (1:1 row replacement)
    N:+a-b   added lines a..b of pure-addition hunk N
Headers are recomputed with a running offset.

`gate` takes `--hunks <file> <spec>` (repeatable) for files that also hold other sessions' edits and
`--add <file>` (repeatable) for files holding only this session's changes. Spec numbers are the ones `list`
printed while the index was clean.

When to use
-----------
    Committing to the main checkout (the sprint-status tracker, the slot registry, epics, story history)
    while other sessions or background agents may stage in the same index: `list`, then `gate` +
    `git commit` + `verify`. `stage` alone only when the commit is checked by hand.

Shared index
------------
`gate` checks content, not file names:
  1. refuses (exit 3, nothing staged) unless the index equals HEAD;
  2. builds the expected tree in a scratch index: HEAD + this run's hunks and --add files;
  3. stages the same changes into the shared index and refuses (exit 3, undo commands printed) unless the
     shared index's tree equals the expected tree -- a foreign hunk in a file this run also stages is caught;
  4. prints the expected tree on stdout (its only stdout line; messages go to stderr).
The caller then runs `git commit` with no paths (never `--only`). `verify <tree>` exits 4 when HEAD's tree
differs from the expected tree (the index changed between `gate` and the commit) and lists the foreign paths;
it never amends. After a bare `stage`, check `git diff --cached` yourself, then `git commit` with no paths.

Exit codes: 0 = ok; 1 = a hunk spec was invalid or `git apply` / `git add` failed; 2 = git/setup error;
3 = `gate` found the index holding other changes; 4 = `verify` found the commit holding more than this run's
changes.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

HDR = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
# Every parsed `git diff` and the patch built from it takes these: user config (color.diff, diff.external, a
# textconv attribute, diff.noprefix, diff.mnemonicPrefix, diff.interHunkContext) would otherwise change the
# hunks read or the patch applied.
DIFF_ARGS = ("-U0", "--inter-hunk-context=0", "--no-color", "--no-ext-diff", "--no-textconv", "--src-prefix=a/",
             "--dst-prefix=b/")


def default_repo() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        sys.exit(f"not inside a git repo: {proc.stderr.strip()}")
    return proc.stdout.strip()


def split(repo, path):
    out = subprocess.run(["git", "-C", repo, "diff", *DIFF_ARGS, "--", path],
                         capture_output=True, check=True).stdout.decode("utf-8")
    header, hunks, cur = [], [], None
    for ln in out.splitlines(keepends=True):
        if ln.startswith("@@"):
            cur = [ln]
            hunks.append(cur)
        elif cur is None:
            header.append(ln)
        else:
            cur.append(ln)
    return header, hunks


def parse(h):
    m = HDR.match(h[0].rstrip("\n"))
    old_s = int(m.group(1))
    old_c = int(m.group(2)) if m.group(2) is not None else 1
    body = h[1:]
    minus = [ln for ln in body if ln.startswith("-")]
    plus = [ln for ln in body if ln.startswith("+")]
    tail = [ln for ln in body if ln.startswith("\\")]
    return old_s, old_c, minus, plus, tail


def fmt(n, c):
    return f"{n}" if c == 1 else f"{n},{c}"


def build_patch(repo, path, spec):
    """Return the patch text for `spec` against `path`, and the hunk count."""
    header, hunks = split(repo, path)
    pieces = []  # (old_start, old_count, minus, plus)
    for item in spec.split(","):
        if ":" not in item:
            old_s, old_c, minus, plus, tail = parse(hunks[int(item) - 1])
            if tail:
                sys.exit(f"hunk {item} has a no-newline marker; refusing")
            pieces.append((old_s, old_c, minus, plus))
            continue
        n, sub = item.split(":")
        old_s, old_c, minus, plus, tail = parse(hunks[int(n) - 1])
        if sub.startswith("+"):
            if old_c != 0:
                sys.exit(f"{item}: hunk is not pure-addition")
            a, b = (int(x) for x in sub[1:].split("-"))
            pieces.append((old_s, 0, [], plus[a - 1:b]))
        else:
            k = int(sub)
            if len(minus) != len(plus):
                sys.exit(f"{item}: removed/added counts differ ({len(minus)}/{len(plus)})")
            pieces.append((old_s + k - 1, 1, [minus[k - 1]], [plus[k - 1]]))
    pieces.sort(key=lambda p: p[0])
    offset = 0
    out = ["".join(header)]
    for old_s, old_c, minus, plus in pieces:
        new_c = len(plus)
        if old_c == 0:
            new_s = old_s + 1 + offset
        elif new_c == 0:
            new_s = old_s - 1 + offset
        else:
            new_s = old_s + offset
        out.append(f"@@ -{fmt(old_s, old_c)} +{fmt(new_s, new_c)} @@\n")
        out.append("".join(minus) + "".join(plus))
        offset += new_c - old_c
    return "".join(out), len(hunks), len(pieces)


def cmd_list(repo, path, maxl):
    _, hunks = split(repo, path)
    for i, h in enumerate(hunks, 1):
        print(f"#{i} {h[0].rstrip()}")
        for ln in h[1:1 + maxl]:
            print("   " + ln.rstrip()[:260])
        if len(h) - 1 > maxl:
            print(f"   ... (+{len(h) - 1 - maxl} lines)")


def cmd_pick(repo, path, outp, spec):
    patch, n_hunks, n_pieces = build_patch(repo, path, spec)
    # newline="" preserves the file's own line endings (CR bytes stay embedded in
    # the decoded text) instead of letting the platform default translate them.
    with open(outp, "w", encoding="utf-8", newline="") as fh:
        fh.write(patch)
    print(f"wrote {n_pieces} piece(s) from {n_hunks} hunks -> {outp}")


def cmd_stage(repo, path, spec):
    patch, n_hunks, n_pieces = build_patch(repo, path, spec)
    proc = subprocess.run(
        ["git", "-C", repo, "apply", "--cached", "--unidiff-zero", "-"],
        input=patch.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", errors="replace"))
        sys.exit(1)
    print(f"staged {n_pieces} piece(s) from {n_hunks} hunks in {path}")
    stat = subprocess.run(
        ["git", "-C", repo, "diff", "--cached", "--stat", "--", path],
        capture_output=True,
    )
    sys.stdout.write(stat.stdout.decode("utf-8", errors="replace"))


def _git(repo, args, index=None, data=None):
    """Run git in `repo`; `index` points GIT_INDEX_FILE at a scratch index."""
    env = dict(os.environ, GIT_INDEX_FILE=index) if index else None
    return subprocess.run(["git", "-C", repo, *args], input=data,
                          capture_output=True, env=env)


def _out(proc):
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _say(text):
    """`gate` messages go to stderr: its stdout is the expected tree alone."""
    print(text, file=sys.stderr)


def _fail(proc, what, code):
    sys.stderr.write(f"{what} failed:\n" + proc.stderr.decode("utf-8", errors="replace"))
    sys.exit(code)


def _stage_all(repo, patches, adds, index=None):
    """Apply every patch and add every file; return (proc, what) of the first failure."""
    for path, patch in patches:
        proc = _git(repo, ["apply", "--cached", "--unidiff-zero", "-"], index,
                    patch.encode("utf-8"))
        if proc.returncode != 0:
            return proc, f"git apply --cached ({path})"
    if adds:
        proc = _git(repo, ["add", "--", *adds], index)
        if proc.returncode != 0:
            return proc, "git add"
    return None


def _print_undo(undo):
    _say("undo what this run staged (only the steps that ran) with:")
    for line in undo:
        _say("  " + line)


def cmd_gate(argv):
    ap = argparse.ArgumentParser(prog="stage_own_hunks.py gate")
    ap.add_argument("--hunks", nargs=2, action="append", default=[], metavar=("FILE", "SPEC"))
    ap.add_argument("--add", action="append", default=[], metavar="FILE")
    ap.add_argument("--repo")
    a = ap.parse_args(argv)
    if not a.hunks and not a.add:
        ap.error("nothing to stage: give --hunks FILE SPEC and/or --add FILE")
    repo = a.repo or default_repo()

    # 1. The index must equal HEAD: anything staged belongs to someone else.
    staged = _git(repo, ["diff", "--cached", "--name-only"])
    if staged.returncode != 0:
        _fail(staged, "git diff --cached", 2)
    if _out(staged):
        _say("INDEX NOT EMPTY - staged by another session; nothing staged.")
        _say("Retry once its owner commits; never unstage another session's changes:")
        _say(_out(staged))
        sys.exit(3)

    patches = [(path, build_patch(repo, path, spec)[0]) for path, spec in a.hunks]

    # 2. Expected tree: HEAD + this run's changes, built in a scratch index.
    work = tempfile.mkdtemp(prefix="stage_own_hunks-")
    scratch = os.path.join(work, "index")
    proc = _git(repo, ["read-tree", "HEAD"], scratch)
    if proc.returncode != 0:
        _fail(proc, "git read-tree HEAD (scratch index)", 2)
    failed = _stage_all(repo, patches, a.add, scratch)
    if failed:
        _fail(*failed, 1)
    proc = _git(repo, ["write-tree"], scratch)
    if proc.returncode != 0:
        _fail(proc, "git write-tree (scratch index)", 2)
    expected = _out(proc)

    undo = []
    for i, (path, patch) in enumerate(patches, 1):
        saved = os.path.join(work, f"{i}.patch")
        with open(saved, "w", encoding="utf-8", newline="") as fh:
            fh.write(patch)
        undo.append(f"git -C {repo} apply --cached -R --unidiff-zero {saved}   # {path}")
    if a.add:
        undo.append(f"git -C {repo} restore --staged -- {' '.join(a.add)}"
                    "   # also unstages anything another session staged in these paths")

    # 3. Stage into the shared index; its tree must equal the expected tree.
    failed = _stage_all(repo, patches, a.add)
    if failed:
        _print_undo(undo)
        _fail(*failed, 1)
    proc = _git(repo, ["write-tree"])
    if proc.returncode != 0:
        _print_undo(undo)
        _fail(proc, "git write-tree (shared index)", 2)
    actual = _out(proc)
    if actual != expected:
        _say("INDEX CHANGED while staging - it holds more than this run's changes; do not commit:")
        _say(_out(_git(repo, ["diff-tree", "-r", "--name-status", expected, actual])))
        _print_undo(undo)
        sys.exit(3)

    # 4. The expected tree is the only stdout line, for `verify`.
    shutil.rmtree(work, ignore_errors=True)
    _say(f"staged {len(patches)} patch(es) and {len(a.add)} file(s); index tree = expected tree")
    print(expected)


def cmd_verify(argv):
    ap = argparse.ArgumentParser(prog="stage_own_hunks.py verify")
    ap.add_argument("tree", help="the expected tree `gate` printed")
    ap.add_argument("--repo")
    a = ap.parse_args(argv)
    repo = a.repo or default_repo()
    want = _git(repo, ["rev-parse", "--verify", "-q", f"{a.tree}^{{tree}}"])
    if want.returncode != 0:
        sys.stderr.write(f"not a tree: {a.tree!r}\n")
        sys.exit(2)
    head_tree = _git(repo, ["rev-parse", "HEAD^{tree}"])
    if head_tree.returncode != 0:
        _fail(head_tree, "git rev-parse HEAD^{tree}", 2)
    sha = _out(_git(repo, ["rev-parse", "--short", "HEAD"]))
    if _out(head_tree) != _out(want):
        print(f"COMMIT {sha} HOLDS MORE THAN THIS RUN'S CHANGES "
              "(the index changed between `gate` and the commit):")
        print(_out(_git(repo, ["diff-tree", "-r", "--name-status", _out(want), _out(head_tree)])))
        sys.exit(4)
    print(f"verified {sha}: its tree is exactly this run's changes")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if len(sys.argv) >= 2 and sys.argv[1] in ("gate", "verify"):
        (cmd_gate if sys.argv[1] == "gate" else cmd_verify)(sys.argv[2:])
        return
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    mode, path = sys.argv[1], sys.argv[2]
    rest = sys.argv[3:]
    repo = None
    if "--repo" in rest:
        i = rest.index("--repo")
        repo = rest[i + 1]
        del rest[i:i + 2]
    repo = repo or default_repo()

    if mode == "list":
        maxl = int(rest[0]) if rest else 12
        cmd_list(repo, path, maxl)
    elif mode == "pick":
        outp, spec = rest[0], rest[1]
        cmd_pick(repo, path, outp, spec)
    elif mode == "stage":
        spec = rest[0]
        cmd_stage(repo, path, spec)
    else:
        sys.exit(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
