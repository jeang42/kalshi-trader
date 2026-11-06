"""Contract tests for the pm/ package layout (P3/R-24).

Two invariants that the package split can silently break, both found the hard
way on 2026-08-24 within an hour of Phase B landing:

1. File-path DEFAULTS must anchor on the repository root, never on
   `Path(__file__)`. When `emergency_stop` moved to pm/core/, its
   `__file__`-relative flag default silently became `pm/core/data/...` —
   nothing crashed, `status` just reported NOT ACTIVE while looking at a path
   nobody writes. For the kill switch and the per-profile data tree, "looks in
   the wrong place without erroring" is the worst available failure mode.
   (This is the sibling of docs/file-map.md non-API gotcha #1: freezing an env var at
   import captures the environment; anchoring on __file__ captures the
   LOCATION. Both look correct until something moves.)

2. The layering is strict: a pm/ package may import only from layers below
   it. This was verified by hand when the boundaries were drawn; this test
   keeps it true as modules keep moving in.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pm
from pm.core import emergency_stop, profiles

REPO_ROOT = Path(__file__).resolve().parent.parent


# Files that exist ONLY at the repository root, so finding all of them
# together means REPO_ROOT really is the root and not some package directory
# that happens to be an ancestor of a module.
#
# Deliberately NOT `.git`: that was the original check, and it made the test
# depend on repository METADATA rather than on repository CONTENT. Anyone who
# downloads a GitHub source ZIP instead of cloning gets the whole tree without
# `.git`, and this test — the first in the layout contract — failed for them
# on a correct checkout. Nothing in `pm.REPO_ROOT` needs git to be right;
# `pm/core/provenance.py` is the only module that reads `.git`, and it is
# documented to return None without one.
ROOT_MARKERS = ("pyproject.toml", "LICENSE", "README.md", "pm/__init__.py")


def test_repo_root_points_at_the_repository():
    assert pm.REPO_ROOT == REPO_ROOT
    missing = [m for m in ROOT_MARKERS if not (pm.REPO_ROOT / m).exists()]
    assert missing == [], (
        f"pm.REPO_ROOT ({pm.REPO_ROOT}) is missing root markers {missing} — "
        "it is not pointing at the repository root")


def test_repo_root_is_not_a_package_directory():
    """The failure mode this whole file exists for: when `emergency_stop` moved
    to pm/core/, a `__file__`-relative default became `pm/core/data/...` and
    nothing crashed. A root that is itself an importable package is the shape
    of that bug, so rule it out directly rather than inferring it from markers.
    """
    assert not (pm.REPO_ROOT / "__init__.py").exists(), (
        "pm.REPO_ROOT is an importable package directory, so it has resolved "
        "to a package instead of the repository root")


def test_emergency_stop_default_flag_is_at_the_repo_data_dir():
    assert emergency_stop.DEFAULT_PATH == REPO_ROOT / "data" / ".emergency_stop"


def test_profiles_default_root_is_the_repo_data_dir():
    assert profiles.DEFAULT_PROFILES_ROOT == REPO_ROOT / "data"


def test_no_file_relative_data_defaults_in_pm():
    """No pm/ module may build a path default from __file__ except pm itself
    (which derives REPO_ROOT) and provenance (which ASCENDS to .git — correct
    from any depth)."""
    allowed = {REPO_ROOT / "pm" / "__init__.py",
               REPO_ROOT / "pm" / "core" / "provenance.py"}
    def uses_file(p: Path) -> bool:
        tree = ast.parse(p.read_text())
        return any(isinstance(n, ast.Name) and n.id == "__file__"
                   for n in ast.walk(tree))

    offenders = [str(p.relative_to(REPO_ROOT))
                 for p in (REPO_ROOT / "pm").rglob("*.py")
                 if p not in allowed and uses_file(p)]
    assert offenders == [], (
        f"__file__-relative paths in {offenders}: anchor on pm.REPO_ROOT "
        "instead — __file__ silently changes meaning when a module moves")


LAYER_ORDER = ["core", "venue", "ledger", "feeds", "archive", "live",
               "research", "trading", "arms", "capture", "mention", "ops"]


def test_pm_layering_is_strict():
    rank = {name: i for i, name in enumerate(LAYER_ORDER)}
    violations = []
    for layer in LAYER_ORDER:
        for f in (REPO_ROOT / "pm" / layer).glob("*.py"):
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                for m in mods:
                    parts = m.split(".")
                    if parts[0] == "pm" and len(parts) > 1 and parts[1] in rank:
                        if rank[parts[1]] > rank[layer]:
                            violations.append(f"{f.name} ({layer}) -> {m}")
    assert violations == [], f"upward imports across pm/ layers: {violations}"


def test_order_audit_log_is_out_of_logrotate_reach():
    """The real-money audit trail must not match `scripts/logrotate.conf`.

    Regression guard for 2026-09-20: `DEFAULT_ORDERS_LOG` was
    `data/shared/orders.log`, the logrotate config globs
    `data/shared/*.log` daily with `rotate 14` + `copytruncate`, and so the
    append-only order audit trail was truncated nightly and every generation
    past two weeks deleted — all order history before ~2026-07-11 is
    permanently gone.

    The fix is positional (the log lives one directory down, and the glob is
    non-recursive) rather than an exclusion, because logrotate 3.21 treats a
    specific-stanza + glob overlap as a FATAL duplicate: measured, it stopped
    processing the pattern and silently dropped 28 of 47 other logs from
    rotation while exiting 1. So this asserts NON-MATCHING, not "excluded".
    """
    import glob as globmod
    import pathlib

    from pm.venue.kalshi_orders import DEFAULT_ORDERS_LOG

    conf = (pm.REPO_ROOT / "scripts" / "logrotate.conf").read_text()
    patterns = [
        line.split("{")[0].strip()
        for line in conf.splitlines()
        if "{" in line and not line.lstrip().startswith("#")
    ]
    assert patterns, "no logrotate stanzas parsed — did the config format change?"

    audit = (pm.REPO_ROOT / DEFAULT_ORDERS_LOG).resolve()
    for pat in patterns:
        # glob(3) semantics, which is what logrotate uses — `*` does NOT
        # cross a `/`, so a subdirectory is genuinely out of reach. fnmatch
        # would be wrong here: its `*` spans separators and reports a false
        # match on the very layout that fixes the bug.
        hit = {pathlib.Path(m).resolve() for m in globmod.glob(pat)}
        assert audit not in hit, (
            f"the order audit log {audit} is matched by logrotate pattern "
            f"{pat!r} — it would be truncated nightly and aged out. Keep it "
            "in a subdirectory the glob cannot reach."
        )
