"""Full counterfactual sweep. Ranks strategies on the gathered data.
Writes /tmp/scenarios.txt."""
from __future__ import annotations

import scratch_crunch as C
import scratch_sim as S

d = C.load()
OUT = open("/tmp/scenarios.txt", "w")
def p(*a): print(*a, file=OUT)

results = []  # (name, metrics)
def run(name, entry, exit_):
    tr = S.simulate(d, entry, exit_)
    m = S.metrics(tr)
    results.append((name, m))
    return m

# ── A. FORECAST QUALITY (Brier on pre-game forecast vs outcome) ───────────
p("="*70)
p("A. FORECAST QUALITY — Brier (lower=better), per source")
p("="*70)
def brier(source):
    # take each side's FIRST snapshot forecast (pre/early game) vs outcome
    terms = []
    for t, rows in d.traj.items():
        if t not in d.won:
            continue
        fc = None
        for r in rows:
            v = r.get(f"fc_{source}")
            if v is not None:
                fc = v
                break
        if fc is None:
            continue
        actual = 1.0 if d.won[t] else 0.0
        terms.append((fc - actual) ** 2)
    if not terms:
        return None, 0
    return sum(terms)/len(terms), len(terms)
for src in ("baseball", "deep", "consensus"):
    b, n = brier(src)
    p(f"  {src:10s} Brier={b:.4f}  n={n}" if b is not None else f"  {src}: none")
p("  (reference: always-0.5 Brier = 0.2500; perfect = 0)")

# ── entry builders ────────────────────────────────────────────────────────
def cg_entry(source, *, lo=0.40, hi=0.60, edge=0.05, min_ask=0.0,
            inn_max=3, both_agree=False, side="under"):
    """Close-game entry gating on `source`. side='under' buys the
    underpriced side (ask below forecast); 'over' would buy the dearer side."""
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
            if both_agree:
                others = [r.get(f"fc_{s}") for s in ("baseball","deep","consensus")
                          if s != source]
                if any(o is None or not (lo <= o <= hi) for o in others):
                    continue
            e = fc - ask
            if side == "under" and e >= edge:
                return i
            if side == "over" and (ask - fc) >= edge:
                return i
        return None
    return fn

# ── B. EXIT-RULE COUNTERFACTUALS on the consensus close-game entry ────────
p("")
p("="*70)
p("B. EXIT RULES on consensus close-game entry (min_ask=0.25)")
p("="*70)
base = cg_entry("consensus", min_ask=0.25)
run("  hold-to-resolution (no scalp)", base, S.exit_hold)
for tp in (0.10, 0.15, 0.25, 0.40):
    run(f"  TP +{int(tp*100)}% only", base, S.make_exit(tp=tp))
for tp, sl in [(0.25,0.40),(0.25,0.25),(0.25,0.15),(0.15,0.15),(0.15,0.25)]:
    run(f"  TP +{int(tp*100)}% / SL -{int(sl*100)}%", base, S.make_exit(tp=tp, sl=sl))
run("  TP +25% / time-stop inn7", base, S.make_exit(tp=0.25, time_inning=7))
run("  TP +25% / SL -25% / time inn7", base, S.make_exit(tp=0.25, sl=0.25, time_inning=7))
run("  ORACLE peak (upper bound)", base, S.exit_oracle_peak)

# ── C. ENTRY COUNTERFACTUALS (which source to gate on) ────────────────────
p("")
p("="*70)
p("C. ENTRY GATE — source comparison (best exit so far: TP25/SL25)")
p("="*70)
ex = S.make_exit(tp=0.25, sl=0.25)
for src in ("baseball","deep","consensus"):
    run(f"  gate={src} TP25/SL25", cg_entry(src, min_ask=0.25), ex)
run("  gate=consensus BOTH-AGREE TP25/SL25",
    cg_entry("consensus", min_ask=0.25, both_agree=True), ex)
# edge size sensitivity
for edge in (0.05, 0.10, 0.15):
    run(f"  consensus edge>={edge} TP25/SL25",
        cg_entry("consensus", min_ask=0.25, edge=edge), ex)

# ── D. NEW IDEAS ──────────────────────────────────────────────────────────
p("")
p("="*70)
p("D. NEW IDEAS")
p("="*70)
# D1: buy the OVERPRICED (favorite/dearer) side instead — fade the model
run("  D1 buy DEARER side (fade model) TP25/SL25",
    cg_entry("consensus", min_ask=0.25, side="over"), ex)
# D2: hold the underpriced side to resolution, no scalp (pure value bet)
run("  D2 underpriced HOLD-to-resolution",
    cg_entry("consensus", min_ask=0.25), S.exit_hold)
# D3: widen band - any forecast, just need a price-vs-forecast gap >=10pp
run("  D3 any-band edge>=0.10 TP25/SL25",
    cg_entry("consensus", lo=0.0, hi=1.0, edge=0.10, min_ask=0.25), ex)
# D4: favorites only (forecast 0.55-0.70), buy them, hold
run("  D4 favorites 0.55-0.70 HOLD",
    cg_entry("consensus", lo=0.55, hi=0.70, edge=0.0, min_ask=0.0), S.exit_hold)
# D5: longshot rescue — can ANY exit save the longshot entry? hold vs scalp
def ls_entry(rows):
    for i, r in enumerate(rows):
        inn = r.get("inning") or 0
        if not (4 <= inn <= 7):
            continue
        if r.get("blowout"):
            continue
        ask = r.get("ask")
        if ask is None:
            continue
        if 0.10 <= ask <= 0.35:
            return i
    return None
run("  D5 longshot entry HOLD-to-res", ls_entry, S.exit_hold)
run("  D5 longshot entry TP25 only", ls_entry, S.make_exit(tp=0.25))
run("  D5 longshot entry TP25/SL25", ls_entry, S.make_exit(tp=0.25, sl=0.25))
run("  D5 longshot ORACLE peak", ls_entry, S.exit_oracle_peak)

# ── RANKING ───────────────────────────────────────────────────────────────
p("")
p("="*70)
p("RANKED (by sharpe/trade, then net) — n>=8 only")
p("="*70)
rank = [(nm,m) for nm,m in results if m.get("n",0) >= 8]
rank.sort(key=lambda x: (x[1].get("sharpe_per_trade",-9), x[1].get("net",-9e9)), reverse=True)
for nm, m in rank:
    p(S.fmt(nm.strip(), m))

OUT.close()
print("wrote /tmp/scenarios.txt")
