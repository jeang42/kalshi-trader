# Strategy Scoreboard — every attempt, caveat-adjusted

*As of 2026-07-02. "Raw result" = the headline number it first showed. "Adjusted
verdict" = what it actually is after the honesty controls (executable prices,
fees, de-correlation by event, captured-edge vs mid-illusion, hindsight checks).
The gap between those two columns is the whole story of this project.*

**Bottom line: ~20 attempts, all dead or unproven except ONE — the retail-longshot
fade — which is now in out-of-sample paper validation. Every dead one died the
same way: the raw number was a mirage that vanished under a proper cost/variance
control.**

---

## 1. Static / prediction edges — all dead (market is efficient)

| Attempt | Raw result | Caveat-adjusted verdict | Status |
|---|---|---|---|
| MLB game-winner model (Pythagorean+) | Brier ~0.26 | Worse than the market price; model = market at best | ❌ dead |
| Favorite-hold (buy 0.55–0.70 favs, hold) | −$— / 165 / 55% win | No edge — favorites win *at* their price; market calibrated | ❌ retired |
| Close-game scalp | net negative | Efficient market; static price edge absent | ❌ retired |
| Longshot scalp / flip | ~−$— / 67 | Cheap-underdog *entry* is net-negative | ❌ retired |
| Favorite-longshot bias (sports, near-close) | "bias present at 24h" | NOT tradeable near-close — every band net-neg at executable prices | ❌ dead |
| LLM forecasting (deep analysis) | — | Loses to the market head-to-head (776 forecasts, every model worse) | ❌ dead |

## 2. Speed / reaction edges — the "one signal" that turned out to be a mirage

| Attempt | Raw result | Caveat-adjusted verdict | Status |
|---|---|---|---|
| In-game reaction scalp (MLB) | +$— paper (1 slugfest) | Fat-tail variance; live −$— (whipsaw + double cost) | ❌ dead |
| **Reaction-AND-hold (MLB)** | **+$— / 51 / 69% win** | **NO EDGE** — captured-edge test (2,488 shocks): the ask already reflects the play; −0.4¢/contract gross, −0.9¢ net. Win rate is the favorite-payoff illusion; P&L is fat-tail variance | ❌ falsified, paused |
| Latency reduction ("be faster") | — | Moot — faster = *worse* (ask spikes then relaxes); no edge to capture net of spread | ❌ reversed |
| Settlement-timing "arb" | +11¢/$—k (!) | Artifact — 76% stale/halted books + a last-play≠final-out proxy + hindsight; score-reconstruction killed it | ❌ dead |
| Crypto latency (Kalshi book vs Coinbase) | corr 0.37 contemporaneous | ~0 residual lag; HFT-saturated, moves lockstep | ❌ dead |
| News-reaction speed (free RSS) | — | Detection lag ~150 min — far too slow for liquid markets | ❌ dead |
| Tennis reaction lag | flat curve | +0.004¢ move vs ~1¢ spread; book reprices before we detect | ❌ dead |
| WNBA reaction lag | weak | Medians 0, means <0.6¢, fat-tail-driven; not clear | ⚠️ inconclusive |

## 3. Microstructure / market-making — both sides of the spread lose

