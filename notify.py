"""Root shim so `python -m notify` keeps working after the R-24 package split.

notify moved to `pm.core.notify` on 2026-08-24 (Phase B). This shim exists because
polymarket-alert@.service runs `python -m notify --unit %i` as the OnFailure hook for EVERY daemon — a broken alert path fails silently, which is the worst failure mode an alerting system can have.

Shims are NOT the general pattern for the split — these are operational
entry points, which is a different thing from an import. New code should
import from `pm.core.notify` directly. The systemd unit path migrates in Phase C.
"""

from pm.core.notify import *  # noqa: F401,F403
from pm.core.notify import main

if __name__ == "__main__":
    raise SystemExit(main())
