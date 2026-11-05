"""Root shim so `python -m live_gates` keeps working after the R-24 package split.

live_gates moved to `pm.trading.live_gates` on 2026-08-24. This file exists ONLY so the
documented operator command keeps working — it is referenced throughout the docs, and for `live_gates` in particular
the whole point is that operators run THIS command instead of an ad-hoc shell
check that resolves the gate chain differently (see the "live-gate state
depends on the asking process" gotcha). A stale invocation printing
"No module named live_gates" is exactly how someone ends up back at the ad-hoc
check, so the command surface is kept stable deliberately.

Shims are NOT the general pattern for the split — every other moved module was
updated at its call sites. These two are documented *operator* entry points,
which is a different thing from an import.

New code should import from `pm.trading.live_gates` directly.
"""

from pm.trading.live_gates import *  # noqa: F401,F403
from pm.trading.live_gates import main

if __name__ == "__main__":
    raise SystemExit(main())
