"""The lint/type-check gate must be the SAME gate everywhere it is invoked.

Why this file exists: ``.pre-commit-config.yaml`` carried its own copy of the
mypy target list as a regex of ROOT-level module names
(``^(polymarket_client|portfolio|...)\\.py$``). The R-24 package split moved
every one of those modules under ``pm/``, and because the pattern is anchored
at ``^`` it then matched exactly ONE path in the tree — ``live_gates.py``, a
root shim. For as long as the ``pm/`` layout has existed the local hook
reported success while type-checking 1 of 36 intended targets; CI kept its own
(correct) copy, so nothing ever surfaced the divergence.

Three copies of one list is the defect, not the regex. The list now lives
once, in ``[tool.mypy] files``, and every consumer invokes a bare ``mypy``.
These tests pin that arrangement: they fail if a target list is pasted back
into the Makefile, the workflow or the hook, and they fail if an entry in the
single list stops pointing at a real path.

Parsed as text rather than with PyYAML on purpose — the suite must run on
``requirements.txt`` alone, and a config guard is not worth a new dependency.
The ``check-yaml`` pre-commit hook already owns "is this valid YAML".
"""
from __future__ import annotations

import re
import tomllib

import pm

PYPROJECT = pm.REPO_ROOT / "pyproject.toml"
PRECOMMIT = pm.REPO_ROOT / ".pre-commit-config.yaml"
MAKEFILE = pm.REPO_ROOT / "Makefile"
CI_WORKFLOW = pm.REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _mypy_files() -> list[str]:
    cfg = tomllib.loads(PYPROJECT.read_text())["tool"]["mypy"]
    assert "files" in cfg, (
        "[tool.mypy] must declare `files` — it is the single source of truth "
        "for the type-check gate, and a bare `mypy` checks nothing useful "
        "without it")
    return cfg["files"]


def test_every_mypy_target_exists():
    files = _mypy_files()
    assert len(files) > 20, (
        "the mypy gate covers ~36 modules; a list this short means entries "
        "were dropped rather than deliberately narrowed")
    missing = [f for f in files if not (pm.REPO_ROOT / f).exists()]
    assert missing == [], (
        f"[tool.mypy] files names paths that do not exist: {missing} — a "
        "renamed or moved module silently leaves the type-check gate")


def test_mypy_targets_reach_the_pm_package():
    """The regression guard proper: the old regex matched only a root shim."""
    under_pm = [f for f in _mypy_files() if f.startswith("pm/")]
    assert len(under_pm) > 20, (
        "almost every gated module lives under pm/ — if few or none of the "
        "targets do, the list is pre-R-24 root-level names again")


def _make_recipe(target: str) -> list[str]:
    """The command lines of one Makefile target."""
    out: list[str] = []
    seen = False
    for line in MAKEFILE.read_text().splitlines():
        if line.startswith(f"{target}:"):
            seen = True
            continue
        if seen:
            if line.startswith("\t"):
                out.append(line.strip())
            elif line.strip() and not line.startswith("#"):
                break
    assert seen, f"no {target!r} target in the Makefile"
    return out


def test_makefile_typecheck_invokes_bare_mypy():
    assert _make_recipe("typecheck") == ["mypy"], (
        "`make typecheck` must invoke a bare `mypy` so it inherits "
        "[tool.mypy] files instead of carrying a second copy of the list")


def test_ci_typecheck_step_invokes_bare_mypy():
    # The `- name: mypy` step's `run:` value, however it is indented.
    m = re.search(r"-\s*name:\s*mypy\s*\n(?:\s*#.*\n)*\s*run:\s*(.+)",
                  CI_WORKFLOW.read_text())
    assert m, "no `- name: mypy` step with a `run:` in the CI workflow"
    assert m.group(1).strip() == "mypy", (
        f"the CI mypy step must run a bare `mypy`, got {m.group(1).strip()!r} "
        "— a pasted target list is how this gate drifted from the hook before")


def _precommit_mypy_hook() -> str:
    """The text of the `- id: mypy` hook entry, up to the next hook or repo."""
    text = PRECOMMIT.read_text()
    start = text.index("- id: mypy")
    rest = text[start + 1:]
    ends = [i for i in (rest.find("\n      - id:"), rest.find("\n  - repo:"))
            if i != -1]
    return rest[:min(ends)] if ends else rest


