"""Small git helpers for the change summary. All calls are bounded in time and size."""

from __future__ import annotations

import subprocess
from typing import Any

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
TEXT_CAP = 20_000
GIT_TIMEOUT = 15


def _git(cwd: str, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, p.stdout


def _cap(s: str) -> str:
    if len(s) <= TEXT_CAP:
        return s
    return s[:TEXT_CAP] + f"\n... [truncated, {len(s) - TEXT_CAP} more chars]"


def snapshot(cwd: str) -> dict[str, Any] | None:
    """Record repo state at prompt start; None if cwd is not inside a git work tree."""
    rc, out = _git(cwd, "rev-parse", "--show-toplevel")
    if rc != 0:
        return None
    top = out.strip()
    rc, head = _git(cwd, "rev-parse", "--verify", "-q", "HEAD")
    rc_b, branch = _git(cwd, "symbolic-ref", "--short", "-q", "HEAD")
    return {
        "toplevel": top,
        "head": head.strip() if rc == 0 and head.strip() else None,
        "branch": branch.strip() if rc_b == 0 else None,
    }


def summary(cwd: str, start: dict[str, Any] | None) -> dict[str, Any] | None:
    if start is None:
        snap = snapshot(cwd)
        if snap is None:
            return None
        start = {"head": None, "branch": None, "note": "cwd became a git repo during the prompt"}
    now = snapshot(cwd)
    if now is None:
        return {"error": "cwd is no longer a git work tree"}
    base = start.get("head")
    _, status = _git(cwd, "status", "--short")
    _, diffstat = _git(cwd, "diff", "--stat", base or EMPTY_TREE)
    if base:
        _, log = _git(cwd, "log", "--oneline", "--no-decorate", "-n", "50", f"{base}..HEAD")
    elif now.get("head"):
        _, log = _git(cwd, "log", "--oneline", "--no-decorate", "-n", "50", "HEAD")
    else:
        log = ""
    commits = [line for line in log.splitlines() if line.strip()]
    return {
        "branch": now.get("branch"),
        "start_branch": start.get("branch"),
        "start_head": base,
        "head": now.get("head"),
        "status_short": _cap(status),
        "diff_stat_vs_start": _cap(diffstat),
        "new_commits": commits,
    }
