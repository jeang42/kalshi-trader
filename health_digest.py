"""Root shim so `python health_digest.py --print` keeps working after R-24.

health_digest moved to `pm/ops/health_digest` on 2026-08-24 (Phase C). This
shim exists because `python health_digest.py --print` is one of the four
standard verification commands — the commands run from
muscle memory before anything else on a return, precisely when a "No module
named" error would be most misleading. The systemd timer unit was migrated to
`-m pm.ops.health_digest` in the same phase; this file serves the human, not
the fleet.

New code should import from `pm.ops.health_digest` directly.
"""

from pm.ops.health_digest import *  # noqa: F401,F403
from pm.ops.health_digest import main

if __name__ == "__main__":
    raise SystemExit(main())
