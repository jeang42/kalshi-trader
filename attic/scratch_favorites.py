"""Deep-dive the one green thread: buying the favorite side and holding.
Tests robustness across bands, edge requirements, source, and timing.
Writes /tmp/fav.txt."""
from __future__ import annotations

import scratch_crunch as C
import scratch_sim as S

d = C.load()
OUT = open("/tmp/fav.txt", "w")
def p(*a): print(*a, file=OUT)

def fav_entry(source, *, lo, hi, edge=0.0, inn_max=3, min_ask=0.0):
    """Buy a side whose forecast is in [lo,hi] (a favorite band), optionally
    requiring it to be underpriced by `edge` (ask <= fc-edge), early innings."""
    def fn(rows):
        for i, r in enumerate(rows):
            inn = r.get("inning") or 0
            if not (1 <= inn <= inn_max):
                continue
            if r.get("blowout"):
                continue
            ask = r.get("ask")
            fc = r.get(f"fc_{source}")
            if ask is None or ask <= 0 or fc is None or ask < min_ask:
                continue
            if not (lo <= fc <= hi):
                continue
            if (fc - ask) >= edge:
                return i
        return None
    return fn

results = []
def run(name, entry, exit_):
    tr = S.simulate(d, entry, exit_)
    m = S.metrics(tr)
    results.append((name, m))
    return m

p("="*72)
p("FAVORITE-DIRECTION DEEP DIVE (hold-to-resolution unless noted)")
p("="*72)

# Bands, each source, hold
for src in ("consensus","deep","baseball"):
    p(f"-- source={src} --")
    for lo,hi in [(0.50,0.60),(0.55,0.65),(0.55,0.70),(0.60,0.70),(0.60,0.80),(0.55,0.80)]:
        run(f"  fav {lo:.2f}-{hi:.2f} {src} HOLD", fav_entry(src,lo=lo,hi=hi), S.exit_hold)

p("")
p("-- require underpricing (ask <= fc - edge), consensus, HOLD --")
for lo,hi in [(0.55,0.70),(0.55,0.80)]:
    for edge in (0.03,0.05,0.08):
        run(f"  fav {lo:.2f}-{hi:.2f} edge>={edge} HOLD",
            fav_entry("consensus",lo=lo,hi=hi,edge=edge), S.exit_hold)

p("")
p("-- favorites with a SCALP exit instead of hold (consensus 0.55-0.70) --")
run("  fav 0.55-0.70 TP+15%", fav_entry("consensus",lo=0.55,hi=0.70), S.make_exit(tp=0.15))
run("  fav 0.55-0.70 TP+25%", fav_entry("consensus",lo=0.55,hi=0.70), S.make_exit(tp=0.25))
run("  fav 0.55-0.70 TP25/SL25", fav_entry("consensus",lo=0.55,hi=0.70), S.make_exit(tp=0.25,sl=0.25))

p("")
p("-- calibration check: actual win rate of favorite bands (consensus) --")
def winrate_band(source, lo, hi):
    w=n=0
    for t,rows in d.traj.items():
        if t not in d.won:
            continue
        fc=None
        for r in rows:
            v=r.get(f"fc_{source}")
            if v is not None:
                fc=v
                break
        if fc is None or not(lo<=fc<=hi):
            continue
        n+=1
        w+=1 if d.won[t] else 0
    return w,n
for lo,hi in [(0.50,0.60),(0.55,0.65),(0.60,0.70),(0.55,0.70)]:
    w,n = winrate_band("consensus",lo,hi)
    mid=(lo+hi)/2
    p(f"  band {lo:.2f}-{hi:.2f}: actual_win={w}/{n}={w/n:.2f} (forecast mid {mid:.2f})" if n else f"  band {lo}-{hi}: n=0")

p("")
p("="*72)
p("RANKED favorites (sharpe, n>=10)")
p("="*72)
rk=[(nm,m) for nm,m in results if m.get("n",0)>=10]
rk.sort(key=lambda x:(x[1]["sharpe_per_trade"],x[1]["net"]),reverse=True)
for nm,m in rk:
    p(S.fmt(nm.strip(),m))

OUT.close()
print("wrote /tmp/fav.txt")
