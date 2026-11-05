"""User profiles — Option A from the multi-user discussion.

A profile is a named directory under `data/` that holds one user's local
state (portfolio, forecasts, alerts, settings). The active profile is
remembered in `data/.active_profile` between runs. Switching profiles
takes effect on the next data-module operation because each storage path
is looked up lazily (see `_load`/`_save` in portfolio.py, calibration.py,
alerts.py, risk.py).

This is intentionally a **file-system-only** design with no authentication
— intended for trusted users sharing one machine. Concurrent multi-user
or remote access is out of scope; that's Option B in the design and lands
when we replatform off Streamlit.

Filesystem layout::

    data/
      .active_profile    one-line file with the current profile name
      jeff/
        portfolio.json
        forecasts.json
        alerts.json
        settings.json
      dex/
        ...
      sandbox/           wipeable via the Settings page
        ...
      shared/            cross-profile artifacts (forecast library, strategies);
                          intentionally empty for now, populated as features land
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from pm import REPO_ROOT

log = logging.getLogger(__name__)

#: Default location of the per-profile data tree.
# Anchored on the REPO root (see pm/__init__.py) — this is the default root
# of the entire per-profile data tree; a __file__-relative default became
# pm/core/data/ when R-24 moved the module.
DEFAULT_PROFILES_ROOT = REPO_ROOT / "data"


def profiles_root() -> Path:
    """The per-profile data tree, resolved on every call.

    Env override: ``POLYMARKET_DATA_DIR``.

    Resolved lazily rather than frozen at import for the same reason every
    other path in this project is (see the "lazy file-path pattern" note in
    docs/file-map.md): a module-level constant captures whatever was set when the
    module happened to be imported. Here that had a specific consequence — the
    test suite could not redirect it, so tests that passed a profile name wrote
    into the REAL data tree, which is how synthetic rows ended up in production
    stores.
    """
    env = os.getenv("POLYMARKET_DATA_DIR", "").strip()
    return Path(env) if env else DEFAULT_PROFILES_ROOT


def active_profile_file() -> Path:
    return profiles_root() / ".active_profile"


def __getattr__(name: str):
    """Keep ``profiles.PROFILES_ROOT`` / ``ACTIVE_PROFILE_FILE`` working for
    existing callers, but resolve them on access instead of at import.

    Module-level ``__getattr__`` (PEP 562) means these read like constants and
    behave like functions — so nothing outside this module had to change.
    """
    if name == "PROFILES_ROOT":
        return profiles_root()
    if name == "ACTIVE_PROFILE_FILE":
        return active_profile_file()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
# Process-local active-profile override. When set, takes precedence over the
# shared `.active_profile` file — mirrors the lazy file-path env overrides
# (POLYMARKET_*_FILE) so a background daemon's identity (including the
# kalshi_auth owner-profile lock) is process-local and doesn't depend on
# whatever the UI last selected globally. Set by long-running runners around
# their cycle.
ACTIVE_PROFILE_ENV = "POLYMARKET_ACTIVE_PROFILE"
SHARED_DIR_NAME = "shared"

DEFAULT_PROFILES: list[str] = ["jeff", "dex", "sandbox", "agentic"]
"""Profiles created at first launch. The user can add more later.

The ``agentic`` profile is intended to be auto-driven by
``strategy_runner.py`` (a cron-able script that picks a registered
strategy and records its signals as forecasts + paper buys into this
profile). The point is to accumulate "what would full automation have
produced?" calibration data without trusting any source enough to
ship it as a real-money recommendation yet."""

DEFAULT_ACTIVE_PROFILE = "jeff"

# Profile names that are driven by an automated runner rather than by
# user clicks. Action pages flag these so accidental manual edits don't
# muddle the experiment. The runner creates these profiles lazily on
# first invocation — only the prefix matters for the banner check.
AGENTIC_PROFILE_PREFIX = "agentic"


def is_agentic_profile(name: str) -> bool:
    """True when the given profile name is meant to be auto-driven by
    ``strategy_runner.py``. Matches the bare name ``agentic`` and any
    profile prefixed with ``agentic_`` (e.g. ``agentic_favorites``)
    so the user can run multiple parallel experiments under different
    profile names without each one needing its own registration."""
    return name == AGENTIC_PROFILE_PREFIX or name.startswith(
        AGENTIC_PROFILE_PREFIX + "_",
    )

# File kinds each profile owns. Centralized here so adding a new kind only
# requires editing this constant + the new data module.
PROFILE_FILE_KINDS = ("portfolio", "forecasts", "alerts", "settings")


# ---------------------------------------------------------------------------
# Profile discovery and management
# ---------------------------------------------------------------------------

def profile_dir(name: str) -> Path:
    """The directory for the named profile. Doesn't ensure existence."""
    return profiles_root() / name


