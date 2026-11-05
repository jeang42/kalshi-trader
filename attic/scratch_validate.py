"""Validate the simulator: reconstruct the ACTUAL close-game consensus
entry rule and compare replayed P&L to the real exits.jsonl."""
import json

import scratch_crunch as C
import scratch_sim as S

d = C.load()
out = open("/tmp/validate.txt", "w")
def p(*a): print(*a, file=out)

# --- Actual close-game consensus entry, as the daemon does it ---
# Entry: first snapshot where, gating on consensus forecast,
#   inning in 1..3, not blowout, ask>=0.25 (floor era) OR any (pre-floor),
#   consensus in [0.40,0.60], edge = fc - ask >= 0.05, ask>=... .
# We approximate the daemon's "first qualifying cycle" entry.
def cg_entry(min_ask):
    def fn(rows):
        for i, r in enumerate(rows):
            inn = r.get("inning") or 0
            if not (1 <= inn <= 3):
                continue
            if r.get("blowout"):
                continue
            ask = r.get("ask")
            fc = r.get("fc_consensus")
            if ask is None or ask <= 0 or fc is None:
                continue
            if ask < min_ask:
                continue
            if not (0.40 <= fc <= 0.60):
                continue
            if fc - ask >= 0.05:
                return i
        return None
    return fn

# Exit: scalp +25% on bid, else hold to resolution (flip off in close_game)
exit_scalp = S.make_exit(tp=0.25)

for label, floor in [("consensus pre-floor (min_ask=0)", 0.0),
                     ("consensus floor (min_ask=0.25)", 0.25)]:
    tr = S.simulate(d, cg_entry(floor), exit_scalp)
    p(S.fmt(label, S.metrics(tr)))

# --- Compare to REAL exits ---
p("")
p("--- ACTUAL exits.jsonl (consensus arm, all-time) ---")
rows = [json.loads(line) for line in open("data/agentic_closegame_consensus/exits.jsonl")]
net = sum(r["pnl_dollars"] for r in rows)
wins = sum(1 for r in rows if r["pnl_dollars"] > 0)
p(f"  real consensus: n={len(rows)} net=${net:.2f} win={wins/len(rows):.0%}")
by = {}
for r in rows:
    er = r["exit_reason"]
    b = by.setdefault(er, [0, 0.0])
    b[0]+=1
    b[1]+=r["pnl_dollars"]
for er,(n,v) in sorted(by.items()):
    p(f"    {er}: n={n} ${v:.2f}")

# How many of the real exit tickers do we have trajectories for?
real_t = {r["condition_id"] for r in rows}
have = sum(1 for t in real_t if t in d.traj)
p(f"  real tickers with observer trajectory: {have}/{len(real_t)}")
out.close()
print("wrote /tmp/validate.txt")
