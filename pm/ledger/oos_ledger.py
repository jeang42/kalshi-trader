"""Out-of-sample bookkeeping — separating evidence from the data that made it.

The problem this exists to prevent
----------------------------------
Twice, an arm that was losing was made profitable by excluding the buckets
that were losing, using that arm's own live data. Both exclusions have
mechanistic stories and both are documented honestly. But the effect is that
each surviving arm's headline P&L is **in-sample with respect to its own
filters**: the rules were chosen partly to fit the very trades being counted.

Nothing in the system recorded that. ``--stats`` reported one cumulative
number spanning the period before a filter existed and the period after, and
there was no way to tell them apart without remembering, per arm, which commit
changed what and when. Memory is not a control.

What this does
--------------
1. **Fingerprints an arm's decision parameters.** Any change to the values an
   arm uses to decide — bands, thresholds, exclusions, caps — changes the hash.
2. **Records when the fingerprint last changed** (the "rules-frozen" date).
3. **Splits P&L at that date.** Positions opened before it are in-sample with
   respect to the current rules; positions opened after are genuine
   out-of-sample evidence.

A parameter edit therefore **resets the evidence clock automatically**, and
says so. That is the point: re-fitting is legitimate — the mistake is
re-fitting and then continuing to count the old trades as if the new rules had
predicted them.

This is bookkeeping, not judgement. It will not tell you a strategy is good.
It tells you how much of what you are looking at actually tested the rules you
are running now, which is the number that was missing.

Usage::

    # in an arm, once per cycle (cheap; writes only when the hash changes)
    oos_ledger.record_params("fade_nearclose", {"band_lo": 0.10, ...})

    python -m oos_ledger --status
    python -m oos_ledger --split fade_nearclose
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pm.core import atomic_io, provenance

DEFAULT_LEDGER = "data/shared/oos_ledger.json"


def ledger_path() -> Path:
    return Path(os.getenv("OOS_LEDGER_FILE", DEFAULT_LEDGER))


def fingerprint(params: dict) -> str:
    """Stable hash of a decision-parameter set.

    Sorted keys and a canonical separator so the hash reflects the *values*,
    not dict ordering or formatting. Floats are normalised via repr so 0.10
    and 0.1 do not read as different rules.

    >>> fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    True
    >>> fingerprint({"a": 0.10}) == fingerprint({"a": 0.1})
    True
    >>> fingerprint({"a": 1}) == fingerprint({"a": 2})
    False
    """
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"),
                           default=repr)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass
class ArmRules:
    """What we know about one arm's rule history."""

    arm: str
    fingerprint: str
    frozen_at: str              # ISO8601 — when these rules took effect
    params: dict
    previous_fingerprint: str = ""
    change_count: int = 0

    @property
    def frozen_dt(self) -> datetime:
        return datetime.fromisoformat(self.frozen_at)

    def days_of_evidence(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        return max(0.0, (now - self.frozen_dt).total_seconds() / 86400.0)


def _load() -> dict:
    return atomic_io.read_json(ledger_path(), None) or {"arms": {}}


def freeze_at(arm: str, when: datetime, *, reason: str = "") -> ArmRules | None:
    """Backdate an arm's rules-frozen boundary to a known date.

    ``record_params`` can only start the clock when it first sees an arm, which
    understates the evidence for rules that have in fact been unchanged for
    weeks. The truthful boundary is the date the selection parameters last
    actually changed — recoverable from ``git log -S`` on the constants.

    Use this once, deliberately, with a date you can point at a commit for.
    Moving a boundary EARLIER than the real change date would count trades as
    out-of-sample that the rules were fitted to, which is the exact error this
    module exists to prevent.
    """
    path = ledger_path()
    with atomic_io.locked(path):
        data = atomic_io.read_json(path, None) or {"arms": {}}
        entry = data.get("arms", {}).get(arm)
        if not entry:
            return None
        entry["frozen_at"] = when.isoformat()
        entry["frozen_reason"] = reason
        atomic_io.write_json(path, data)
    return get_rules(arm)


def record_params(arm: str, params: dict, *, now: datetime | None = None) -> ArmRules:
    """Register an arm's current decision parameters.

    Cheap to call every cycle: it writes only when the fingerprint actually
    changes. When it does change, the rules-frozen date resets to now — the
    evidence clock restarts, because the trades before this moment did not
    test these rules.
    """
    now = now or datetime.now(UTC)
    fp = fingerprint(params)
    path = ledger_path()
    with atomic_io.locked(path):
        data = atomic_io.read_json(path, None) or {"arms": {}}
        entry = data.get("arms", {}).get(arm)
        if entry and entry.get("fingerprint") == fp:
            return ArmRules(arm=arm, fingerprint=fp,
                            frozen_at=entry["frozen_at"], params=params,
                            previous_fingerprint=entry.get("previous_fingerprint", ""),
                            change_count=int(entry.get("change_count", 0)))
        rules = ArmRules(
            arm=arm, fingerprint=fp, frozen_at=now.isoformat(), params=params,
            previous_fingerprint=(entry or {}).get("fingerprint", ""),
            change_count=int((entry or {}).get("change_count", 0)) + (1 if entry else 0),
        )
        data.setdefault("arms", {})[arm] = {
            "fingerprint": rules.fingerprint,
            "frozen_at": rules.frozen_at,
            "params": params,
            "previous_fingerprint": rules.previous_fingerprint,
            "change_count": rules.change_count,
        }
        atomic_io.write_json(path, data)
    return rules


def get_rules(arm: str) -> ArmRules | None:
    entry = _load().get("arms", {}).get(arm)
    if not entry:
        return None
    return ArmRules(arm=arm, fingerprint=entry["fingerprint"],
                    frozen_at=entry["frozen_at"], params=entry.get("params", {}),
                    previous_fingerprint=entry.get("previous_fingerprint", ""),
                    change_count=int(entry.get("change_count", 0)))


def all_arms() -> list[ArmRules]:
    return sorted((get_rules(a) for a in _load().get("arms", {})),
                  key=lambda r: r.arm)  # type: ignore[union-attr,arg-type]


@dataclass
class Split:
    """P&L either side of the rules-frozen boundary."""

    arm: str
    frozen_at: str
    in_sample_n: int = 0
    in_sample_pnl: float = 0.0
    out_of_sample_n: int = 0
    out_of_sample_pnl: float = 0.0
    #: Independent clusters (distinct events) on the out-of-sample side. This,
    #: not the position count, is the honest sample size: these arms cap
    #: themselves at one position per event precisely because members of one
    #: event resolve together, so N positions across K events is K
    #: observations, not N.
    out_of_sample_clusters: int = 0
    in_sample_clusters: int = 0

    @property
    def oos_mean(self) -> float | None:
        if not self.out_of_sample_n:
            return None
        return self.out_of_sample_pnl / self.out_of_sample_n

    @property
    def oos_mean_per_cluster(self) -> float | None:
        """Mean P&L per INDEPENDENT observation — the number to judge on."""
        if not self.out_of_sample_clusters:
            return None
        return self.out_of_sample_pnl / self.out_of_sample_clusters

    @property
    def verdict(self) -> str:
        """A deliberately conservative reading.

        Sample-size thresholds are not a statistical test — they are a guard
        against reading a number that cannot mean anything yet. Judge on mean
        outcome per independent cluster, never on win rate; these arms win
        80-95% of trades while two of them lose money.
        """
        if self.out_of_sample_n == 0:
            return "NO OOS EVIDENCE — every trade predates the current rules"
        # Judge on independent clusters where we know them, not positions.
        n = self.out_of_sample_clusters or self.out_of_sample_n
        unit = "events" if self.out_of_sample_clusters else "trades (events unknown)"
        if n < 30:
            return f"TOO EARLY — {n} OOS {unit}, need far more"
        if self.out_of_sample_pnl > 0:
            return f"positive OOS over {n} {unit} — encouraging, not yet a result"
        return f"NEGATIVE OOS over {n} {unit}"


def split_positions(arm: str, positions: list[dict], *,
                    opened_key: str = "opened_at",
                    pnl_key: str = "pnl",
                    event_key: str | None = "event_id") -> Split | None:
    """Split resolved positions at the rules-frozen boundary.

    ``positions`` is any sequence of dicts carrying an open timestamp (epoch
    seconds or ISO) and a realized P&L. Kept generic so each arm can pass its
    own rows without this module knowing any arm's schema.
    """
    rules = get_rules(arm)
    if rules is None:
        return None
    boundary = rules.frozen_dt
    split = Split(arm=arm, frozen_at=rules.frozen_at)
    is_events: set = set()
    oos_events: set = set()
    for p in positions:
        pnl = p.get(pnl_key)
        if pnl is None:
            continue                      # unresolved: not evidence either way
        raw = p.get(opened_key)
        opened = _as_dt(raw)
        if opened is None:
            continue
        ev = (p.get(event_key) if event_key else None) or None
        if opened < boundary:
            split.in_sample_n += 1
            split.in_sample_pnl += float(pnl)
            if ev:
                is_events.add(ev)
        else:
            split.out_of_sample_n += 1
            split.out_of_sample_pnl += float(pnl)
            if ev:
                oos_events.add(ev)
    split.in_sample_clusters = len(is_events)
    split.out_of_sample_clusters = len(oos_events)
    return split


def _as_dt(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, int | float):
        try:
            return datetime.fromtimestamp(float(raw), UTC)
        except (ValueError, OSError):
            return None
    try:
        dt = datetime.fromisoformat(str(raw))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def format_status(arms: list[ArmRules]) -> str:
    if not arms:
        return ("No arms registered yet. An arm registers by calling\n"
                "  oos_ledger.record_params(\"<arm>\", {...decision params...})\n"
                "once per cycle.")
    lines = ["  arm                     frozen (UTC)         days   edits  fingerprint",
             "  " + "-" * 74]
    for r in arms:
        lines.append(f"  {r.arm:<22}  {r.frozen_at[:19]}  {r.days_of_evidence():6.1f}"
                     f"  {r.change_count:5d}  {r.fingerprint}")
    lines.append("")
    lines.append("  'days' is how long the CURRENT rules have been running. Any change to")
    lines.append("  an arm's decision parameters resets it — trades before that point did")
    lines.append("  not test the rules now in force.")
    lines.append(f"  (read at code {provenance.label()})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true", help="show every arm's rule age")
    ap.add_argument("--split", metavar="ARM", help="split one arm's P&L at the boundary")
    ap.add_argument("--freeze-at", nargs=3, metavar=("ARM", "ISO_DATE", "REASON"),
                    help="backdate an arm's boundary to a date you can point at a commit for")
    args = ap.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if args.freeze_at:
        arm, iso, reason = args.freeze_at
        when = datetime.fromisoformat(iso)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        r = freeze_at(arm, when, reason=reason)
        if r is None:
            print(f"  {arm!r} has not registered any parameters yet.")
            return 1
        print(f"  {arm}: boundary set to {r.frozen_at[:19]} ({reason})")
        return 0

    if args.split:
        rules = get_rules(args.split)
        if rules is None:
            print(f"  {args.split!r} has not registered any parameters yet.")
            return 1
        print(f"  {args.split}: rules frozen {rules.frozen_at[:19]} "
              f"({rules.days_of_evidence():.1f} days, {rules.change_count} edit(s))")
        print(f"  fingerprint {rules.fingerprint}")
        print("\n  Pass this arm's resolved positions through split_positions() to get")
        print("  the in-sample / out-of-sample breakdown; each arm owns its own schema.")
        return 0

    print(format_status(all_arms()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
