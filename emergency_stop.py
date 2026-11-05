"""Root shim so `python -m emergency_stop` keeps working after the R-24 package split.

emergency_stop moved to `pm.core.emergency_stop` on 2026-08-24 (Phase B). This shim exists because
this is the documented kill switch (`python -m emergency_stop`, 8 doc references) — the one command that must work DURING an incident, when nobody is reading refactor notes.

Shims are NOT the general pattern for the split — these are operational
entry points, which is a different thing from an import. New code should
import from `pm.core.emergency_stop` directly. The systemd unit path migrates in Phase C.
"""

from pm.core.emergency_stop import *  # noqa: F401,F403
from pm.core.emergency_stop import main

if __name__ == "__main__":
    raise SystemExit(main())
