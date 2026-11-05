"""Which code produced this result? (P2 / R-18)

No backtest run, research report, position row or audit line recorded the
commit that produced it, so several logged findings could not be
reproduced exactly: the parameters were known (``oos_ledger`` fingerprints
those) but not the code that applied them. This module is the one answer,
and it is deliberately boring: read the SHA from ``.git`` without spawning
a process, cache it for the life of the process (a daemon's loaded code
does not change if the working tree does), never raise.

* :func:`git_commit` → short SHA (``"3fc7cc3"``), or ``None`` when there is
  no ``.git`` (a container, a tarball). Env ``PROVENANCE_COMMIT`` overrides
  for exactly that case — bake it in at build time.
* :func:`is_dirty` → ``True`` if the working tree had uncommitted changes
  to tracked files when first asked (``git status --porcelain``, one
  subprocess, cached; ``None`` when git is unavailable). A dirty stamp is
  a warning label: "this SHA plus something".
* :func:`stamp` → the dict every JSON/JSONL writer embeds;
  :func:`stamp_line` → the one-line header every text report starts with.

What is stamped (all as of 2026-08-17): every arm position row (``commit_sha``
column, schema v3 — ``commit`` is a SQLite keyword), every real-order audit line in ``orders.log``, every
row in a profile's ``exits.jsonl`` and in ``reactive_log.jsonl``, every
saved backtest run, every text report under ``data/shared/*_report*.txt``,
and the ``oos_ledger --status`` header. Historic rows predate the stamp and
read ``NULL`` / absent — that is the honest answer, not a default.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

SHORT = 7


def _repo_root(start: Path | None = None) -> Path | None:
    p = start if start is not None else Path(__file__).resolve().parent
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def _read_head_sha(root: Path) -> str | None:
    """Resolve HEAD → SHA by reading .git files (no subprocess). Handles a
    symbolic ref, a detached HEAD, packed refs, and a `.git` file (worktree)."""
    git = root / ".git"
    if git.is_file():                       # worktree: "gitdir: <path>"
        try:
            target = git.read_text().strip().split(":", 1)[1].strip()
        except (OSError, IndexError):
            return None
        gitdir = (root / target).resolve()
        common = gitdir / "commondir"
        head_dir = gitdir
        refs_root = (gitdir / common.read_text().strip()).resolve() if common.exists() else gitdir
    else:
        head_dir = refs_root = git
    try:
        head = (head_dir / "HEAD").read_text().strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head or None
    ref = head.split(":", 1)[1].strip()
    loose = refs_root / ref
    try:
        return loose.read_text().strip() or None
    except OSError:
        pass
    packed = refs_root / "packed-refs"
    try:
        for line in packed.read_text().splitlines():
            if line.startswith("#") or line.startswith("^"):
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    except OSError:
        pass
    return None


@lru_cache(maxsize=1)
def git_commit(short: bool = True) -> str | None:
    """Short SHA of HEAD, or None. Cached for the process. Env
    ``PROVENANCE_COMMIT`` wins (for builds without a ``.git``)."""
    env = os.getenv("PROVENANCE_COMMIT")
    if env:
        return env[:SHORT] if short else env
    root = _repo_root()
    sha = _read_head_sha(root) if root else None
    if sha is None and root is not None:
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                 text=True, timeout=5).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            sha = None
    return (sha[:SHORT] if (sha and short) else sha) or None


@lru_cache(maxsize=1)
def is_dirty() -> bool | None:
    """Uncommitted changes to tracked files at first call? None if unknown."""
    if os.getenv("PROVENANCE_COMMIT"):
        return False
    root = _repo_root()
    if root is None:
        return None
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                             cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return bool(out.stdout.strip())


def stamp() -> dict:
    """``{"commit": "3fc7cc3", "dirty": False, "python": "3.11.9",
    "stamped_at": "...Z"}`` — embed in any JSON result."""
    return {
        "commit": git_commit(),
        "dirty": is_dirty(),
        "python": sys.version.split()[0],
        "stamped_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def stamp_line(prefix: str = "# ") -> str:
    """One-line header for text reports.

    >>> line = stamp_line("# ")
    >>> line.startswith("# produced by commit ")
    True
    """
    sha = git_commit() or "unknown"
    dirty = is_dirty()
    state = "dirty" if dirty else ("clean" if dirty is False else "unknown-state")
    return f"{prefix}produced by commit {sha} ({state}) at {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}"


def label() -> str:
    """Compact ``"3fc7cc3"`` / ``"3fc7cc3+dirty"`` / ``"unknown"`` for logs and tables."""
    sha = git_commit()
    if not sha:
        return "unknown"
    return f"{sha}+dirty" if is_dirty() else sha