def list_profiles() -> list[str]:
    """All profile directory names, alphabetised. Excludes `shared/` and any
    dotfiles. Returns the defaults if the data directory doesn't exist yet."""
    if not profiles_root().exists():
        return list(DEFAULT_PROFILES)
    names = [
        p.name for p in profiles_root().iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != SHARED_DIR_NAME
    ]
    if not names:
        return list(DEFAULT_PROFILES)
    return sorted(names)


def active_profile() -> str:
    """Currently selected profile. Honors the ``POLYMARKET_ACTIVE_PROFILE``
    env override (process-local) first, then the shared `.active_profile`
    file. Falls back to the first available profile (or
    DEFAULT_ACTIVE_PROFILE) if neither names an existing profile."""
    env_name = os.getenv(ACTIVE_PROFILE_ENV, "").strip()
    if env_name and profile_dir(env_name).exists():
        return env_name
    if active_profile_file().exists():
        try:
            name = active_profile_file().read_text().strip()
        except OSError:
            name = ""
        if name and profile_dir(name).exists():
            return name
    available = list_profiles()
    if DEFAULT_ACTIVE_PROFILE in available:
        return DEFAULT_ACTIVE_PROFILE
    return available[0] if available else DEFAULT_ACTIVE_PROFILE


def set_active_profile(name: str) -> None:
    """Persist the active-profile selection. Creates the profile directory
    if it doesn't already exist."""
    profiles_root().mkdir(parents=True, exist_ok=True)
    profile_dir(name).mkdir(parents=True, exist_ok=True)
    active_profile_file().write_text(name)
    log.info("active profile → %s", name)


def create_profile(name: str) -> bool:
    """Create a new (empty) profile directory. Returns True if created,
    False if it already existed. Names must be filesystem-safe."""
    if not name or "/" in name or name.startswith(".") or name == SHARED_DIR_NAME:
        raise ValueError(f"Invalid profile name: {name!r}")
    d = profile_dir(name)
    if d.exists():
        return False
    d.mkdir(parents=True)
    log.info("created profile %s", name)
    return True


def reset_profile(name: str) -> int:
    """Wipe all data files in a profile directory. Returns the number of
    files deleted. The directory itself is preserved so the profile still
    appears in `list_profiles()`."""
    d = profile_dir(name)
    if not d.exists():
        return 0
    n = 0
    for f in d.iterdir():
        if f.is_file():
            f.unlink()
            n += 1
        elif f.is_dir():
            shutil.rmtree(f)
            n += 1
    log.info("reset profile %s (%d items removed)", name, n)
    return n


# ---------------------------------------------------------------------------
# Per-profile storage paths (consulted by data modules' lazy file lookups)
# ---------------------------------------------------------------------------

def file_path(kind: str, profile: str | None = None) -> Path:
    """Storage path for one file kind in a profile.

    `kind` must be one of `PROFILE_FILE_KINDS`. If `profile` is None, the
    currently active profile is used.

    >>> file_path("portfolio", "jeff").name
    'portfolio.json'
    """
    if kind not in PROFILE_FILE_KINDS:
        raise ValueError(f"Unknown profile file kind: {kind!r}")
    p = profile if profile is not None else active_profile()
    return profile_dir(p) / f"{kind}.json"


def shared_dir() -> Path:
    """The cross-profile artifacts directory. Mostly a placeholder for
    Tier 3b strategies + shared forecast library."""
    return profiles_root() / SHARED_DIR_NAME


# ---------------------------------------------------------------------------
# First-run initialisation and legacy migration
# ---------------------------------------------------------------------------

def initialize_profiles(project_root: Path | None = None) -> None:
    """Ensure the data/ directory structure exists and run a one-time
    migration of any legacy root-level state files into `data/jeff/`.

    Idempotent: safe to call on every app start. Logs INFO when it actually
    moves a file.
    """
    project_root = project_root or REPO_ROOT

    profiles_root().mkdir(parents=True, exist_ok=True)
    shared_dir().mkdir(parents=True, exist_ok=True)
    for name in DEFAULT_PROFILES:
        profile_dir(name).mkdir(parents=True, exist_ok=True)

    # Migrate any pre-profile-system state files into `jeff/`. We only move
    # if the target doesn't already exist — never overwrites real profile data.
    jeff_dir = profile_dir(DEFAULT_ACTIVE_PROFILE)
    for kind in PROFILE_FILE_KINDS:
        legacy = project_root / f"{kind}.json"
        target = jeff_dir / f"{kind}.json"
        if legacy.exists() and not target.exists():
            try:
                legacy.replace(target)
                log.info("Migrated legacy %s → %s", legacy.name, target)
            except OSError as e:
                log.warning("Could not migrate %s: %s", legacy.name, e)

    if not active_profile_file().exists():
        try:
            active_profile_file().write_text(DEFAULT_ACTIVE_PROFILE)
        except OSError as e:
            log.warning("Could not write active-profile file: %s", e)