def test_precommit_mypy_hook_defers_to_the_shared_list():
    assert PRECOMMIT.read_text().count("- id: mypy") == 1, \
        "expected exactly one mypy hook"
    hook = _precommit_mypy_hook()
    assert re.search(r"^\s*pass_filenames:\s*false\s*$", hook, re.M), (
        "the mypy hook must set `pass_filenames: false` so it type-checks the "
        "[tool.mypy] files list rather than whatever happens to be staged")
    for key in ("files", "exclude", "args"):
        assert not re.search(rf"^\s*{key}:", hook, re.M), (
            f"no `{key}:` on the mypy hook — a path filter or path argument "
            "here overrides [tool.mypy] files and reintroduces a second "
            "target list, which is exactly what silently reduced this gate "
            "to one root shim after the R-24 move")


def test_ruff_is_pinned_identically_in_ci_and_pre_commit():
    """Already true, and worth pinning: an unpinned CI ruff drifted onto newer
    rule sets than the hook, so the two disagreed about what 'clean' meant."""
    pc = PRECOMMIT.read_text()
    hook_rev = re.search(
        r"ruff-pre-commit\s*\n\s*rev:\s*(\S+)", pc)
    assert hook_rev, "no pinned rev on the ruff-pre-commit repo"
    ci_pin = re.search(r"pip install ruff==(\S+)", CI_WORKFLOW.read_text())
    assert ci_pin, "CI no longer pins a ruff version"
    assert hook_rev.group(1).lstrip("v") == ci_pin.group(1), (
        f"ruff pinned to {ci_pin.group(1)} in CI but {hook_rev.group(1)} in "
        "pre-commit")


# ---------------------------------------------------------------------------
# Public-licence / private-distribution metadata.
#
# These two facts read as a contradiction if you meet them cold — an
# independent review flagged exactly that — so they are pinned together here,
# with the reasoning in the pyproject.toml comment above `classifiers`:
# Apache-2.0 is the LICENCE (public, redistributable), `Private :: Do Not
# Upload` is the DISTRIBUTION policy (not on PyPI). Neither constrains the
# other, and a future edit that drops one without the other should fail.
# ---------------------------------------------------------------------------

def _project() -> dict:
    return tomllib.loads(PYPROJECT.read_text())["project"]


def test_apache_licence_is_declared_consistently():
    proj = _project()
    assert proj["license"] == {"text": "Apache-2.0"}
    assert "License :: OSI Approved :: Apache Software License" in \
        proj["classifiers"]
    licence = (pm.REPO_ROOT / "LICENSE").read_text()
    assert "Apache License" in licence and "Version 2.0" in licence
    assert (pm.REPO_ROOT / "NOTICE").exists(), \
        "Apache-2.0 §4(d): keep the NOTICE file"
    readme = (pm.REPO_ROOT / "README.md").read_text()
    assert "Apache License, Version 2.0" in readme, \
        "the README must state the licence a reader is being granted"


def test_do_not_upload_guard_is_intact():
    assert "Private :: Do Not Upload" in _project()["classifiers"], (
        "the upload tripwire is gone. It is an UNREGISTERED trove classifier "
        "on purpose: PyPI rejects unrecognised classifiers, so an accidental "
        "`twine upload` fails at the index. Remove it only when deliberately "
        "setting up a real release — and then say so in the README, which "
        "currently tells readers there is no pip install for this project.")


def test_project_is_not_a_build_target():
    """The primary guard: no build backend, so no artifact can be produced.

    Verified behaviour, not theory — `python -m build --wheel` fails in
    setuptools' flat-layout package discovery ("Multiple top-level packages
    discovered in a flat-layout: ['pm', 'wnba', 'edges', ...]") before the
    classifiers are ever read. Adding a `[build-system]` table or
    package-discovery config makes this repo publishable and defeats that, so
    it should be a deliberate act with the README updated to match.
    """
    cfg = tomllib.loads(PYPROJECT.read_text())
    assert "build-system" not in cfg, (
        "a [build-system] table makes this project buildable and therefore "
        "publishable — see this test's docstring before adding one")
    assert "setuptools" not in cfg.get("tool", {}), \
        "no package-discovery config: see this test's docstring"
    readme = (pm.REPO_ROOT / "README.md").read_text()
    assert "PyPI" in readme, \
        "the README must tell readers this is not a pip-installable package"
