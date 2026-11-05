"""Polymarket analyzer packages (P3/R-24).

REPO_ROOT is the one place the repository root is derived. Modules that used
to live at the root and anchored file defaults with `Path(__file__).parent`
kept working by accident of location; the R-24 move broke that silently for
`emergency_stop` (the kill-switch flag) and `profiles` (the per-profile data
tree) — the flag path became `pm/core/data/...` and nothing crashed, it just
looked in the wrong place. Anchor on REPO_ROOT instead; it stays correct at
any package depth, and `tests/test_pm_layout.py` pins the contract.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