| Attempt | Raw result | Caveat-adjusted verdict | Status |
|---|---|---|---|
| Market-making (blind, quote ±q) | gross +1–3¢ spread | −3¢/fill net — adverse selection > spread; toxic flow (worse at wider q) | ❌ dead |
| Market-making (feed-protected) | quiet-time fills | Still −2¢/fill — toxicity everywhere, not just around plays | ❌ dead |
| Fade the in-game overreaction | — | Swings STICK (don't revert); fade loses, worst when contradicting the favorite | ❌ dead |

## 4. Arb / cross-market — efficient venue

| Attempt | Raw result | Caveat-adjusted verdict | Status |
|---|---|---|---|
| Cross-market arbitrage (one-winner fields) | candidates flagged | No real arb across 3,000 markets (executable bid/ask + fees); all incomplete-field artifacts | ❌ dead |
| Cross-venue lead-lag (Kalshi↔Polymarket) | — | Overlap only in slow futures/politics; no fast shared markets to exploit | ❌ not viable |
| Correlated-market lag (field members) | — | Not testable — no multi-member field book data | ⚪ untested |

## 5. Favourite-longshot fades — the backtest that failed forward ⛔

| Attempt | Raw result | Caveat-adjusted verdict | Status |
|---|---|---|---|
| **Retail-longshot fade** (non-sport/crypto: primaries, awards, music, mentions — arbitrageur-light) | backtest +17.6% ROI, longshot gap −8 to −19pp | Survived every *backtest* control (+11.8% ROI net of a 4¢ spread, 86% win by independent event, top-1-event 15%, documented mechanism) — then **lost forward**: 200 OOS resolutions, **−$—**, mean −$—/position | ⛔ **retired 2026-08-23 → `attic/`** |
| ↳ Prop-overround sub-cut (Drake features / Met Gala / "will X say Y") | backtest +34–64% ROI | The +64% was ~2 correlated events. Forward: 224 resolutions, **−$—**, mean −$0.75 | ⛔ **retired 2026-08-23 → `attic/`** |
| ↳ Fast round-leader props (`KXPGAR1LEAD` / `KXPGAR3LEAD`) | — | The **only** slice that never went negative. Never isolated into its own arm | 🔬 unexplored |
| ↳ Early-exit take-profit on the fade | — | Backtested: HURTS — halves ROI (17.3%→7.9%) to save ~1 day (corrections come late). Disabled, then retired with the arm | ❌ disabled |

## 5b. The near-close fades — closed by the retrospective ⛔

Same favourite-longshot bias, harvested at a **2-hour lead** instead of days
out. Judged on **mean P&L per independent cluster, never win rate** — and on
2026-08-24 the per-cluster read plus a two-surface retrospective closed the
thesis (see `edge-research-log.md` §2026-08-24):

| Arm | Headline | The honest number | Verdict |
|---|---|---|---|
| `fade_nearclose` (buy NO on YES 0.10–0.20) | +$— lifetime | uniform-stake era (≥07-30): **−$— / 618 clusters**, P(mean≤0)=0.61. Retrospective: the founding −6.0pp band mispricing is **+0.4pp** on 14,400 markets now; arm-selected executable −3.25¢/cluster — agrees with the forward arm | ⛔ **thesis closed in current regime** — the June–July edge was a transient that never reached significance in any single month |
| `back_nearclose` (buy YES 0.90–0.98) | +$— lifetime | +0.10¢/cluster [−1.1, +1.2] over 1,033 clusters (capture surface); −2.99¢ at the ask on the panel | ⛔ **never above noise** — mean +0.8¢ vs ~1¢ ask-side spread |

**Structural close:** median spreads at 2h lead run 3–15¢ against calibration
gaps of ±1pp — the venue doesn't need to be perfectly calibrated when its
spreads are 5–30× the mispricing. Three instruments agree (capture
retrospective, candle retrospective, the forward paper arm). The fill canary
is moot: there is no paper edge left to measure fills against. Re-entry
trigger, pre-registered: the monthly `candle_panel` calibration table showing
the 0.10–0.20 gap back below ~−4pp.

---

## The throughline (why the two columns differ so much)

Every dead strategy showed a **tempting raw number** that evaporated under one of these:
- **Executable prices, not mids** — the reaction "edge" was mid-to-mid; the ask already had the info.
- **Fees + spread** — favorite-longshot and favorite-hold die once you pay to trade.
- **De-correlation** — headline n's were really a few correlated events (settlement, prop-overround).
- **Variance vs edge** — reactive-hold's +$— is one lucky longshot, not a repeatable rate.
- **Hindsight** — settlement "arb" was buying eventual winners at mid-9th prices.

The retail-longshot fade **looked** like the one edge that survived all of
them — a real behavioral mechanism (retail overpays for exciting longshots
where no arbitrageur corrects them), and a number that held up net of cost,
de-correlated, and broad-based. **It did not survive out-of-sample.** The
forward paper arm resolved 200 positions for **−$—** (mean −$—) against
a backtest promising +11.8% ROI and 86% win by event; the parallel prop fade
resolved 224 for **−$—**. Both arms were retired to `attic/` on
2026-08-23. Add a sixth item to the list above, and it is the one that cost the
most to learn:
- **A backtest that clears every honesty control can still be wrong.**
  De-correlation by event, executable prices, fee-and-spread haircuts and a
  plausible mechanism were all necessary and none of them were sufficient. Only
  the forward sample settled it.

What remains: the two **near-close** arms (`fade_nearclose`, `back_nearclose`),
which harvest the same favourite-longshot bias at a 2-hour lead and are both
positive on frozen rules — encouraging, not yet a result. The one slice of the
retired arms that never went negative is the fast round-leader props
(`KXPGAR1LEAD`/`KXPGAR3LEAD`).

**Structural conclusion:** Kalshi is efficient everywhere the pros are (sports, crypto, big econ, liquid markets) — no speed/timing/microstructure edge for us there. Any exploitable inefficiency lives only in the thin, retail-driven, arbitrageur-light corner, as a behavioral (favorite-longshot) bias — not a speed edge.

**Amended 2026-08-23:** that conclusion survives, but it is now known to be
weaker than it read. The bias is *measurable* in the retail corner and still
was not *tradeable* there at a multi-day lead — the two arms that tried it lost
$— of paper money across 424 resolutions. What is left of the thesis is the
**near-close** version (§5b), where the same bias is harvested hours rather than
days before settlement. Whether the lead is what matters, or whether near-close
is simply the next thing to fail forward, is exactly what the frozen-rule OOS
clock is there to answer. Do not write "the retail corner is the edge" again
without saying which lead.
