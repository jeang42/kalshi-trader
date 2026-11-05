"""Consistent SQLite snapshots for the nightly backup.

The problem this solves
-----------------------
The backup used to ``rsync`` the whole ``data/`` tree directly. Several capture
services write continuously, so at 05:30 UTC their databases had non-empty
write-ahead logs. ``rsync`` copies the main file and its ``-wal`` at different
instants and has no idea they are related, so a restored snapshot could be
internally inconsistent — for exactly the stores that were most active, which
are the ones worth backing up. Nobody had ever tested a restore, so this was
invisible.

The fix is SQLite's online backup API (``Connection.backup``), which takes a
transactionally consistent copy of a live database without blocking writers.
Each database is staged to a cold directory and ``PRAGMA integrity_check``ed
there; only then does the file-level copy run — against files nothing is
writing, which also lets ``rsync --link-dest`` hardlink unchanged snapshots
properly instead of re-copying gigabytes nightly.

Size policy
-----------
One archive in this tree is tens of gigabytes and belongs to a retired
research thesis. Copying it nightly is what pushed the backup target past 2 TB.
Databases above ``--max-mb`` are **skipped and named in the output** rather than
silently dropped: a backup that quietly omits things is worse than one that
refuses, because you find out at restore time. Naming them in a log nobody
reads proved not to be enough, so ``--skipped-file`` hands the names to
``backup.sh``, which records them in ``backup_status.json``; the health digest
pages on any skip not listed in ``BACKUP_SKIP_EXPECTED``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

#: Databases larger than this are skipped unless force-included. Sized to keep
#: a retired 40 GB-class archive out while letting the live study stores in:
#: at 2048 the candle panel crossed the cap ~2026-10-01 and was silently left
#: at its Oct-1 copy for 12 nights while every run reported "ok".
DEFAULT_MAX_MB = 8192.0

#: Over-cap databases whose absence is a known, deliberate decision. They are
#: still skipped, but reported as EXPECTED so the health digest notes them
#: instead of paging nightly. book_archive is the retired kalshi_ws/book_poller
#: archive (frozen 2026-08-11), already a symlink into cold storage on the
#: backup drive itself. Override with BACKUP_SKIP_EXPECTED (comma-separated).
DEFAULT_SKIP_EXPECTED = "book_archive.sqlite"

#: WAL/SHM sidecars: never copied directly. The staged snapshot already
#: contains everything they held, and copying them alongside a snapshot taken
#: at a different instant is the inconsistency we are removing.
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass
class Result:
    """Outcome for one database."""

    source: Path
    staged: Path | None
    size_mb: float
    seconds: float
    status: str          # "ok" | "skipped-too-large" | "failed-integrity" | "error"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def find_databases(root: Path) -> list[Path]:
    """Every SQLite database under ``root``, excluding WAL/SHM sidecars.

    >>> find_databases(Path("/nonexistent"))
    []
    """
    if not root.exists():
        return []
    out: list[Path] = []
    for p in sorted(root.rglob("*.sqlite*")):
        if not p.is_file():
            continue
        if any(p.name.endswith(sfx) for sfx in SIDECAR_SUFFIXES):
            continue
        out.append(p)
    return out


def should_skip(size_mb: float, max_mb: float, name: str,
                force_include: set[str]) -> bool:
    """Size policy, isolated so it is testable without touching a disk.

    >>> should_skip(10.0, 2048.0, "small.sqlite", set())
    False
    >>> should_skip(43000.0, 2048.0, "book_archive.sqlite", set())
    True
    >>> should_skip(43000.0, 2048.0, "book_archive.sqlite", {"book_archive.sqlite"})
    False
    >>> should_skip(43000.0, 0.0, "anything.sqlite", set())   # 0 disables the cap
    False
    """
    if max_mb <= 0 or name in force_include:
        return False
    return size_mb > max_mb


def snapshot_one(src: Path, dest: Path) -> Result:
    """Take a consistent snapshot of ``src`` at ``dest`` and verify it.

    Uses the online backup API, so a writer holding the database open does not
    block or corrupt the copy. The integrity check runs against the *staged*
    file — verifying the source would prove nothing about what we stored.
    """
    started = time.time()
    size_mb = src.stat().st_size / 1e6
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    try:
        if tmp.exists():
            tmp.unlink()
        # Read-only source: a backup must never be able to modify what it reads.
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            out = sqlite3.connect(str(tmp))
            try:
                con.backup(out)
            finally:
                out.close()
        finally:
            con.close()

        check = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if verdict != "ok":
            return Result(src, None, size_mb, time.time() - started,
                          "failed-integrity", verdict)

        os.replace(tmp, dest)
        return Result(src, dest, size_mb, time.time() - started, "ok")
    except Exception as e:  # noqa: BLE001 - one bad db must not stop the rest
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return Result(src, None, size_mb, time.time() - started, "error", str(e))


#: Records the source (mtime_ns, size) each staged file was built from, so an
#: unchanged database can be left alone on the next run — see stage_all.
MANIFEST_NAME = ".stage_manifest.json"


def _load_manifest(staging: Path) -> dict[str, list]:
    try:
        with open(staging / MANIFEST_NAME) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_manifest(staging: Path, manifest: dict[str, list]) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    tmp = staging / (MANIFEST_NAME + ".tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, staging / MANIFEST_NAME)


def is_unchanged(src: Path, staged: Path, recorded: list | None) -> bool:
    """True when ``src`` has not changed since ``staged`` was built from it.

    Compares the SOURCE's (mtime_ns, size) against what was recorded when the
    snapshot was taken. The staged file's own timestamps are useless for this:
    it is rewritten by the backup API, not copied.

    >>> is_unchanged(Path("/nope"), Path("/nope2"), None)
    False
    """
    if recorded is None or not staged.exists():
        return False
    try:
        st = src.stat()
    except OSError:
        return False
    return list(recorded) == [st.st_mtime_ns, st.st_size]


def stage_all(data_root: Path, staging: Path, *, max_mb: float = DEFAULT_MAX_MB,
              force_include: set[str] | None = None) -> list[Result]:
    """Snapshot every database under ``data_root`` into ``staging``, mirroring
    the relative layout so a restore is a plain copy back.

    A database whose source is byte-for-byte unchanged since the last run is
    **left alone**, which is what actually makes ``rsync --link-dest``
    hardlink it into the new snapshot. Re-running the backup API would mint a
    fresh inode and mtime for identical content, and rsync would copy it again
    — measured as two full 1.8 GB snapshots instead of one plus diffs. Most of
    the volume here belongs to archives of retired services that never change.
    """
    force_include = force_include or set()
    manifest = _load_manifest(staging)
    fresh: dict[str, list] = {}
    results: list[Result] = []
    for src in find_databases(data_root):
        stat = src.stat()
        size_mb = stat.st_size / 1e6
        if should_skip(size_mb, max_mb, src.name, force_include):
            results.append(Result(src, None, size_mb, 0.0, "skipped-too-large",
                                  f"{size_mb / 1024:.1f} GB > {max_mb / 1024:.1f} GB cap"))
            continue
        rel = src.relative_to(data_root)
        dest = staging / rel
        key = str(rel)
        if is_unchanged(src, dest, manifest.get(key)):
            fresh[key] = [stat.st_mtime_ns, stat.st_size]
            results.append(Result(src, dest, size_mb, 0.0, "ok", "unchanged — reused"))
            continue
        res = snapshot_one(src, dest)
        if res.ok:
            fresh[key] = [stat.st_mtime_ns, stat.st_size]
        results.append(res)
    _save_manifest(staging, fresh)
    return results


def verify_snapshot(snapshot_root: Path) -> list[Result]:
    """Run ``PRAGMA integrity_check`` over every database in a stored snapshot.

    This is the check that closes "no restore has ever been tested": point it
    at a real snapshot directory and it proves the databases in it open and are
    structurally sound.
    """
    results: list[Result] = []
    for db in find_databases(snapshot_root):
        started = time.time()
        size_mb = db.stat().st_size / 1e6
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                con.close()
            status = "ok" if verdict == "ok" else "failed-integrity"
            results.append(Result(db, db, size_mb, time.time() - started,
                                  status, "" if status == "ok" else verdict))
        except Exception as e:  # noqa: BLE001
            results.append(Result(db, None, size_mb, time.time() - started,
                                  "error", str(e)))
    return results


def format_results(results: list[Result], *, title: str) -> str:
    lines = [f"  {title}"]
    for r in sorted(results, key=lambda x: -x.size_mb):
        mark = {"ok": "ok  ", "skipped-too-large": "SKIP",
                "failed-integrity": "BAD ", "error": "ERR "}.get(r.status, "?   ")
        line = f"    [{mark}] {r.size_mb:>9.1f} MB  {r.seconds:>6.2f}s  {r.source.name}"
        if r.detail:
            line += f"   ({r.detail})"
        lines.append(line)
    n_ok = sum(1 for r in results if r.ok)
    n_skip = sum(1 for r in results if r.status == "skipped-too-large")
    n_bad = len(results) - n_ok - n_skip
    lines.append(f"    -> {n_ok} ok, {n_skip} skipped, {n_bad} failed")
    if n_skip:
        lines.append("    NOTE: skipped databases are NOT in this backup. "
                     "Set BACKUP_FORCE_INCLUDE or raise --max-mb to include them.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--staging", help="where to write consistent snapshots")
    ap.add_argument("--max-mb", type=float,
                    default=float(os.getenv("BACKUP_MAX_SQLITE_MB", DEFAULT_MAX_MB)))
    ap.add_argument("--force-include", default=os.getenv("BACKUP_FORCE_INCLUDE", ""),
                    help="comma-separated database filenames to include regardless of size")
    ap.add_argument("--skipped-file", metavar="PATH",
                    help="write the skipped databases' names here as JSON "
                         '({"skipped": [...], "skipped_expected": [...]}), so '
                         "backup.sh can carry them into backup_status.json")
    ap.add_argument("--skip-expected",
                    default=os.getenv("BACKUP_SKIP_EXPECTED", DEFAULT_SKIP_EXPECTED),
                    help="comma-separated names whose skip is deliberate (noted, not paged)")
    ap.add_argument("--verify", metavar="SNAPSHOT_DIR",
                    help="integrity-check every database in a stored snapshot and exit")
    args = ap.parse_args(argv)

    if args.verify:
        results = verify_snapshot(Path(args.verify))
        if not results:
            print(f"  no databases found under {args.verify}")
            return 1
        print(format_results(results, title=f"restore verification — {args.verify}"))
        return 0 if all(r.ok for r in results) else 1

    if not args.staging:
        ap.error("--staging is required unless --verify is given")
    force = {p.strip() for p in args.force_include.split(",") if p.strip()}
    results = stage_all(Path(args.data_root), Path(args.staging),
                        max_mb=args.max_mb, force_include=force)
    print(format_results(results, title="sqlite snapshots (online backup + integrity check)"))
    if args.skipped_file:
        expected = {p.strip() for p in args.skip_expected.split(",") if p.strip()}
        names = sorted(r.source.name for r in results if r.status == "skipped-too-large")
        Path(args.skipped_file).write_text(json.dumps({
            "skipped": [n for n in names if n not in expected],
            "skipped_expected": [n for n in names if n in expected],
        }), encoding="utf-8")
    # A skip is a policy decision, not a failure. A corrupt or errored database is.
    return 0 if all(r.ok or r.status == "skipped-too-large" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
