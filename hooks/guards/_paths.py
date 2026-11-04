#!/usr/bin/env python3
"""Path normalisation shared by the review guards: forward slashes, `.`/`..` collapsed, a drive-letter path
compared case-insensitively. Works for POSIX (`/x/y`) and Windows (`C:/x/y`, `C:\\x\\y`) spellings."""
from __future__ import annotations

import posixpath
import re
import sys

_DRIVE = re.compile(r"^[A-Za-z]:/")
_MSYS = re.compile(r"^/([a-zA-Z])(/.*)?$")


def is_absolute(path: str) -> bool:
    p = path.replace("\\", "/")
    return p.startswith("/") or bool(_DRIVE.match(p))


def canon(path: str) -> str:
    """Comparison form of an absolute path: forward slashes, normalised, lower-cased when it has a drive letter
    or the host is Windows."""
    p = path.strip().strip("'\"").replace("\\", "/")
    if sys.platform == "win32":
        m = _MSYS.match(p)
        if m:
            p = f"{m.group(1)}:{m.group(2) or '/'}"
    head = p[:2] if _DRIVE.match(p) else ""
    p = head + posixpath.normpath(p[len(head):] or "/")
    return p.lower() if head or sys.platform == "win32" else p


def under(path: str, root: str) -> bool:
    """`path` is `root` or inside it (both canonicalised)."""
    a, b = canon(path), canon(root).rstrip("/")
    return a == b or a.startswith(b + "/")


def has_dot_segment(path: str) -> bool:
    return any(seg in (".", "..") for seg in path.replace("\\", "/").split("/"))
