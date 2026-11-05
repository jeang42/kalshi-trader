# Edge Research Log

*Canonical, session-spanning log of edge research: thesis → mechanism → evidence →
verdict, written **before** any instrument or arm is coded. Workflow: research
and documentation happen first; code follows from this doc. Sibling docs:
`strategy-scoreboard.md` (final verdicts on completed attempts),
`failed-experiments.md` (post-mortems), `compounding-capital-cycle.md` (the
sizing layer, gated).*

---

## 2026-07-09 — Reading the unread instruments + new-corner recon

### 1. ⭐ Near-close venue panel: ONE band survives executable prices — 2-hour-turnaround fade candidate

**Source:** `near_close_capture.sqlite` — 21,975 RESOLVED snapshots at ~2h lead
(accruing since 6-10; the 2026-06-10 `favorite_longshot_study` concluded "no
near-close edge" on a *starved* sample, n=7 — this panel overturns the sample-size
problem with 3 orders of magnitude more data).

**Mid-price calibration @2h lead (vol>50), venue-wide:** longshots overpriced —
0.10–0.20 gap **−6.0pp** (n=772), 0.20–0.35 −5.7pp, 0.35–0.55 −4.6pp; favorites
0.55–0.75 **+3.1pp** underpriced; deep 0.02–0.10 −1.6pp (n=3,324).

**Executable + de-correlated (the test that kills everything):** buy NO at
(1 − yes_bid), net Kalshi fee, ONE observation per event:

| band (yes mid) | events | by-event edge/contract | by-event win |
|---|---|---|---|
| **0.10–0.20** | **532** | **+1.6¢** | **90%** |
| 0.20–0.35 | 554 | −1.8¢ | 74% |
| 0.35–0.55 | 641 | −2.1¢ | 58% |
| favorites 0.55–0.75 (buy YES at ask) | 624 | −2.2¢ | 67% |

Category slice of the surviving zone: **Crypto +0.98¢/contract on 393 independent
events with 1¢ median spreads** (hourly/daily cycles, 24/7); Other +0.47¢ on 454
events (6¢ spreads); Finance −4.9¢ (dead).

**Verdict: the 0.10–0.20 near-close longshot fade is the strongest NEW lead —
~+1.9% per ~2-hour cycle at 90% by-event consistency on a 532-event de-correlated
sample.** Small per trade, but the turnaround is 2 HOURS (vs weeks for the retail
fade) — exactly the compounding substrate (`compounding-capital-cycle.md`).

**Open questions before an arm**: (a) crypto-market semantics —
the Crypto slice is hourly BTC/ETH strikes; verify the band isn't an artifact of
barrier/terminal structure; (b) capacity at the bid (books are tight but thin);
(c) fill realism — entry assumes taking the standing YES bid (marketable);
(d) stationarity — panel spans one month.
**Next build:** paper arm `pm/arms/fade_nearclose.py` — band 0.10–0.20 only, buy NO
marketable, hold ≤2h to resolution, de-correlated per-event logging, category
tags. PAPER ONLY.

### 2. Tennis react-and-hold gate: CLOSED — DEAD

**Source:** `tennis_capture.sqlite` forward samples — 1,143 market-matched live
events (1,002 breaks), **7,348 forward price samples** (the step-2 gate
measurement that was never read).

After a detected break/set/match win, the beneficiary-oriented book move is
**zero at every horizon**: means −0.16¢…+0.09¢, medians 0.0 across 30s→15m
buckets, all kinds. The book reprices before our 45s-cadence detection —
consistent with the MLB/tennis lag findings.

**Verdict: do NOT build the tennis react arm.** Backlog item closed. The
`tennis-capture` instrument keeps running passively (zero cost) but the thesis is
falsified at our detection latency.

### 3. WNBA WP-swing gate: weak fat-tail — lean dead

**Source:** `wnba_capture.sqlite` — 73 matched WP-swings, 1,480 forward samples.
Post-swing move: mean +0.58¢ @1–2m, +0.98¢ @2–5m — **but medians 0.0** (the mean
is a few outliers; the same fat-tail pathology that made MLB reaction look alive
for weeks). Below WNBA spreads (~1–2¢). **Verdict: lean dead; not worth an arm.
Keep the instrument passively accruing; revisit only if the mean grows with n and
the median moves off zero.**

### 4. Mention family (2026-07-08 find; instrument live)

Fastest-cycling retail corner (~300+ resolutions/3wk; Trump 133+56, WCMENTION 135
@63k med vol, Love Island 54 nightly, LateNight 42, ~10 earnings series). Mention
markets settle YES the moment the word is said on a public stream — deterministic
resolution; GPU whisper stack (`pm/mention/audio_transcribe.py`) already exists.
`pm/capture/mention_capture.py` (10s loop, live) is recording every quote change; **verdict
criterion: jump shape after a word hits — instant tick = sniper exists (dead);
30s–min grind = capturable human lag.** First distributions expected within days
(nightly broadcasts). Trader gated on that read.

### 5. NEW: Weather markets EXIST — daily-resolution physical-model candidate

Probe: `KXHIGHNY/DEN/CHI/AUS/MIA/LAX` each have **12 open markets** (daily
high-temp strike ladders), + `KXRAINNYC`. They never surfaced in our feed scans
(close-time feed flooded by esports/WC — same gotcha as MLB/retail/props).
**Mechanism:** daily resolution against official observations; NWS
forecasts/observations are free and physically modelable; retail corner.
**Unknowns:** volumes/spreads, who's already arbing NWS (weather was Kalshi's
oldest market class — sophisticated players may live here), resolved calibration.
**Next research:** pull resolved KXHIGH* history + NWS forecast archive; measure
whether the book at T−12h/T−2h deviates from NWS-implied probabilities.
Quick-turnaround (daily) if real.

### 6. NEW: Threshold-ladder monotonicity scanner (guaranteed-profit class)

Scalar ladders (e.g. `KXPUREALBUMS-…-{110K…250K}`) must be monotone:
P(>X) ≥ P(>Y) for X<Y. Spot-check found a **mid-price violation** (225K mid ~4.5¢
< 250K mid ~5¢) but **not executable** (no bid on the dominated leg). A venue-wide
scanner over all ladders checking *executable* dominance (ask(higher-strike) <
bid(lower-strike)) is cheap and is the arb-class complement to `arb_scan` (which
covers one-winner fields only). Expectation: rare/small, but risk-free when found.

### 7. NEW: Earnings-call mention base rates (predict, not react)

`KXEARNINGSMENTION*` (~10 series, ~127 resolutions/3wk in season) — Q2 wave lists
mid-July. Word likelihoods ("will the CEO say *tariff*") are estimable from
public past-call transcripts (or whisper on free IR webcast replays) vs retail
vibes-pricing. Days-turnaround prediction edge; measurable as soon as the wave
lists: compare base-rate model vs market price, log forecasts, no trading.

### 8. NEW: Album/chart leading indicators (direct info advantage on positions we HOLD)

Kalshi music markets resolve on Luminate/Billboard/Spotify published data.
**Leading indicators are public days early:** HITS Daily Double publishes
mid-week album sales projections (directly predicts `KXPUREALBUMS` /
`KXALBUMEQUIV` — the Ariana fades we hold resolve Aug 6 on exactly this);
kworb.net mirrors Spotify daily streams (predicts `KXSPOTIFYNUM1USA`, weekly
RANKLIST markets; `KXARTISTSTREAMSY` is year-long → slow, skip). **Next
research:** during the Ariana release week, compare HDD midweek projection to the
book — if the book lags the projection by hours-to-days, that's a repeatable
weekly edge (every major release).

### 9. Parked/smaller candidates
- **New-listing drift** — do fresh listings systematically misprice (anchor-at-50,
  longshot drift)? Measurable from candlesticks of recent listings. Medium effort.
- **Reality-TV winner fades** (Love Island winner etc.) — fandom overbets
  favorites' fans → longshot overpricing; already covered by the retail-fade arm's
  discovery when in band.
- **Approval/poll-average front-running** — compute the published average from
  component polls before the aggregator updates; no active Kalshi approval series
  found in current scans; revisit near election season.

---

## 2026-07-09 (later) — Full category census: perpetuals / commodities / social / "live"

*User asked whether we'd looked at perpetuals, commodities, social, and the live
tab. Honest answer: no — discovery had been keyword/series-driven and
flood-blinded. Ran a raw 4,000-event census using Kalshi's OWN category
taxonomy, then probed tradability (volume/spread) of the unexplored corners.*

**The taxonomy (open events):** Elections **2,148**(!), Sports 673, Entertainment
321, Politics 287, Economics 179, Financials 147, SciTech 81, Companies 67,
Climate/Weather 33, Crypto 17, Mentions 15, Social 10, Commodities 9, Health 6.

### Findings by the user's list

- **Perpetuals** — don't exist on Kalshi (that's crypto-exchange derivatives).
  Nearest analog = rolling hourly/daily series (BTC etc.), already probed
  (HFT-lockstep, dead) — though the near-close crypto slice (+0.98¢, §1 above)
  is the exploitable residue of that family.
- **Commodities** — small (9 events; KXWTI oil ladders 105 open markets,
  med vol 6,285, 3¢ spreads; gold/direction-year). **Likely efficient**: real
  futures markets next door (CME) for pros to arb against, and the near-close
  panel's Finance slice was already dead (−4.9¢). Low priority.
- **Social** — real mechanism, tiny universe: Twitch sub-count markets
  (Ninja vol 8,871; Kai Cenat 6 mkts @139), MrBeast donation total (1,389).
  These resolve on **public real-time counters** (Twitch/YouTube APIs pollable) —
  the "watch the feed" family — but ~10 events total, 7–9¢ spreads →
  **opportunistic watchlist, not an arm.** Recheck when the universe grows.
- **"Live" tab** — not an API category; it's the in-game sports UI grouping.
  Real-time exploitation status: in-game sports books are the FALSIFIED corner
  (MLB captured-edge, tennis zero-move, WNBA fat-tail — pros reprice them);
  the live plays that remain are **mentions** (thin human-updated books —
  measuring NOW via `mention_capture`) and live award/reality broadcasts (same
  family). i.e., we exploit *live* where the marginal price-setter is a human
  fan, not a pro.

### ⭐ Upgrades from the census

- **Daily weather is LIQUID — candidate upgraded.** `KXHIGHNY` med vol **8,118**
  with **1¢ spreads** across a 12-strike daily ladder (×6 cities). Daily
  resolution + tight books + free NWS physics (same-day high forecasts have
  ~1.5–2°F MAE) = the best *structural* quick-turnaround candidate outside
  near-close. Open question: are the books already NWS-calibrated (someone
  semi-pro clearly trades them)? **Next research: capture NWS forecast + book
  daily and/or resolved-history calibration vs NDFD forecast archive (Iowa
  Mesonet, free).**
- **KXGTEMP (yearly global temp): vol 83,240, 3¢ spread** — big slow market;
  modelable mid-year from published YTD anomalies (Berkeley Earth projections).
  Slow (Dec resolution) but potentially large mispricing. Medium/slow lane.
- **Hurricanes (seasonal, thin for now)**: KXHURRICANE/KXNAMEDSTORM vols 7–235,
  spreads 8–58¢ today — but NHC advisories land at SCHEDULED times
  (5/11am/5/11pm EDT) and these books are retail → scheduled-release
  front-running on storm-threat weeks (Aug–Sep volume spikes). Seasonal
  watchlist.
- **The Elections wave is HERE: 2,148 open election events** — the 2026-midterm
  primaries flood the retail fade's home turf. The existing `fade_retail_longshot`
  arm picks these up automatically as they enter its ≤30d window — its OOS
  sample is about to grow much faster than projected. No action needed; expect
  accelerating accrual.
  - **Outcome (2026-08-23):** the wave arrived and the sample did grow as
    predicted — 200 resolutions, **−$—**. This entry is left as written
    because the prediction it makes ("the real test is coming") was correct;
    only the hoped-for direction was wrong. The arm is retired to `attic/`; see
    `docs/failed-experiments.md` §"Favourite-longshot fades at a multi-day
    lead".

---

## 2026-07-09 — Incumbent-latency study: how fast IS the fast money in MLB books? (CLOSES the stadium/courtside question)

*Trigger: user lives near an MLB ballpark and asked whether attending
games — or a phone app listening to the stadium announcer — could beat the book.
All prior "the ask reprices instantly" conclusions were bounded by our 10–30s
delayed feed; this study measures the incumbents' true speed from venue-exact
data.*

**Method:** 7,216 scoring plays with OFFICIAL MLB timestamps (`live_plays`
raw_json `about.endTime`) × the venue-timestamped trade tape (`trade_archive`,
2.65M prints), 633 games bridged. For each play: seconds from real-world play end
to (a) the first trade, (b) the first ≥2¢ traded-price move. Placebo = same
measurement at random non-event times.

| seconds from official play end to… | n | p25 | median | p75 |
|---|---|---|---|---|
| first trade (any) | 1,857 | **0.5s** | **1.2s** | 3.0s |
| first ≥2¢ move (all scoring plays) | 1,452 | 7.6s | 31.3s | 77.8s |
| first ≥2¢ move, **high-leverage** (inn≥7, close) | 166 | **1.1s** | **4.4s** | 24.7s |
| placebo (random times) | 1,523 | 19.8s | 57.0s | 122.3s |

**Reading:** the placebo gap (4.4s vs 57s) proves the post-play moves are real
event-driven repricing — and the incumbents are at **sub-second to ~4s on
exactly the plays a latency strategy would trade**. A quarter of high-leverage
plays are repriced within ~1.1s of the ball landing.

**Verdicts (all pathways):**
- **In-stadium announcer-listening app** (t+5–12s: PA lags the play + whisper
  chunks): ❌ loses decisively. Also *raises* venue-terms risk (automated
  courtsiding profile), doesn't lower it.
- **Home radio (OTA) + whisper** (t+5–10s): ❌ loses — the median high-leverage
  reprice (4.4s) beats it.
- **Human at the game + one-tap** (t+2–3s): ❌ marginal-at-best coin-flip — beats
  the median play by a hair, loses the p25 quarter outright, and the prize for
  winning is the post-play ask spike (captured-edge study: ≈0 net at executable
  prices even when you catch it). Plus ticket-terms ejection risk.
- **Roar-detector** (t+1–2s): ❌ same marginal zone, worse disambiguation.

**Conclusion: the MLB-book latency question is now closed with a measured
number, not an inference.** "Pros reprice in seconds" = literally first trades at
0.5–1.2s, 2¢ repricing at 1–4s on high-leverage plays. No human/audio/consumer
path competes. This *quantifies* the pro-ladder doctrine: don't race these
incumbents — the benchmark for the mention-sniping thesis is now concrete
(`mention_capture` jump shapes vs this 4.4s standard; if mention books take
30s+, that's the corner where WE are the incumbent-speed player).

*Ops note for future studies: `baseball.analysis.tickers_for_game` costs ~1.3s/call
(rescans per call) — batch the game→ticker bridge in one query next time (~20min
→ seconds for 1,054 games). `trade_archive` already has `idx_trades_ticker_ts`.*

---

## 2026-07-09 (eve) — First OOS day for the near-close fade + first mention jump-shapes

### Near-close fade: DAY-1 OOS CONFIRMS the backtest direction ✅

`fade_nearclose` (built yesterday; resolution bug — my spec pointed at
`lookup_markets_by_id` which never returns `resolved_yes` — fixed via the
market-detail `status=finalized`/`result` path, merge `e2c9dea`):

**First resolved batch: 60 independent events, net +$— on ~$— deployed
(~+2.7%/trade), 93% win** — vs the panel backtest's +1.9%/trade and 90%
by-event win. Slightly BETTER than predicted, on the first out-of-sample day,
with per-event de-correlation by construction. Losses behave as modeled (4
longshots resolved YES at ~−$—–10 each; many small wins carry them).
Category split: Other +$— (94%, weather/temp-heavy), Crypto +$— (89%).
Caveat: one day, one market-mix regime; the arm now accrues ~50–70 events/day
so a definitive read (≥300–500 events) arrives within ~a week. If it holds,
this is the compounding-substrate arm (2h capital cycle) and the first
candidate for a live canary after the OOS bar clears.

### Mention jump-shapes: promising, not proven — instrument at its resolution limit

23 YES-ward jumps (<0.60 → >0.85 mid) in the first ~20h of `mention_capture`:
- **Fast cluster (11/23): ≤20–80s, zero intermediate quotes** — WC mention
  markets during live matches; the liquid, watched books. Some are wide/empty-
  book artifacts (e.g. bid 0.01/ask 0.98). Even so: **5–20× slower than MLB's
  4.4s** — the incumbents here are humans.
- **Slow cluster (12/23): 8min–11h grinds with dozens of quote-changes**
  (Love Island words, "Ceasefire", "Nutmeg" 10.8h/250 changes) — but
  indistinguishable from pre-utterance ANTICIPATION in this data.

Two blockers for a verdict: no utterance ground-truth (can't split anticipation
from post-hit lag) and 10–19s poll cadence (can't resolve inside the fast
cluster). **Decisive next measurement: one live broadcast window (WC match /
Love Island night) with (a) `kalshi_ws` orderbook stream on the mention tickers
(sub-100ms, infra exists) + (b) whisper transcription as utterance ground truth.**
If post-utterance lag ≥20s confirms, whisper at t+2–5s wins the race and the
sniping arm gets built; if books move pre/at-utterance, thesis dies cheaply.

---

## 2026-07-09 (night) — ⭐ Base-rate-calibrated mention trading (user-surfaced, strong)

*Origin: user made small manual YES bets on tonight's Love Island mention words
(1–3¢) and noticed "people bought tons of NO recently on everything." Both
observations checked out and led to a real, backtestable strategy — the
CALIBRATED version of the mention/fade work.*

**The NO-sweep is real + quantified.** `mention_capture` shows the Love Island
field-average YES mid fell 0.44→0.17 over the day (nearly halved), 15/18 words
dropped >2¢ — a coordinated NO-sweep into the episode that pushed the whole
field to pennies **without regard to per-word likelihood**.

**Resolution rules (read from market detail):** "if any participating contestant
(excluding host) says [word] during Love Island S8 Ep32 → YES." One episode, any
mention. So the penny prices are NOT explained by narrow criteria — they're the
fade-sweep overshooting common words.

**The edge, from 469 SETTLED mention markets / 26 episodes — per-word YES base
rate vs the swept price:** DRAM 38% (@1¢, +37pp), LOYA 29%, CASA 31%, FIRE 29%,
EX 25%, DUMP 21%, IG 17%, BETR 10%, PRIZ 8% — the user's basket is +EV on 9/10
words, several by 20–37pp. The sweep crushed common words FAR below their
historical base rates.

**The strategy (backtestable now):** build a per-word historical YES base-rate
table from settled mention markets (all series: Trump/WC/LoveIsland/earnings/…);
each episode, compare each word's price to its base rate; **fade the overpriced
words (buy NO) AND back the fade-crushed underpriced words (buy YES).** This is
the calibrated resolution to the whole mention thread — not blind fade (kills us
on common words, cf. Trump "Democrat"), not blind back, but base-rate-scored both
directions. It also reframes the fade arm's mention handling.

**Open questions before an arm:** (a) is the pre-episode NO-sweep REPEATABLE
(every episode?) or a one-off — the setup that creates the YES value; (b) base-
rate stationarity + episode-type conditioning (recap/reunion/Casa episodes differ
wildly — tonight's Ep32 format is the live test of whether the sweep was dumb or
informed); (c) correlation — one episode resolves all ~18 words together, so a
basket is high-variance (lumpy per night, +EV over many). **Next research:**
backtest the base-rate-vs-price rule over the 469 settled LoveIsland markets +
the other mention series (Trump 189, WC 135…); measure the pre-event sweep's
repeatability from `mention_capture`. **Tonight's episode is the first live
data point** (does the crushed field resolve to its base rates?). Tracking the
user's basket in `data/shared/user_loveisland_bets_20260709.json`.

---

## 2026-07-10 — ⚠️ CORRECTION: Love Island is a streaming DROP, not live — the base-rate "edge" was informed-sniper front-running

*User caught it (~16 min after the 9pm ET "start"): the market had already settled
— DATE/FIJI/HIDE priced ~0.99 YES, everything else (incl. the whole user basket)
~0.01 NO. Our `li_book` sub-second capture proves the tell: **every market hit its
final extreme between 7:23–8:33pm ET, BEFORE the 9pm start; nothing repriced at
9pm.** The market had the answer before airtime.*

**Mechanism (user's hypothesis, confirmed):** Love Island is a **pre-recorded
streaming DROP** — the FULL episode is available at once. Snipers batch-transcribe
the whole ~40min audio faster-than-realtime (GPU whisper does 40min in ~1min) the
moment it's available and settle EVERY market in minutes. There is no progressive
reveal, so **no live-sniping window exists** — the "edge" is batch-processing the
drop fastest, a compute race we won't win.

**This overturns the 2026-07-09 base-rate lead (be honest):** the "NO-sweep to
pennies" I flagged as maybe-dumb-fade-flow was almost certainly **informed** — the
snipers had already processed the episode and priced each word to its true 0/1
(0.01 pricing = "we KNOW", not base-rate uncertainty which would be 0.1–0.4). My
"+37pp vs base rate" compared an *informed conditional* price to an *unconditional*
prior — a mirage. The snipers front-ran the base-rate model. User's basket (~$—)
is priced to lose; the words genuinely weren't said this episode.

**The durable lesson — a hard filter for the whole mention/live thesis: LIVE
(progressive reveal) vs DROP (instant full availability).**
- **DROP content** (Love Island, scripted/pre-recorded streaming): ❌ dead. Settled
  by fast batch-transcription in minutes; often before nominal airtime. Base-rate
  and live-sniping both lose to the batch snipers.
- **LIVE content** (speeches/debates/pressers on C-SPAN/YouTube, live sports, live
  award shows): the ONLY place a lag can exist, because content is revealed in
  real-time and can't be pre-processed. This is where `mention_capture` +
  `audio_transcribe` should point. Love Island was the wrong vehicle — pre-recorded.

**Value delivered:** cheap ($— of the user's own money) tuition for a clean
distinction that reshapes the thesis. The measurement infra worked perfectly — the
sub-second book capture is exactly what exposed the pre-airtime settle. Mention
base-rate strategy: NOT dead in general, but only tradeable on content that is
genuinely LIVE and/or BEFORE snipers can process it — verify the live/drop nature
of each series first. `mention_capture` keeps running (still valid for the live
series: WC matches, debates, pressers).

---

## 2026-07-10 — Live-event test calendar (applying the LIVE-vs-DROP filter)

*Swept all mention series for genuinely-LIVE (progressive-reveal) events. Three
tests inside six days, escalating in autonomy:*

**Test 1 — Fri Jul 10, 3:00pm ET: Spain vs Belgium (`KXWCMENTION-26JUL10ESPBEL`, 32 words).**
"What will the announcers say during…" — live sports broadcast, cannot be
pre-processed. Server: `li_book_capture` (sub-second WS book) re-pointed at the
32 tickers, launched ahead of kickoff. User PC: the already-tested `li_listen.py`
whisper rig against the Fox broadcast in a browser (user subscribes). Measures
the first true post-utterance lag on live content. Two more WC-match events
(Jul 11 ARG-SUI, NOR-ENG) allow immediate replication.

**Test 2 — Tue Jul 14: big-bank earnings calls (`KXEARNINGSMENTIONJPM/C/WFC`,
26JUL14, 48 word-markets).** The architecturally cleanest: public IR webcasts at
exact scheduled times (JPM ~8:30am ET), typically DRM-free → the SERVER can pull
audio via ffmpeg directly = fully autonomous whisper+book, no user in the loop.
ALSO the honest venue for the base-rate predict angle (price "Tariff" etc. vs
past-call base rates BEFORE the event — legitimate here because a not-yet-
-happened call cannot be pre-processed by snipers). Day-of webcast-URL recon
needed.

**Test 3 — Wed Jul 15: Trump Pennsylvania Defense & Innovation event
(`KXTRUMPMENTION-26JUL15`, 25 words).** Live speech, C-SPAN/YouTube stream,
ffmpeg-direct on the server. Second autonomous rehearsal.

**Live-vs-drop taxonomy (verify per event, not per series):** WCMENTION LIVE;
EARNINGSMENTION* LIVE (scheduled webcast); TRUMPMENTION per-event (WHCD + PA
speech LIVE; "Pre-Tape Podcast" was a DROP); LOVEISLMENTION DROP (proven);
LATENIGHT/cable appearances = reveal-at-airtime (live-ish; fine). The Kalshi
series label doesn't determine it — the event's production format does.

---

## 2026-07-14 — Near-close fade OOS correction + the cherry-pick study (what replicates, what's noise)

**Thesis under test:** the near-close 0.10–0.20 fade (the #1 lead) after 5 days
forward paper, and the user's question: *can we cherry-pick the best/safest
slices and size up on them instead of fading everything?*

**Part 1 — OOS correction (shipped today).** The raw arm ran **−$— at 88%
win over 643 resolved fades** — longshot-fade fat-tail, live. Cut by market
STRUCTURE (not Kalshi category), the bleed is entirely two classes the backtest
had under-sampled (panel had n≈10/series; the arm accumulated n=274):

| Structural kind | n | net | ¢/ct |
|---|---:|---:|---:|
| weather (KXTEMP*/KXLOWT*) | 274 | −$— | −3.4 |
| commodity/energy settles | 45 | −$— | −11.3 |
| crypto/index thresholds | 312 | **+$—** | **+1.1** |
| discrete retail (mention/award/chart/econ-print) | 23 | **+$—** | **+3.6** |

**Mechanism:** weather/commodity near-close longshots are genuine Gaussian
tails on a continuous underlying, priced off realized vol by participants who
model it — no retail overpricing to fade, and near settle the underlying drifts
TOWARD the threshold (commodity −11¢/ct is that drift). The behavioral fade
edge exists only in fan-priced discrete markets. `EXCLUDE_SERIES_PREFIXES` +
`series_excluded()` shipped in `pm/arms/fade_nearclose.py`; excluding the two classes
flips the arm to ~+$—. (FX — KXEURUSD/KXUSDJPY — same structural class, added
to exclusions; index was already mostly caught by the Finance-category filter.)

**Part 2 — cherry-pick cross-validation.** Two INDEPENDENT samples, identical
economics (buy NO at 1−yes_bid, net fee, one obs/event): (A) the capture panel
(530 kept events) and (B) the live arm's own fills (339 kept events).

*Family-level (asset × cadence) selection FAILS — sign flips everywhere:*
HYPE/15m +13.1→−4.1; ETH/15m +12.9→−2.1; ETH/hourly +8.3→−1.2; XRP/daily
+6.3→−4.4; XRP/15m −5.0→+13.1; SOL/eod −4.7→+5.3. At n=5–30/family with −90¢
tail losses, per-family means are noise. Sub-band splits (0.10–0.15 vs
0.15–0.20) also flip between samples. **Do NOT pick winners by realized
per-family P&L.**

*What replicates:* kind-level — **crypto +2.07¢ (A, n=404) vs +2.01¢ (B,
n=268)**; discrete retail +1.37¢ (n=72) vs +3.62¢ (n=23). Both positive in both
samples; crypto's agreement is near-exact.

*The ONE defensible slice — spread ≤2¢ (panel only, arm didn't store spread):*
crypto tight-spread **+4.40¢/ct (n=206)** vs wide **−0.36¢ (n=198)**.
Trustworthy because it's MECHANISTIC (a cost filter, not an outcome filter): we
pay (1−yes_bid), so the half-spread is direct entry cost against a ~2¢ edge.
Tight books carry essentially ALL of the crypto edge.

**Verdict → sizing design (paper):** cherry-pick by STRUCTURE + COST, never by
realized family P&L. Tier the paper stakes: base $—; **$— (4×) for crypto
with spread ≤2¢** — the loading-up lever the user asked for, pointed at the
only slice with big-n + mechanism. Record `spread` + structural `kind` + stake
tier on every position so the live arm OOS-validates the spread split itself.
Live compounding stays gated per `docs/compounding-capital-cycle.md` (arm must
confirm OOS post-fix first; ~1 week of clean data).

---

## 2026-07-18 — ⛔ GATE READ: live-mention repricing is one-tick INSTANT (Trump PA speech, first LIVE-content measurement)

**The question this answers** (the gate the mention-sniping trader was built
behind): when a word is said on genuinely-LIVE content — no pre-processing
possible — does the book reprice instantly (sniper exists → dead) or grind
(capturable)? The Jul-15 Trump Pennsylvania speech (`KXTRUMPMENTION-26JUL15`,
28 markets, 18 YES / 10 NO, 25k captured deltas at ~10s cadence) is the first
clean LIVE sample.

**Finding 1 — clean utterances reprice in ONE capture tick.** ~16/18 YES words
went pre-utterance-level → 0.99/1.00 inside a single ≤10–40s delta. The
dramatic cases prove the point: HOTT decayed 0.60→0.32 over 40 min (market
pricing "not said yet"), then 0.33→1.00 in one tick at 20:08:06; RUSS decayed
0.47→0.11, then 0.16→1.00 in one tick at 20:16:06. Somebody lifts the whole
book within seconds of the utterance, on live content, even at minute 45 of a
speech. (My first-pass "grind" reads were a detector artifact — the detector
caught pre-utterance chop, not the utterance.) Only MANU/IRAN show maybe-grind
(~10–18¢ over 15–30 min) — plausibly genuine adjudication ambiguity, n=2, thin.

**Finding 2 — mid-speech anticipation pricing is roughly CALIBRATED.** 7/10
NO-words spiked ≥0.49 mid-speech (DRON held ~0.82 for 20 min — "drone" at a
Defense & Innovation event — then collapsed to 0.05). Looks fade-able, but the
YES-words sat in the same 0.4–0.8 band before their utterances: band-wide,
~10 YES vs ~6 NO ≈ 62% resolved YES at ~0.6 mean price. No fade edge either
side. The crowd prices "will it be said" about right and repricings are instant
— **this market is efficient on live content, matching every other speed probe.**

**Caveats:** n=1 event; 10s+delta cadence can't see a sub-10s window (but our
whisper chunk latency is ~5–15s, so a <10s window wouldn't be ours anyway);
the planned sub-second WS capture (`li_book_capture`) for the Jul-10/11 WC
matches has no artifacts in the repo — that test appears not to have run.
The Jul-14 earnings-call test was ALSO missed: `KXEARNINGSMENTION*` series were
never added to `MENTION_SERIES` (fixed today — JPM/C/WFC/GS/MS/BAC added, 15
series now watched).

**Verdict:** the live-mention SNIPING thesis (race the utterance) is
**dead-or-marginal** — do not build the trader on current evidence. What
survives: (a) the **earnings base-rate angle** (pre-position BEFORE a call on
base-rate-vs-price gaps — a forecasting edge, no race; instrument now captures
the series for the next earnings wave), (b) replication on 1–2 more live
events falls out of the passive capture for free — revisit only if a
qualitatively different shape shows up.

---

## 2026-07-19 (early AM) — ⚠️ MAJOR REVISION: the trade tape shows the "instant" reprice is a 7–60s HUMAN CASCADE, and the prize is 5 figures per event

**The unlock:** Kalshi's public executed-trade tape (`get_all_trades`) is
backfillable with exchange timestamps at full precision — no WS needed for
post-hoc latency measurement. Pulled the tape for the Trump-speech and
WC-final (played 07-18 22:10 UTC; missed the live sub-second window on a
date slip) mention sweeps. Yesterday's "one-tick instant" verdict was an
artifact of the 10s capture cadence.

**Cascade anatomy (Trump 07-15):** HOTT — first fills 0.29–0.36, heavy
volume 0.34–0.72 over the next 6s, 0.97 by +7s, 0.99 by +16s. GOLD — sweep
0.18→0.91 in 1s, 0.99 by +10s, **preceded by ~90s of pre-utterance
accumulation at 0.09–0.18** (somebody listening to topic context scaled in
250+ contracts BEFORE the word landed). Repeated 15/45-lot fill patterns =
a CROWD of fast human listeners, not one HFT bot.

**WC final (07-18):** cascades even slower and much deeper — 23–60s to
0.95, with 17k–65k contracts per word (TRUM swept from 0.04 on 64,892
contracts; GIAN from 0.08 on 30k). Upper-bound sniper revenue on the Trump
speech alone ≈ $—k (34.9k contracts at the jumps × pre-ask edge); true
take lower (fills walk the book) but the per-event prize is thousands, and
the calendar is weekly-plus.

**Revised verdict:** the pure "win the race" snipe is still hard (first
fills land ~4s post-utterance; a cable feed alone is 4–8s) — but it's NOT
required. Two capturable tiers: (1) **mid-cascade participation** — an
8–12s feed+KWS+pre-staged-order pipeline lands inside a 23–60s cascade at
0.4–0.8 fills; (2) **context anticipation** (the GOLD pattern) — streaming
transcript + topic model buys cheap words when the speech approaches them,
30–90s of leisure, no race at all. Tier 2 is the richer, less-contested
edge and matches our whisper+LLM stack.

**Next:** `mention-ws-jul19` transient unit is live (26 tickers, book depth
@15s + full trade tape) through Sunday's FTN morning show + late-night
show — gives resting-depth-before-utterance (capacity) + two more cascade
samples. Then spec the Tier-2 anticipator against the Trump 07-24 event.

---

## 2026-07-19 (midday) — First pipeline rehearsal (FTN replay): mechanics work; SPEAKER ATTRIBUTION is the gate; single-speaker events are Tier-1-safe

**Setup:** Track A shipped (`pm/mention/mention_listen.py` detection + `pm/mention/mention_rehearsal.py`
paper-fire/tape-join, 43 tests). Both Sunday live targets
(FTN 14:30 UTC; the "latenight" event = Corden's WC post-show, overnight)
aired before the build finished — so the rehearsal ran in REPLAY mode on the
official FTN Homan-interview audio (7:08, YouTube), scored against the settled
answer key (YES: BODY/SANC/BORD) + the real trade tape + the WS depth capture
(both events, 15s books, 04:26–16:57 UTC).

**Results — 5 fires, 2 right, 3 wrong, 1 miss; paper ≈ −$— on $— stakes:**
BORD ✓ (+~$— but detected off the ANCHOR's intro — lucky), BODY ✓ (+~$—
clean Homan utterance), MAIN ✗ / MINN ✗ (BOTH said by moderator Brennan, not
Homan — −$— each), TRUM ✗ (anchor intro + it's a 3+ COUNT market — −$—),
SANC missed (the word IS in the audio at 1:26 — full-file whisper catches it,
the 3s-chunk streaming pass dropped it → recall bug, not an ASR limit).

**The structural read:** on multi-speaker content the losses are built-in:
likely words price 0.6–0.8 pre-utterance (small upside when right) while the
moderator's stray words are cheap longshots (total loss when wrong).
Speaker-blind v0 on interviews = reliable net-negative. **Single-speaker
events (Trump speeches/rallies — also the biggest tape prize) have no
attribution problem at all** — the only voice is the resolving voice.

**v0.1 fixes queued:** (1) chunk-overlap/recall fix (the SANC class),
(2) COUNT-market support (fire at Nth occurrence), (3) config flag
`single_speaker: bool` — Tier-1 live rehearsals restricted to
single-speaker events until diarization (pyannote, phase 2) lands.
**Next live target: Trump event Fri 07-24** — single-speaker, ~28 markets,
tuner likely arrived. Zero dollars were risked in this rehearsal; it found
three pipeline defects and one strategic constraint for free.

---

## 2026-07-24 — Live-sniping infra stood up (OTA tuner + full pipeline) + WHCD live rehearsal; fade-nearclose spread-tier OOS INVERTS

**Two threads.**

**(1) Mention-sniping goes from thesis to instrumented pipeline.** Built + wired
the full stack for the 7–60s human-cascade edge (docs 2026-07-19):
`pm/mention/mention_listen.py` (audio→whisper word detection, v0.1: 5s/1s chunk fix +
count-markets + single_speaker guard), `pm/mention/mention_rehearsal.py` (paper-fire +
trade-tape scoring, no orders), `pm/mention/mention_cascade_backfill.py` (reconstruct
cascade shapes from the historical tape — validated: reproduces Trump-Jul15
HOTT at 7.0s exactly), `pm/mention/feed_latency.py` (cross-correlation feed picker,
sign-verified on real audio), `pm/mention/hdhomerun.py` (OTA tuner, tuner-pinned for Plex
coexistence). **Hardware live**: HDHomeRun Flex Quatro (DFW: CBS 11.1 / Fox 4.1
/ NBC 5.1 / ABC 8.1), full antenna→ffmpeg→whisper chain validated. FTN replay
rehearsal (07-19) taught the load-bearing lesson: **speaker attribution is the
gate** — multi-speaker interviews are net-negative (moderator's words resolve
NO), single-speaker events (speeches) are Tier-1-safe.

**WHCD live rehearsal (tonight 07-24, single-speaker Trump):** WHCD airs
C-SPAN-only (cable, not OTA) → feed = C-SPAN YouTube `NNRSNp-pnsI`. Autonomous
paper capture (`whcd-capture` transient unit) waits for the stream, runs
listener + rehearsal-follow through the dinner. Real-money plan = **Path A**:
human-in-the-loop $—/$— base-rate canary on a near-certain word priced <~0.90
before it's said (books are EMPTY pre-event — liquidity only appears during the
broadcast, so no pre-position possible). NOT auto-fire (untested code on real
money during a live event = against measure-first discipline). Weekend blackout
(system off Fri night→Wed) costs nothing on data: cascades backfill from the
tape Wednesday (WHCD + MLB announcer-mention games Jul 25-26 — `KXMLBMENTION`
"what will the announcers say", the high-frequency redirect of the dead MLB
game-reaction thesis).

**(2) ⚠️ fade-nearclose spread-tier premise FAILS OOS (but the arm is healthy).**
Post-fix arm now **+$— net at 90% win** (from −$— pre-fix) — the
weather/commodity exclusion + kind-level edge fully vindicated: crypto
+$— (+2.08¢/ct, matches the +2¢ three-sample estimate), discrete_retail
+$— (+4.27¢/ct). BUT the **$— tight-spread-crypto tier's premise is
inverted OOS**: the panel said tight(≤2¢) +4.40¢ vs wide −0.36¢; two live reads
now say the OPPOSITE — **tight +1.63¢/ct (n=115) vs wide +3.63¢/ct (n=243)**,
consistent across 07-18 (tight +1.35 / wide +3.97) and 07-24. Both buckets are
positive (the $— tier still made +$—), so it's not losing money — but the
"tight book carries the edge" rationale is wrong; if anything wider spreads =
more mispricing = more edge (plausible: retail overpricing the longshot creates
BOTH the wide spread and the edge). **Action: flatten the tier to uniform stake
OR re-base on the wide-spread signal — the spread refinement isn't earning its
complexity.** Deferred to post-weekend (paper, no urgency).

---

## 2026-07-30 — WHCD scored + announcer-mention gate read: cascade speed scales with ATTENTION; two silent tool bugs falsified prior reads

**Thesis under test:** (a) is our live detection pipeline fast enough to land
early in a mention cascade, and (b) do MLB/WNBA **announcer** mention books —
the attribution-FREE, highest-frequency corner (tier 1 of the 07-24 attribution
ladder) — cascade slowly enough to be capturable?

### ⚠️ First: two silent bugs that invalidated earlier reads

1. **`mention_rehearsal` scored nothing.** `ts_detected` is written in epoch
   **milliseconds** (`mention_listen`'s documented schema) but every consumer
   treated it as seconds. `get_all_trades(since_ts=…)` 400'd on every call, so
   the whole report rendered `n/a` — *and the false-positive counter printed 0*
   while 4 real false positives sat in the table. A broken measurement that
   reads as a clean result is worse than no measurement. Fixed via `_ts_seconds()`
   normalization at the consumption boundary (handles both units, back-compatible
   with rows already on disk); added a fill-independent **detector precision**
   line so detection errors are reported whether or not a fill was simulated.
2. **`fade_props` + `fade_retail_longshot` could never resolve a position.** Both
   read a `resolved_yes` column off `lookup_markets_by_id`, which doesn't produce
   one → silent `continue` forever → 172 and 160 positions open, **0 resolved**,
   for weeks. Fixed to the raw `status=="finalized"` + `result` path that
   `fade_nearclose` already used. See the verdicts section below.

### (a) WHCD (`KXTRUMPMENTION-26JUL24`) — the marquee event repriced in ~1 second

45 markets, 19 YES / 26 NO, **50,593 contracts** of cascade volume.

| Shape | n | |
|---|---|---|
| `instant` (≤2s) | 11 | |
| `fast` (≤15s) | 4 | |
| `slow_cascade` (>15s) | 4 | Dumbocrat 31s, Oil 54s, Iran 54s, TDS 59s |

**Median `seconds_to_95`: 1.0s.** This substantially walks back the 07-19
revision's "7–60s human cascade" as a general property. Reconciling with the
07-23 validation (Trump 07-15 HOTT cascaded in exactly 7.0s) and the WC final
(median 2.5s, 4/14 slow), the pattern that fits all three is:

> **Cascade speed scales with attention.** A low-profile speech leaves a 7–60s
> human window; a marquee, heavily-watched event is swept in ~1s. The window is
> widest exactly where the prize is smallest — the same shape as every prior
> null result in this project.

**Detector performance (14 detections vs Kalshi ground truth):**
- **Precision 71%** (10/14). Wrong: Supreme Court, First Term, AI, Epstein.
- **Recall ~53%** (10 of 19 YES words); missed Kamala, Newsom, Biden, Israel,
  IQ, UFC, Oil, Iran, Dumbocrat.
- **0 of 14 beat a cascade.**
- Of the 10 "correct" calls, **6 fired on C-SPAN commentators ~3.6 hours before
  Trump said the word.** They scored as hits only because Trump later said them
  independently — accidentally right, worthless as a race signal, and a live
  auto-buyer would have paid pre-cascade prices on a coin flip.
- The 3 detections that landed inside Trump's remarks *and* mapped to a cascade
  were **30–57 minutes LATE** (China +30, TDS +57, Hottest +57) — first-utterance
  recall misses caught on a later repeat, long after the book had settled at 0.95+.

**Verdict on Tier-2 (solo-speaker time-gate):** the attribution logic is sound,
but the detector is not accurate or fast enough to trade this event class. At 71%
precision / 53% recall / 0 cascades beaten, an auto-buyer is strictly negative.
The `pre_jump` prices confirm the prize was real (Israel 0.125, Oil 0.105, IQ
0.18 → $1) — we simply never got there first.

### (b) MLB / WNBA announcer mentions — real windows, small prize

5 settled events (`26JUL25SEATEX`, `26JUL25LADNYM`, `26JUL26CLETB`,
`26JUL28CHCSTL`, `26JUL28INDSEA`), **41 YES words**:

| Shape | n | share |
|---|---|---|
| `instant` (≤2s) | 14 | 34% |
| `fast` (≤15s) | 11 | 27% |
| `slow_cascade` (>15s) | 8 | 20% |
| `no_sweep` | 8 | 20% |

Per-event median `seconds_to_95`: 25s / 0s / 4s / 0s / 3s. **~39% of words either
cascade slowly or never sweep at all** — a genuine, repeatable window, and
attribution is free (booth is the only speech). Good entries exist: Draft 0.11,
No Hitter 0.27, Grand Slam 0.28, Shutout 0.26 → $1.

**But the prize is ~15× thinner.** Cascade volume runs **1,322–3,925 contracts
per event vs WHCD's 50,593.** A perfect snipe on a whole announcer game is worth
tens of dollars, not the 5 figures the 07-19 note projected from WC/Trump.

**Verdict:** the announcer corner is the most *tradeable* mention niche (free
attribution, real windows, high frequency ~19 MLB + 13 WNBA events in 2 weeks)
and the least *lucrative* per event. It is a volume/compounding play, not a
jackpot — which makes detector precision, not speed, the binding constraint:
at 71% precision the false positives eat a thin per-event prize. **Next action:
fix recall/precision on a dedicated single-source booth feed before building any
auto-fire arm.** Do not build the trader on the current detector.

### Cross-cutting caution logged
Both bugs shared a failure mode: **an instrument that silently degrades to a
clean-looking null.** The rehearsal printed "false positives: 0"; the fade arms
printed "resolved=0" as though nothing had settled. Any new measurement tool
should assert that its inputs are non-empty and fail loudly when they aren't.

---

## 2026-07-30 (late) — PANEL RE-MINE at 4× the data: the FAVORITE side of the near-close surface is systematically cheap in continuous-underlying classes

**Context.** User directive: revisit killed methods, mix and match, find what we're
missing. The near-close panel has grown to **81,260 snapshots / 4,561 resolved
de-correlated events** (vs ~22k snapshots at the 07-09 mine). Re-mined the whole
band × class × **side** matrix — crucially including BUY-YES, which the 07-09
mine never tested (it swept buy-NO only). Methodology: executable prices (NO at
1−yes_bid, YES at ask), net entry-leg fee, ONE obs/event, vol>50, and a
**replication demand**: a cell only counts if positive in BOTH time halves.

### The finding — a coherent inverse of everything we killed

**BUY YES on 0.90–0.98 favorites at ~2h lead is positive in every
continuous-underlying class, and in all three time thirds:**

| Class | n | net ¢/ct | win | halves |
|---|---|---|---|---|
| Crypto 0.90–0.98 YES | **670** | **+1.58** | 98% | +1.44 / +1.71 |
| Commodity 0.90–0.98 YES | 37 | +2.62 | 100% | +2.85 / +2.42 |
| Finance 0.90–0.98 YES | 54 | +0.98 | 98% | +0.70 / +1.55 |
| Weather 0.75–0.90 YES | 49 | +2.58 | 94% | +0.90 / +3.48 |
| **Discrete 0.90–0.98 YES** | 50 | **−3.37** | 94% | −0.88 / −7.43 |

Combined crypto+commodity+finance: **n=761, +1.59¢/ct, 98% win, time-thirds
+0.77/+2.04/+1.95 (all positive)**, 78% of 45 days positive, worst day −$—
per 1-contract-each. Under an **arithmetic entry cap** (ask ≤ 0.97 — at the
measured ~2% loss rate, ask ≥ 0.985 needs >99.4% win, i.e. structurally −EV;
this is EV math, not outcome-fitting): **n=400, +2.31¢/ct, thirds
+1.70/+3.13/+2.12**. ~9 candidates/day, median volume 901, median spread 2¢.

**The mechanism is the same favorite-longshot bias, harvested from the other
side, in exactly the classes where fading longshots LOST.** Retail lottery
demand bids up the cheap side → the expensive side gets cheap. In
continuous-underlying markets (crypto/commodity/finance/weather thresholds),
near-settle Gaussian drift toward the already-likely outcome — the exact
mechanism that made our weather/commodity longshot-fades net-negative — is what
PAYS the favorite buyer. And the mirror confirms it: **discrete** markets, where
the drift mechanism doesn't exist, are the one class where favorite-backing is
NEGATIVE — and the one class where longshot-fading works. Two market classes,
two sides, one bias. Complementary, not overlapping (a 0.94 market and a 0.15
market are different tickers), so a favorite-back arm can run beside the fade arm.

**Sub-edges/warnings:** KXSOLD (SOL daily) is the largest drag inside crypto
(n=178, −0.66¢ mean) — watch, don't exclude (that's how the spread-tier
happened). KXINXU and KXZEC15M small negative. ETHD/XRPD/BTCD/15Ms all positive.

### Also from the re-mine

- **Certainty-pct sub-band tweaks inside the 0.10–0.20 fade band are noise** —
  0.10–0.15 vs 0.15–0.20 flip sign across thirds with no stable cut. Leave the
  band alone.
- **The panel's crypto 0.10–0.20 NO cell decays in its third tranche (−6.32¢
  Jul 15–30)** but the ARM's own realized P&L does not (Jul-b: +2.28¢/ct on
  n=386 all-kinds, +2.06¢ on n=348 crypto). The divergence is sampling: the
  panel's one-obs-per-event dedupe picks an arbitrary member (often out-of-band)
  while the arm deliberately trades the in-band member. Arm P&L is the
  decision-relevant number; still, watch July closely.
- **Weather longshot-fade at exactly-2h lead shows POSITIVE in the panel**
  (0.02–0.20 NO, +0.85 to +5.27¢, both halves) — contradicting the arm's
  realized −$—. Likely lead-time: the arm entered anywhere inside the 2h
  window (including near-settle where drift dominates); the panel samples at
  first-crossing ≈2h exactly. UNRESOLVED — do not re-enable weather fades
  without a lead-time-stratified study.

### Live canary (RESUME §2C-5) — ARMED tonight, first fills in

User go-ahead given; `NEARCLOSE_CANARY_ENABLED=true`. First armed cycle
surfaced three real bugs, all fixed: (1) missing `load_dotenv()` in `main()` —
the exact silent-paper trap the docs warn about; (2) `_canary_deployed` read
ACCOUNT-wide exposure (~$— incl. the retail book + manual trades) against the
$— ceiling → permanent silent veto; scoped to own recorded fills; (3) the
slippage benchmark used the stored paper entry, which can be a full cycle
stale — KXRAIN repriced yes 0.17→0.75 in 14 min and produced a nonsense −58¢
"slip". Now re-quotes at order time (`_fresh_no_cost`, handles the
`*_dollars` field gotcha), orders only if still in-band, and records
`live_quote_cost` as the same-instant benchmark. Also: the daemon had NO
logging handler and buffered stdout — both fixed (third silent-null of the day;
the pattern is now a standing lesson).

### Proposed next build (research → code handoff)

**`back_nearclose` — favorite-back paper arm**, sibling of `fade_nearclose`:
buy YES at ask on crypto/commodity/finance markets with yes_mid 0.90–0.98 AND
ask ≤ 0.97 at ~2h lead (+ weather 0.75–0.90 as a tagged secondary cell), 1
obs/event, hold ~2h to settlement, net fee, uniform stake, `kind`/`spread`
instrumentation from day one, loud logging + non-empty-input assertions per
today's lesson. Expected profile if the panel holds: ~+2.3¢/ct on ~0.94 cost
≈ +2.4%/trade at 98% win, ~9 candidates/day — a second compounding substrate,
uncorrelated side of the book from the fade arm.

---

## 2026-08-01 — Canary first-48h: TWO venue-level bugs isolated (dot-in-coid 400s; YES-leg fill prices) → the corrected answer: **fills land AT paper (median slip 0.0¢)**

The armed canary's first two days produced a 49/58 error rate and six
impossible "−70¢ slips". Both turned out to be OUR-stack bugs masking a clean
result — and one of them had been silently crippling every live arm for weeks:

1. **Kalshi v2 orders 400 (`invalid_parameters`) on any `client_order_id`
   containing a dot.** `_make_client_order_id` embedded the raw ticker, and
   fractional-strike tickers (T72.9999, B1.0499500…) contain dots → every
   fractional-strike market was unorderable through the entire live stack.
   Perfect partition (23/23 errors dotted, 2/2 accepts clean); confirmed by
   A/B probe (identical body, dotted coid 400s, sanitized accepted). Fixed by
   sanitizing to `[A-Za-z0-9_-]`. **Retro-implication: the retail canary's
   thin-book "non-fills" since July 2 may partly be this bug, not book
   thinness — its dotted-ticker orders could never have been accepted.**
2. **`average_fill_price` is YES-leg regardless of traded side.** A NO buy at
   0.83 reports 0.17. `execution.LiveBroker.buy` now converts to our-side
   terms per its own documented contract; the 7 recorded canary fills were
   migrated (complemented) in the DB.

**The corrected fill-quality read — the make-or-break number for this arm:
median slippage 0.0¢/contract (n=7; mean −1.0¢, dragged by one −11¢ price
IMPROVEMENT; worst +2.0¢).** Fills land at the paper-assumed `(1−yes_bid)`,
occasionally better (crossing 2¢ into a book whose bid has already moved our
way). At this n it's preliminary, but the direction is the good one: the
+1.6¢/ct paper edge survives execution so far. The full real cycle also
closed: RAIN/ZEC×2/SOL15M×2/NEAR15M/HYPE/XRPMINMON all settled NO (our side),
balance $— → $— over the 48h (mixed with retail-canary settlements).

Paper arm concurrently surged: **+$— lifetime / 90% win / 1,183
resolved** (was +$— two days ago). Retail fade continued degrading
(−$— on 46). Props −$— on 156.

**Watch item:** with the coid fix the canary now reaches the fractional-strike
crypto series (SOLD/SOLE/XRPD…) that dominate the candidate flow — slippage n
should grow fast. Next read: ~1 week, expecting n≳50 before any sizing talk.

---

## 2026-08-02 — `back_nearclose` BUILT; two corrections to the 07-30 favourite-back spec, and a discovery bug that was hiding most of the window

Built the favourite-back arm the 07-30-late entry proposed. Re-verifying the
cell against the (now larger) panel before writing code changed the spec twice,
and building it exposed a third problem in how we see the market at all.

### Correction 1 — the edge is ~half the advertised size, but it IS robust
The 07-30 headline was "+2.31¢/ct (n=400) under an ask ≤ 0.97 cap". Re-running
on the grown panel and de-duplicating per **(asset, close-hour)** rather than
per event — the dependence structure the live fade canary exposed — gives:

| dedupe | n | net ¢/ct | win | time-thirds |
|---|---|---|---|---|
| per event (as researched) | 782 | +1.00 | 97% | +0.92 / +0.67 / +1.43 |
| **per (asset, close-hour)** | **757** | **+1.15** | **97%** | **+1.19 / +0.95 / +1.30** |

De-duplication slightly *improved* the number, and all three thirds are
positive — so the finding replicates, at **+1.15¢/ct**, not +2.31¢. The arm is
built to +1.15¢. (Reassuring: the honest de-correlation didn't just survive
contact with the correct independence unit, it read marginally better.)

### Correction 2 — the weather cell is DROPPED, not tagged
07-30 proposed weather 0.75–0.90 as a tagged secondary cell (+2.58¢). Across
time-thirds it decays **+6.51 / +0.37 / −1.49¢** — the most recent third is
negative. It fails the same replication demand the main cell passes, and
weather already cost the fade arm ~$—. Excluded outright.

### Correction 3 (found at build time) — our discovery only ever saw ~8 minutes of lead
`get_markets_closing_within(2h)` hits the **1000-row API cap**, and results are
closing-soonest-first, so truncation silently kept only the most imminent
markets: **75% of returned rows sat at 8 minutes of lead; the furthest was 68
minutes.** The arm would never have seen most of the 2h window it claims to
trade. Fixed with a two-pass fetch (far band via `min_days`, near band
unfiltered, unioned).

This also explains a long-standing puzzle: `fade_nearclose`'s median hold is
**38 minutes**, not the 2h its docstring implies — same unguarded fetch, same
skew. **`fade_nearclose` was deliberately left alone**: its edge was validated
on its own realized fills at whatever lead it truly used, so "fixing" discovery
there would change the strategy out from under the track record that endorsed
it. Worth a separate, measured decision.

### A lead-bucket cherry-pick, declined
With lead visible, the edge looks strongly lead-dependent:
`0–0.5h +0.56¢ / 0.5–1.0h +0.60¢ / 1.0–3.0h +3.36¢ @ 99% win`. Tempting — and
rejected. Filtering to `lead ≥ 1.0h` gives a higher mean (+2.70¢) but its
**middle time-third is negative** (+4.19 / −0.20 / +4.14, n=131), while
all-leads is positive in every third (n=757). Higher mean, worse evidence —
the exact shape of the spread tier we deleted on 07-30. The arm trades all
leads; revisit if the long-lead advantage survives a full third.

### What shipped
`pm/arms/back_nearclose.py` + `back-nearclose.service` (15-min loop, paper only,
imports neither `kalshi_orders` nor `execution`). Buys YES at ask on
crypto/commodity/finance with mid ∈ [0.90, 0.98) and **ask ≤ 0.97** (EV
arithmetic: at a ~3% loss rate, 0.985 needs >99.4% to break even), uniform $—
one position per event AND per (asset, close-hour), holds to settlement,
settles off raw `status=="finalized"` + `result`. 15 tests.

**Risk shape to remember:** ~97% of trades win ~+1¢/contract and ~3% lose the
whole ~94¢ entry — roughly +11¢ per win against −$— per loss at $— stake.
Long serene stretches punctuated by full-stake losses. **Judge it on mean P&L
per cluster, never on win rate** — the metric that made reactive-hold look like
an edge for weeks.

---

## 2026-08-06 — `back_nearclose` first read: the whole loss was ONE underlying; WTI/NASDAQ excluded, commodity de-correlation fixed

Four days and 263 resolved positions in, the arm read **−$— / −0.62¢/ct**
against a panel prediction of +1.15¢. Decomposing by underlying rather than by
class showed the headline was hiding a clean split:

| Slice | n | net | ¢/ct | win | full-stake losses |
|---|---|---|---|---|---|
| **WTI** | 78 | −$— | **−6.43** | 88% | 9 |
| NASDAQ | 5 | −$— | −15.00 | 80% | 1 |
| **everything else** | **180** | **+$—** | **+2.30** | **97%** | 5 |

**Root cause of the miss — a population mismatch I should have caught at build
time.** The panel that justified this arm was 88% crypto (670 of 761) with
commodity a thin n=37. The live arm came out the inverse: **58% commodity, 31%
WTI**, because the 2h discovery window surfaces mostly commodity hourly settles.
The arm was largely trading a cell the research never really measured, on the
strength of a 37-sample slice — the same thin-slice error that killed the props
fade, propagated into a new build.

**Held to the replication test before acting** (dropping the worst name after
seeing results is exactly what produced the deleted spread tier):

    WTI thirds:             −6.34 / −13.90 / +0.95¢     (negative in 2 of 3)
    everything-else thirds: +3.57 / +1.77 / +1.56¢      (positive in 3 of 3)

WTI's 88% win rate also sits far outside the others' 97% — a different
population, not variance — and it is mechanistically distinct: crude is the most
professionally-traded underlying on the board. **Caveat recorded in-code: WTI's
most recent third is POSITIVE (+0.95¢) and its last 24h ran only −1.11¢/ct**, so
the deficit is real but not currently widening. Treated as a hypothesis under
test, not a settled fact.

**Also fixed: the commodity half of the (asset, close-hour) de-correlation.**
`asset_of` collapsed crypto only, so `KXWTI` / `KXWTIH` / `KXWTI15M` counted as
three independent assets — six same-hour duplicate pairs observed. Added
commodity/FX/index roots (WTI, BRENT, GOLD, SILVER, NATGAS, COPPER, AAAGAS,
EURUSD, USDJPY, NASDAQ, INX, DJI). Honest note: every one of those six dupes was
WTI or NASDAQ, so on *this* history the de-dup changes nothing once they're
excluded — it is preventive for GOLD/SILVER, which publish sibling series too.

**Replay of both changes over the real 263-row record:**
`−$— → +$—`, `−0.62¢ → +2.30¢/ct`, `94% → 97% win`, thirds
`+3.57 / +1.77 / +1.56¢`. That is roughly double the panel's +1.15¢, on the
classes the panel actually validated.

**What to watch:** with WTI gone, ~30% of flow disappears, so accrual slows.
The next honest read is ~100 further clusters. If the excluded names turn
positive over a full further third, the exclusion should come back off.

---

## 2026-08-07 — Gap sweep over held data: the SPOT-DISTANCE gate (real, replicated) + a lookahead bug that briefly manufactured a money-printer; data-collection plan incl. the OTA tuner

User directive: find gaps in data we already hold, then plan new collection.

### Gap B (the find): crypto near-close arms trade threshold markets BLIND to the underlying

Both nearclose arms trade `T`-strike markets (settle ≥ K) using only the book.
But the outcome is ~fully determined by (spot − K) and remaining vol, and we
have Coinbase klines. Joined 5,659 T-strike panel snapshots (BTC/ETH/SOL/XRP)
to hourly candles; z = (spot − K) / (σ_hourly · √h · spot).

**⚠️ Methodology incident, recorded because it's the day's real lesson:** the
first run joined each snapshot to the candle whose START was ≤ ts — a candle
whose close is up to an hour in the FUTURE. That version showed a model-vs-
market strategy making **+11 to +23¢/ct on n≈2,200** — a money printer. With
the join fixed (only fully-closed candles; spot up to 1h stale, never future)
and one obs per (asset, close-hour), the "printer" flipped to **negative at
every threshold**. A naive 1h-stale Gaussian does NOT beat this market. Any
future study joining market snapshots to external time series must prove the
external value was knowable at snapshot time.

**What survives the honest join — the z-gate on the BACK cell** (n=299
de-correlated, halves in brackets):

    z <0.5:    n= 42   −0.11¢  95%   [−0.49/+0.26]
    z 0.5–1:   n= 93   −0.71¢  95%   [−1.85/+0.41]
    z 1–2:     n=124   +2.85¢  98%   [+1.14/+4.55]
    z 2–4:     n= 36   +4.02¢ 100%   [+4.10/+3.94]

Monotone in z, replicates in both halves, and mechanistically it IS the drift
thesis quantified: the favourite-back edge lives where spot is already
comfortably past the strike; the sub-z=1 half of the flow carries none of it.
Live trading polls spot FRESH (vs 1h stale here) so this likely understates.
The FADE cell shows the mirror shape (z −1..−0.5: +9.22¢ n=12; z>−0.5
negative) but n=54 total — suggestive only.

**Proposed (measure-first, no gate yet):** record z at entry on every
crypto position in BOTH nearclose arms + tag would-pass-gate, so the gate is
validated forward on full flow before it cuts any. Commodity slice needs a
spot source first (see collection plan).

### Also confirmed feasible with held data (queued mines)

- **Candlestick panel expansion (Gap A):** `get_candlesticks` carries hourly
  `yes_bid_close`/`yes_ask_close` — executable-ish reconstruction at EVERY
  hourly lead for any settled ticker. The panel's "historical near-close prices
  are unreachable" premise is false at 1h fidelity. A backfill over the settled
  window would multiply the band×class×z sample ~10×+ and re-test the WTI
  exclusion at scale. Overnight, paced tool — build item.
- **Mention base-rate library (Gap C):** `mention_capture` holds **984,690
  quote rows across 2,077 tickers** (556 Trump, 247 MLB, 242 TrumpB, 244
  WorldNews, 89 WNBA, 69 LateNight…) and every settled result is fetchable.
  Nobody has computed: per-series word base rates, pre-broadcast calibration
  (are mention books mispriced BEFORE the broadcast? the anticipation tier
  needs no race), and overround structure over time. Retail-fade's say_mention
  slice lost money fading mention longshots (−$—) — if mention longshots are
  UNDERpriced, the mirror (back the word) is the live hypothesis. Build item.

### Data-collection plan (new data, priority order)

1. **Live spot at entry (crypto)** — poll Coinbase spot in both nearclose
   arms' cycles; store `spot`, `sigma`, `z` per position. Trivial, validates
   the z-gate forward. (Then: gate crypto entries at z≥1 once forward-confirmed.)
2. **Commodity/index quotes for the same feature** — WTI/GOLD/SILVER/INX have
   no spot client. Delayed futures quotes (e.g. yfinance CL=F/GC=F, 10–15min
   lag) are fine at a 2h lead. Unlocks the z feature for the arm's largest
   flow class and would test the WTI exclusion mechanistically (was WTI's
   deficit just low-z entries?).
3. **🆕 OTA tuner nightly corpus (the 4-channel HDHomeRun).** The mention
   detector's binding constraint is precision/recall (71%/53% at WHCD), and
   tuning it needs ground-truth audio+outcome pairs. The tuner gives us, free,
   every night: **late-night monologues** (NBC Tonight Show, ABC Kimmel — 
   monologue ≈ single speaker = attribution tier 1-2; `KXLATENIGHTMENTION`
   already captured, 69 tickers), **MLB national broadcasts** (FOX Saturday —
   booth audio is cleaner than C-SPAN floor feeds), and from September **NFL
   on CBS/FOX/NBC** (announcer-mention volume should dwarf MLB) plus fall
   award shows. Plan: scheduled `mention_listen --hdhr-channel` recordings
   (pinned tuner 3, Plex-safe) building a transcript library scored against
   Kalshi settlements — detector precision curves per content type, word
   base rates per host/booth, zero trading until precision clears a bar.
4. **NWS forecast capture** vs the (liquid) weather books — queue #3, still
   the right measure-first instrument; unchanged priority.
5. **Candlestick panel expander** from (Gap A) — also a collection tool going
   forward (nightly top-up keeps the expanded panel current).

---

## Priority queue (as of 2026-07-09, updated 07-24)

| # | Lead | Turnaround | Status | Next action (owner) |
|---|---|---|---|---|
| 1 | Near-close 0.10–0.20 fade | **~2h** | ⚠️→✅ 07-14 OOS correction: weather/commodity excluded (were the whole −$— bleed); surviving edge = crypto +2¢/ct + discrete retail. **07-30: spread tier FLATTENED** — forward data inverted the panel (tight +1.24¢/ct n=117 vs wide +2.81¢/ct n=255), and 4× stake supplied every ~−$— tail event. Now the LAST fade arm standing after props + retail both failed OOS | **real-fill canary (RESUME §2C) — highest-value experiment we have**; treat the +$—–121 paper number with the skepticism its two siblings just earned |
| 2 | Mention-sniping pipeline | seconds–min | ⚠️ 07-30 SCORED: WHCD repriced in **1s median** (not 7–60s — cascade speed scales with attention); detector 71% precision / 53% recall / **0 cascades beaten**; 6 of 10 "hits" were commentators 3.6h early. Announcer corner (attribution-free) has real windows (39% slow/no-sweep) but **~15× thinner prize** (1.3–3.9k vs 50.6k contracts) | **detector precision is now the binding constraint, not speed** — improve recall/precision on a dedicated booth feed BEFORE any auto-fire arm |
| 3 | Weather vs NWS | **daily** | ⬆ upgraded — books are LIQUID (8k vol, 1¢ spreads) | NWS-vs-book capture instrument + resolved calibration (research→code) |
| 4 | Chart/album leading data | days–weekly | thesis documented | HDD-vs-book check during Ariana week (research) |
| 5 | Earnings mention base rates | days | waiting on Q2 wave (~mid-July) | build base-rate table when lists (research→code) |
| 6 | Ladder monotonicity scanner | instant | spot-checked, mid-only violation | venue-wide executable scan (small) |
| 7 | KXGTEMP yearly climate | slow (Dec) | vol 83k, modelable from YTD anomalies | Berkeley-Earth-vs-book check (research) |
| 8 | Hurricanes / NHC schedule | event-driven | seasonal watchlist (thin until a storm threatens) | revisit on first storm-threat week (Aug–Sep) |
| 9 | Social counters (Twitch/YT) | days | universe too small (~10 events) | opportunistic watchlist |
| ⚠️ | **Base-rate mention trading** | per-episode | DOWNGRADED 07-10 — on DROP content (Love Island) the sweep is informed snipers front-running; "edge" was a mirage. Only viable on LIVE/pre-processing content | re-scope to LIVE-only mention series; verify live-vs-drop per series first |
| — | Live-mention sniping (whisper) | minutes | ⚠️ DROP content dead (batch-transcribed in min); LIVE content only | point `audio_transcribe` at a genuinely-live event (debate/presser/WC), not a streaming drop |
| — | Elections wave (2,148 events) | — | feeds the EXISTING retail fade arm automatically | none — monitor accrual rate |
| — | Commodities (WTI/gold) | — | likely efficient (CME next door; Finance slice dead) | none |
| — | Tennis react | — | ❌ dead (gate read: zero move, n=7,348) | none — closed |
| — | WNBA react | — | ⚠️ lean dead (fat-tail, medians 0) | passive accrual only |

*Everything above stays subordinate to the two OOS validations in flight (prop
fade resolutions from ~Jul 26, retail fade from ~Aug 1) and the gated compounding
layer that follows them.*

**2026-07-30 UPDATE — both OOS validations came back NEGATIVE** (they had been
invisible behind the resolver bug documented in the 07-30 entry):

- **Prop fade: −$— over 150 resolved / 62 independent events, 78% win.**
  The de-correlated backtest was flagged "promising but thin (5–11 events)"; at
  62 events it does not survive. Losses concentrate in WC team-total-goals
  (−$—) and PGA top-10 (−$—). The *only* positive slices are the fast
  round-leader props: `KXPGAR1LEAD` +$— and `KXPGAR3LEAD` +$—, both 100%
  win — i.e. the hours-resolving props the docs always flagged as a separate
  build. **Action: narrow `PROP_SERIES` to round-leader; retire the broad fade.**
- **Retail fade: −$— over 40 resolved, 60% win** — the arm docs/file-map.md calls
  "the FIRST +EV EDGE" (backtest +12% ROI, 86% by-event win). **Caveat: only 40
  of 160 have settled and 26 are `say_mention`** (fast-resolving), not the
  primaries/awards the backtest was built on; the August awards/election closes
  are the real verdict. Corroborating: the 15 resolved **real-money** canary
  fades are −$—, matching the 07-24 "backtest +12% → live ~breakeven" flag.

**Consequence for the compounding gate:** the gate in
`docs/compounding-capital-cycle.md` says build the sizing layer only after an arm
confirms OOS. Two of three fade arms have now *failed* OOS. `fade_nearclose`
(+$— paper, 89% win) is the last one standing and deserves the same skepticism
its siblings just earned — **the real-fill canary (§2C-5 of RESUME) is now the
single highest-value experiment we have**, because "paper-positive fade arm"
has a demonstrated track record of not surviving contact with real fills.

## 2026-08-24 — Near-close retrospective at scale: the founding mispricing is GONE; thesis closed in the current regime

**Why now:** the forward arm's uniform-stake era (opened ≥07-30) reads
−$— over 618 independent (asset, close-hour) clusters, P(mean≤0)=0.61 —
and the CI arithmetic says forward accrual cannot resolve a +0.5%-ROI edge
from zero in under ~2 years. Per the decision framework
(`[[project_nearclose_oos_decision]]`): retrospective before canary, always.

**Method — the founding 2026-07-05 methodology held fixed** so the numbers
are comparable: band by yes-mid at ~2h lead, P&L at EXECUTABLE prices (fade:
buy NO at 1−yes_bid; back: buy YES at ask), net Kalshi fee
(`kalshi_fees.fee_for_trade`, sports rate where applicable), de-correlated to
one observation per (asset, close-hour) cluster, bootstrap CI on the mean.
Two independent surfaces. (Study scripts in session scratchpad; read-only.)

### Surface 1 — `near_close_capture` (real captured quotes, Jun→Aug, 11,742 mkts, vol>50)

Fade 0.10–0.20 by month, mean ¢/contract per cluster [95% CI]:

| month | clusters | mean | CI |
|---|---|---|---|
| 2026-06 | 122 | −1.01 | [−8.1, +5.5] |
| 2026-07 | 132 | **+4.97** | [−1.3, +10.4] |
| 2026-08 | 134 | +1.82 | [−5.7, +8.3] |
| all | 388 | +2.00 | [−1.8, +5.7] |

**No month is CI-positive.** The founding "+1.6¢, 90% by-event, 532 events"
(sampled early July) sits inside the July bump — with hindsight it was a
regime reading, not a structural one. Applying the arm's own exclusions
(Sports/Finance categories + `series_excluded`) gives +0.35¢ [−6.7, +6.4]
overall — the exclusions, fitted on early data, made June/July *worse* and
August better. That is what chasing noise looks like.

Back 0.90–0.98: **+0.10¢ [−1.1, +1.2] over 1,033 clusters** across all three
months. Zero, measured about as precisely as we will ever measure it.

### Surface 2 — `candle_panel` (153,368 settled markets, 07-31→08-24, 115k joined @2h±1h with quotes, vol>50)

**Calibration at 2h lead — the raw mispricing map that founded the arm:**

| band | n | priced | resolved | gap |
|---|---|---|---|---|
| 0.02–0.10 | 16,266 | 0.056 | 0.067 | **+1.1pp** (longshots now UNDERpriced) |
| **0.10–0.20** | **14,400** | 0.147 | 0.151 | **+0.4pp** |
| 0.20–0.35 | 18,467 | 0.269 | 0.252 | −1.7pp |
| 0.35–0.55 | 24,798 | 0.447 | 0.399 | −4.8pp |
| 0.90–0.98 | 3,881 | 0.941 | 0.956 | +1.5pp |

The −6.0pp gap in the 0.10–0.20 band that motivated `fade_nearclose` is
**absent in the current regime** — +0.4pp on 14,400 markets. Executable fade
of the band: every cell ≤ 0; the ARM-SELECTED subset reads **−3.25¢
[−10.8, +4.0]** (162 clusters) — which agrees with the forward arm's own
−4.6¢/cluster over the same period. **Two independent instruments, one
answer.** Back 0.90–0.98 at the ask: −2.99¢ [−4.7, −1.4] (2,176 clusters).

**Nothing survives the spread.** Median spread at 2h lead: 6¢ in the fade
band (9¢ ex-sports), 9–15¢ in the mid bands, 3–5¢ at the favorite end —
against calibration gaps of ±1pp. The seemingly interesting cells die on
contact: mid-band 0.35–0.55 fade (−4.8pp gap!) = −41¢/cluster executable;
backing the underpriced deep longshots = −9¢/cluster. The venue does not
need to be perfectly calibrated when its spreads are 5–30× the mispricing.

### Verdict

**The near-close thesis is CLOSED in the current regime.** The June–July
edge was at best a transient that never reached significance in any single
month; since ~08-01 the band is calibrated and every executable variant is
zero-to-negative on both measurement surfaces and in the forward arm.
`back_nearclose` never had an edge at any point measurable above noise.

Caveats, honestly: the panel window is 24 days (one regime — the same one
the arm lost in); candle bid/ask are hourly closes, not tick quotes; the
capture surface is thin per month (~130 clusters). But three instruments
agreeing (capture retrospective, candle retrospective, forward paper arm) is
as strong as this evidence gets without spending money.

**Recommended actions** (arm retirement = the user's call, per convention):
retire `back_nearclose` (mean +0.8¢ vs ~1¢ ask-side spread — structurally at
noise); retire or indefinitely idle `fade_nearclose` with the OOS ledger
record preserved; the fill canary is moot — there is no paper edge left to
measure fills against. `near_close_capture` + `candle_panel` keep running
either way: they are the instruments that would detect the mispricing
RETURNING (a monthly re-read of the §Surface-2 calibration table is cheap
and pre-registered as the re-entry trigger).

## 2026-08-24 (late) — Mention-market base rates: the crowd already has them; the pricing angle closes

**Thesis under test** (the "corpus priors" candidate from the post-near-close
edge search): mention markets are fan-priced on vibes, so empirical base
rates — the resolution frequency of the same word across past events of the
same series — should beat the market's pre-event price. This is the PRICING
half of the mention thesis; the SPEED half (sniping) died 2026-07-30.

**Method:** 5,815 settled mention markets across 15 series (per-series
settled backfill via the API + the candle panel; span 06-18→08-24). Word
identity = the stable last ticker segment (SCHU, TRAN, ERRO…). Walk-forward
Laplace-smoothed base rate per (series, word) using strictly-earlier events
only, ≥4 priors required. Pre-event executable quotes from
`mention_capture` (1.17M quotes). Study set: ~1,040 markets / 70–83 events /
7 series. Scored two ways: Brier horse race, and gap-trading at executable
prices net fees, clustered per event, bootstrap CIs.

**Result — decisively negative, twice:**

| window | Brier market | Brier base-rate prior | coin |
|---|---|---|---|
| quote 2–24h pre-close | 0.128 | 0.207 | 0.25 |
| quote 6–72h pre-close (strictly pre-event) | 0.158 | 0.207 | 0.25 |

The market beats the prior in **every series with n≥40** (Trump, TrumpB,
Mamdani, MLB announcers, WNBA announcers, World News). Even a 25% blend
toward the prior worsens the market. Trading the disagreements loses at
every gap threshold 0.10–0.30: mean −13 to −44¢ per event, win rates
14–24%, CI-negative or grazing zero from below. **When a frequency prior
disagrees with the pre-event mention price, the market is right ~3 times in
4** — the disagreement is event-specific context (this week's speech topic,
this game's announcers) that the prior lacks, not mispricing.

Methodological catch worth keeping: the first pass used quotes at close−2h,
which for MLB games is MID-GAME — the announcer markets' Brier of 0.004
was the market watching the broadcast, not pre-event skill. The 6–72h re-run
normalizes MLB to 0.162 and changes no conclusion.

**Verdict: whoever prices mention markets pre-event is not naive — the crowd
demonstrably embeds base rates PLUS event context.** This substantially
downgrades the monologue-corpus pricing idea: a transcript corpus is the
same *class* of prior (statistical-historical), and there is no reason to
expect late-night mention markets, if they relist, to be softer than the six
series just measured. The corpus keeps accruing autonomously at zero cost;
nothing gets built on it.

**Sharpened search criterion** (this is the finding's real value): an anchor
must be **event-specific and primary** — something about THIS event the
crowd cannot have yet — not statistical-historical, because the crowd has
history. Of the remaining candidates that survives: NHC scheduled advisories
(primary, event-specific, timestamped) — still untested, season live now.
Round-leader golf calibration: still worth the afternoon, unrelated
mechanism.

## 2026-08-24 (late-2) — Round-leader golf: the "surviving slice" was small-n noise; pre-round is dead, one in-round curiosity logged

**Thesis under test** (candidate C): the fast round-leader props
(KXPGAR1LEAD/R3LEAD) were the only slice that never went negative through the
whole prop-fade era (+$— / +$— on 10–17 resolutions) and were never
isolated. Is there a real calibration edge in round-leader fields?

**Method:** 7,044 settled round-leader markets backfilled (PGA R1/R2/R3, DP
World Tour R1/R2/R3, Champions R1; 53 tournament-round events, 06-18→08-22).
Executable quotes via a targeted candlestick fetch for every member of all 35
pre-window events (~4,700 requests, paced) merged with the candle panel's
in-window coverage — 309k hourly quote rows. Fade at the bid / back at the
ask, net sports-rate fees, clustered per (series, event), bootstrap CIs.

**Two data traps found before any result could be trusted, both now on
record:**
1. **30% of rounds resolve ZERO winners** (16/53 — ties under the "outright
   lead" rule and/or weather-affected rounds resolve every member NO). Any
   fade study that keeps them is mechanically flattered — everyone loses, so
   every NO wins. Excluded.
2. **Candlestick `volume` is unpopulated venue-wide** (0 across categories
   whose market-level volume is six figures) — candle-level liveness
   filtering is impossible; instrument limitation noted for all future panel
   studies. Economic filters instead: hittable books only (spread ≤10¢, bid
   ≥3¢).

**Result, cleaned:**

| lead | phase | fade 0.05–0.20, mean/event [95% CI] | events |
|---|---|---|---|
| close−14h | pre-round | −2.1¢ [−20.7, +14.8] | 20 |
| close−6h | mixed | −10.5¢ [−29.0, +6.5] | 19 |
| close−2h | in-round | **+17.1¢ [+10.0, +24.7], 100% win** | 11 |

The arm's band (0.10–0.32) pre-round: −5.9¢/event. Deep-band calibration gap
pre-round: −0.8pp — nothing, against 1–10¢ spreads.

**Verdict on the thesis: the pre-round round-leader edge does not exist.**
The prop-fade's positive slice was a small-n draw (its own R2LEAD sibling was
−$— on the next 24 — the honest draw from the same distribution).
Candidate C closes.

**The in-round curiosity, logged NOT endorsed:** at ~2h before close
(mid/late round), members still bid at 3–15¢ who essentially never win —
fading those hittable bids scored +17¢/event at 100% win… on **11 events and
~2 hittable trades per event**. This is (a) exactly the sample size that has
fooled this project twice, (b) an in-play "book lags the leaderboard" claim —
the speed class we have repeatedly buried, though golf's slow drift + zero
syndicate presence makes it the least-buried corner of that class, and (c)
capacity of pennies. **Do not build.** If it earns anything, it is a monthly
re-read alongside the near-close re-entry check: same panel, same query,
zero new instruments.

**Search-status ledger after today:** candidates B (mention priors) and C
(round-leader) tested and closed same-day; the surviving candidate is **A —
NHC scheduled-advisory weather** (event-specific + primary, passes the
sharpened criterion, season live). Instrument to be specified when a named
storm exists; nothing built before then.

### Addendum (2026-08-24, later): the storm trigger is INSTALLED — and it fired on first contact

`pm/research/storm_watch.py` + `pm/feeds/nhc_data.py`, cron 4×/day ~35 min
after each NHC advisory slot, paging once per new named storm / new
storm-flavored Kalshi series (state-deduped). **The first live run fired
immediately**: TS Iselle (EPac) and TS Lala (CPac) are active, and TEN
storm-flavored series are already listed — including path markets
(`KXHURPATHHAWAII`, `KXHURPATHNC`) that are exactly the per-storm,
advisory-sensitive class candidate A targets. The pre-registered condition
("a named storm exists") is therefore ALREADY MET; the next step is the
advisory-instrument spec — measure-first, no trading — as a deliberate
session of its own.

The install also flushed out two latent bugs, both fixed: the entry point
did not load `.env` (gotcha #4 — the phone page silently became
log-only), and `notify._ntfy` crashed on any non-latin-1 title (HTTP
headers are latin-1; RFC 2047 encoding now applied when needed — every
prior alert title happened to be ASCII, so this had never fired).

## 2026-08-24 (late-3) — SPEC: `advisory_capture`, the measure-first instrument for candidate A

Pre-registered before any code, per the standing rule. This section is the
contract; the build must not drift from it without amending it here first.

### Thesis under test

After a scheduled NHC advisory publishes, storm-linked Kalshi books reprice
slowly enough that a JSON/text-reading process acting at T0+seconds-to-minutes
could capture spread-adjusted edge. **Null hypothesis (the house default,
7-for-7 so far): they don't** — books either ignore advisories, or the move
is inside spread+fees, or repricing is effectively instant. The instrument
exists to measure which, not to trade.

Why this candidate survived the 2026-08-24 search when nothing else did: the
anchor is **event-specific and primary** (a numbered advisory about THIS
storm, machine-detectable at publication), not statistical-historical — the
crowd demonstrably has history (mention study), but it cannot have an
advisory before it exists. And the counterparty pool is retail weather books
with no syndicate presence (no CME hurricane futures desk cares about a $—
Kalshi book).

### Day-one landscape (measured 2026-08-24, evening)

- Active named storms: TS Iselle (EPac, adv #005), TS Lala (CPac).
- **Per-storm intensity markets exist and are the primary target**:
  `KXHURCAT-26ISELLE-T3/T4/T5` — and the season's entire name list is
  PRE-LISTED (170 open KXHURCAT markets: Dolly, Edouard, Fay, …), so
  coverage starts at formation, not at listing.
- Adjacent classes: `KXNEXTHURDATE` (formation-date buckets — sensitive to
  the Tropical Weather Outlook), season aggregates (`KXTROPSTORM`,
  `KXHURCTOT*`, `KXFIRSTHURRICANE`), season landfall (`KXHURPATHHAWAII/NC`).
  True per-storm PATH buckets are not yet listed; `storm_watch` will page
  when they appear (likely with the first Atlantic landfall threat).
- Books are real but thin: resting bids of 25–560 contracts at 1–6¢ on the
  season markets. The markets endpoint zeroes bid/ask/volume fields
  (known artifact) — the instrument must read ORDERBOOKS, not market rows.
- **The "scheduled" release is fuzzy-early**: Iselle adv #005 has nominal
  issuance 15:00Z but `fileUpdateTime` 14:33Z — products hit the wire ~27
  min before the slot. T0 is therefore DETECTION time, not the schedule;
  the skew itself gets recorded (it is a finding about who else could be
  reading the feed).

### What it captures

1. **Advisory events** — poll `CurrentStorms.json` (breaker `nhc`); a new
   `advNum`/`fileUpdateTime` on any product (public advisory, forecast
   advisory, wind-speed probabilities, discussion) emits an advisory row:
   `(storm_id, product, adv_num, issuance, file_update_time, detected_at,
   raw_text)`. Raw product text (the small `.shtml` bodies) is stored so
   content parsing — max winds, position, forecast track/intensity table —
   can be (re)done offline; the capture never depends on parsing correctness.
2. **Quote paths** — poll ORDERBOOKS (top 3 levels each side, sizes) for a
   watchlist refreshed each cycle: all `KXHURCAT-<active storm>` members,
   `KXNEXTHURDATE` buckets, season aggregates with non-empty books, plus
   anything `storm_watch` newly flags. Adaptive cadence: **15s inside hot
   windows** (T−45min before each nominal slot until T+90min, and
   immediately on any detection), **90s otherwise**. Depth matters (capacity
   question), so sizes are stored, not just prices.
3. **Resolutions** — joined later from the settled API; idempotent.

Storage: `data/shared/advisory_capture.sqlite` (env
`ADVISORY_CAPTURE_DB`), `sqlite_schema` v1, conftest-redirected,
`tests/test_store_schemas.py` entry. One systemd service, SIGTERM-graceful
(R-20), logctx cycle binding, provenance-stamped. **Places no orders and
must never import `kalshi_orders`/`pm.trading.execution`** (the
`mention_listen` load-bearing rule). NHC polling 1/min hot, 1/5min idle;
Kalshi orderbook load ≈ watchlist(≈30–60) × 4/min hot — below
`mention_capture`'s standing load.

### Pre-registered analysis + decision rule

A **material advisory** = intensity forecast change ≥10 kt at any forecast
hour, classification change, or track shift ≥40 nm at 48h (parsed offline
from the stored forecast-advisory text). After enough material advisories:

- Reprice curve: `mid(t) − mid(T0−)` for t ∈ [0, 90 min], per market class.
- **Captured-edge test** (the validated falsifier): executable fill at
  T0+Δ for Δ ∈ {30 s, 2 min, 10 min} — cross the stored book, net fees —
  scored against T+24h mid and against resolution. Clustered **per storm**
  (advisories of one storm are correlated), bootstrap CIs.
- Capacity: contracts fillable inside the stored depth at each Δ.

**Kill rule**: ≥15 material advisories AND (median |Δmid @90min| < 2¢ OR
the Δ=2min captured edge ≤ 0) → thesis dead, instrument stops, post-mortem
in failed-experiments.md. **Graduation rule** (to a PAPER arm only):
CI-positive captured edge ≥ +3¢/contract at Δ=2min over ≥10 independent
STORM clusters. No live anything before a paper arm earns its own OOS
clock. Season ends Nov 30; whatever the count is then, the ledger closes.

### What would make this a waste of time, said now

If the books simply never move on advisories (likely for the thin season
aggregates), the instrument still yields the null cheaply — that is the
same trade every capture instrument here has made. The one outcome that
would be genuinely new is the one the sharpened criterion predicts is
possible: a primary-source release the crowd reads slowly because nobody
with infrastructure bothers. Fifteen material advisories will say.

### Addendum (2026-08-24, night): `advisory_capture` BUILT and LIVE

Built to the spec without drift; the one schema deviation is documented in
the module (books.ts is REAL — a same-second change-after-heartbeat collided
on an INTEGER primary key and `INSERT OR IGNORE` swallowed the change; the
test caught it before deployment). `advisory-capture.service` enabled and
running: first contact loaded 12 baseline advisory products across Iselle +
Lala, built a 115-market watchlist, and is storing change-only book paths
(89/124 first-sweep rows carry live quotes — e.g. KXTROPSTORM-T20 at 6/7¢
with 5×9.8-contract depth, exactly the thin-book capacity picture the spec
expected). First REAL detections expected at the 21Z slot tonight. The
clock on the kill rule (≥15 material advisories) starts now.

## 2026-09-01 — Candidate D: earnings-call mention markets, the release-to-call window (pre-registered, measure-first)

### Why this is not the dead mention thesis

Both closed halves were about **TV** mentions. The SPEED half died 07-30
(cascades complete in ~1s — a sniper already exists); the PRICING half died
08-24 (base-rate priors lose to the market in every series with n≥40 — the
crowd embeds base rates PLUS event context). The closure's stated survival
criterion: an anchor must be **event-specific AND primary**.

Earnings calls have exactly that, and TV never did: **the company's own
press release (8-K) drops ~30–60 minutes before the call**, and prepared
remarks track its language closely. A release that says "AI" forty times
nearly settles "will they say AI" before the call starts. The mechanism
under test is NOT a listening race (that shape is dead) — it is whether the
thin mention book grinds toward the release-implied probability over the
scheduled release→call window, or whether the crowd prices the release
within minutes. Same delayed-discovery family as candidate A: a scheduled
primary-source document, a fan-priced book, a window measured in minutes.

### Day-one landscape (measured 2026-09-01)

- 22 open `KXEARNINGSMENTION*` series (ORCL, AVGO, COST, LULU, DELL, KR,
  CCL, DPZ, …) — the Sep reporting wave. Also same-class: `KXFEDMENTION`
  (Warsh presser), `KXWALLERMENTION` (Fed governor speech) — scheduled,
  transcript-primary, worth capturing under the same sweep.
- `mention_capture` had captured **zero** of them: its hardcoded series list
  still named the settled Q2 banks. That is the second missed wave (Jul-14
  was the first), so the fix is structural: the list is now a seed and
  `discover_mention_series()` sweeps open events hourly for any `*MENTION*`
  series. Passive capture resumes with this commit; no new instrument is
  needed — the 10s quote paths `mention_capture` already stores are the
  study surface.

### Pre-registered analysis + decision rule

For each captured call: T_release = the 8-K/PR publication time (public,
timestamped), T_call = scheduled call start. Questions, in order:

1. **Does the book move in [T_release, T_call] at all?** If mention books
   sit still until the call and then cascade (as TV books did), the window
   does not exist → dead.
2. **Is the release informative?** Score release-text-implied YES/NO (word
   present in the PR / prepared-remarks style) against resolution. If the
   release does not predict resolution materially better than the pre-release
   price, there is nothing to discover → dead.
3. **Captured edge**: at T_release+5min executable quotes, would buying the
   release-implied side net fees be CI-positive per event, clustered per
   call? The mention-pricing null (crowd is right ~3 in 4) is the default.

**Kill rule:** evaluate after ≥8 captured calls with usable paths. Dead if
(1) median |Δmid| over the release→call window < 2¢, or (3) is CI-negative
or grazing zero. **Graduation (paper only):** CI-positive ≥ +3¢/contract
across ≥8 independent calls. No trader, no listener, no LLM scorer gets
built before the capture answers question 1 — that one is free.

### What would make this a waste of time, said now

The likeliest death: earnings mention markets are priced by people who read
8-Ks the second they drop (the listener population is finance-native, unlike
Love Island). If question 1 shows the reprice completing within ~5 minutes
of T_release, that is the TV result wearing a suit, and this closes in one
paragraph. Second likeliest: volume so thin that executable size is <$— per
market — measurable from the same capture, reported alongside (3).

## 2026-09-09 — Candidate D CLOSES: the window is real, the anchor is empty

**Verdict: dead on the pre-registered question-2 rule.** 8 days, 9 captured
earnings calls, zero dollars risked.

### Question 1 PASSED — the release→call window genuinely exists

9 completed calls captured (PANW, DELL, AVGO, HPE, SNOW, CIEN, LULU, ZS,
PATH), 116 settled markets, 88 with clean windows across 7 events. Where the
information enters:

| moment | Brier |
|---|---|
| T_release − 30m | 0.135 |
| T_release + 15m | 0.117 |
| T_call − 2m | 0.119 |
| T_call + 30m | 0.061 |

The book barely reacts to the 8-K — median |Δmid| **1¢**, only 28/88 markets
move >2¢ — and accuracy is flat from release to call start. Then the call
starts and the Brier nearly halves. Tradeable, too: median spread 1¢, 75/88
≤5¢, median eventual volume 3,216 contracts. An oracle buying the true side
at the window's ask nets **+24.4¢/contract, positive in all 7 events**. My
predicted death ("finance-native crowd reads 8-Ks instantly") did NOT happen.

### Question 2 FAILED — and the reason kills the whole family

The 8-K does not predict what gets said. Press releases pulled from SEC
EDGAR (all 9, filing dates matching the calls):

| | n | resolved YES |
|---|---|---|
| word IS in the release | 11 | 36.4% |
| word is NOT in the release | 77 | 42.9% |
| base rate | 88 | 42.0% |

Wrong direction, Fisher **p = 0.75**. Leave-one-event-out calibrated, the
8-K reader scores Brier **0.258 — worse than a constant base rate (0.244)**
and far worse than the market (0.119).

**The mechanism is visible in the coverage number: only 11 of 88 asked words
(12.5%) appear in the release at all.** Kalshi asks about competitor names
(Wiz, CrowdStrike, Databricks), product names (SquareX, Daydrift, PowerLu)
and topical phrases (Aston Martin, Iran / Middle East) — exactly the
vocabulary a terse financial press release never contains. Verified not a
matcher artifact: control probes ("revenue", "product revenue", "fiscal",
"net revenue") all hit.

**So the window is real and empty.** The only anchor that reaches those
words is listening to the call — which is the sniping shape that died
2026-07-30 (cascades ~1s). Pre-call has no anchor; in-call is a dead race.

**The sharpened criterion survives and gets sharper**: an anchor must be
event-specific AND primary AND *semantically overlapping with the thing
being traded*. The 8-K is the first two and fails the third. Check coverage
BEFORE building: "what fraction of the asked questions could this document
even speak to" is a one-hour query that would have killed this on day one.

**Not pursued** (noted, not pre-registered): prior-quarter transcript base
rates for company-specific language persistence. The 2026-08-24 mention
closure already killed base rates across 7 series, and the crowd's pre-call
Brier here is 0.119 vs a 0.244 base rate — it is already pricing these well.
Low prior; would need its own pre-registration.

Capture continues at zero cost (`mention_capture` sweeps the family anyway);
if a materially different anchor appears, the paths are already stored.

## 2026-09-09 (late) — Candidate sweep: 4,045 series screened, no successor to A

Run after candidate D closed, using the criterion its death produced. The
screen is two-dimensional, because the last two closures failed on opposite
axes: **coverage** (does a scheduled primary source state the answer?) and
**window** (is the crowd slow to price it?). D had a window with no
coverage; CPI has coverage with no window. Only the intersection matters.

### Universe

13,061 open events → **4,045 distinct series**. Classified: 833 DEAD by
prior work (sports/esports = the 06-29 structural-efficiency capstone,
mentions = all three angles, crypto price = latency dead + shard 2 unfunded,
elections/index = sharps), 210 CANDIDATE (scheduled primary source), 3,002
unclassified (swept separately for an official-source smell).

| anchor category | series | verdict |
|---|---|---|
| weather (NWS/NHC) | 77 | **tested below — level already priced** |
| econ release (BLS/BEA) | 41 | coverage perfect, window ~0 (most-watched numbers in finance); many are 2036-dated duration bets |
| central bank | 37 | same — FOMC is not a fan-priced corner |
| commodity report (EIA/USDA) | 18 | energy/ag desks are already there; thin |
| space (launch manifests) | 16 | plausible window, 19 open events — too thin |
| health/regulatory (FDA PDUFA) | 8 | specialist-dominated, long-dated |
| aviation ops (TSA/BTS/FAA) | 8 | obscure and published-with-lag, but ~9 events |
| geophysical (USGS) | 5 | **coverage is ZERO** — USGS reports quakes, it does not forecast them. Resolution source ≠ prediction source |

Novel shape found in the unclassified sweep: **corporate disclosure metrics**
(`KXGOOG`/`KXMETAA`/`KXLMTA` headcount, `KXCMG` restaurant count). Coverage is
excellent — the 10-Q literally states the number, on a scheduled date — and
the corner is obscure. Killed on size, not on mechanism: 2 events per series.
Logged as the one shape worth revisiting if Kalshi ever lists it at volume.

### Weather, tested properly — and it is already priced

The log carried a twice-flagged, never-executed item ("capture NWS forecast +
book; does the book deviate at T−12h/T−2h"). Executed now.

**Level (snapshot, ~24h lead).** Kalshi's ladder is a clean 6-market
partition (4 buckets + 2 open-ended tails). Market-implied mean vs the NWS
forecast for the *station the rules actually name*:

| city | station | NWS | implied mean | gap | Σp |
|---|---|---|---|---|---|
| New York | CLINYC Central Park | 86 | 86.2 | +0.2 | 1.06 |
| Chicago | CLIMDW **Midway** | 78 | 78.9 | +0.9 | 1.00 |
| Denver | CLIDEN | 89 | 89.1 | +0.1 | 1.00 |

**The book tracks the public forecast to about 1°F.** A first pass showed a
tempting +2°F systematic bias that was **entirely my own measurement error** —
it truncated the ladder (buckets only, Σp = 0.43–1.03) and used city-centre
grid points instead of the named stations. Chicago resolves at *Midway*, not
O'Hare; LA at downtown/USC, not the airport. Corrected, the bias vanished.
Recording this because it is the exact shape of a false positive this project
has paid for before: the artifact looked like the finding.

**Dynamics (retrospective, our own panel).** 4,515 settled weather markets /
161,546 candles already on disk — no instrument needed. Fading the longshot
(buy NO at the executable price, net fees, held to resolution), clustered per
city-day with a 2,000-sample bootstrap:

| lead | YES band | clusters | mean | 95% CI |
|---|---|---|---|---|
| 1.5–2.5h | 0.02–0.10 | 78 | −0.002 | [−0.041, +0.025] |
| 3–5h | 0.02–0.10 | 142 | −0.008 | [−0.037, +0.016] |
| 10–14h | 0.10–0.25 | 420 | −0.022 | [−0.051, +0.005] |
| 20–26h | 0.02–0.10 | 726 | −0.019 | **[−0.032, −0.008]** |

Every cell straddles zero except the 1-day longshot fade, which is
**CI-negative** — it loses. **This RESOLVES the standing "weather longshot-fade
at exactly-2h lead shows POSITIVE in the panel — UNRESOLVED" item: it was
noise.** n=86 across 78 clusters, mean −0.002, CI dead-centred on zero. Same
shape as the round-leader "surviving slice" (08-24) — a subgroup of a closed
thesis that looked alive until it was clustered properly.

### Verdict

**No successor to candidate A.** The sweep killed the naive weather thesis on
both axes, resolved an open log item, and found one novel-but-too-thin shape.
A (NHC scheduled advisories) remains the sole live candidate: 9/15 material
advisories, basin quiet, season ledger closes Nov 30.

Process note: this whole sweep cost one session because the coverage test is
cheap and runs first. Candidate D took 8 days to die; its own killer question
("what fraction of the asked questions could this document even speak to")
would have taken an hour. Coverage first, window second, tradeability third.

## 2026-09-09 (late-2) — "Any edge at all?": the venue-wide scan. One real bias, exactly spread-sized

Prompted by "is there any edge, even small or questionable — consider
real-time news, the stock market". Widest net yet: **3.26M candle rows,
197,515 settled markets, 41,521 event clusters**, every category, every price
band, both directions, fee-aware at executable prices, clustered per event
with bootstrap CIs and a split-half check.

### The scan

182 cells. **Three came back CI-positive AND split-half stable**: Crypto
0.55–0.75 YES (+11.6%), Mentions 0.02–0.10 NO (+3.4%), Elections 0.02–0.10 NO
(+2.9%). All three were then interrogated, and all three fail:

- **Crypto 0.55–0.75** is a selection artifact. Crypto markets at a 3h lead
  are bimodal — 845 markets sit ≤0.02 and 449 sit ≥0.98; only 93 are in the
  0.55–0.75 band at all. That band is a rare "still uncertain" state, n=82,
  and the effect size (+11.6%) is implausible on a liquid underlying. Also
  shard 2, unfunded.
- **Elections 0.02–0.10** is n=33. Dismissed on size.
- **Mentions 0.02–0.10** survived every cheap check — 86 event clusters, top
  event only 6% of P&L, median entry spread 1¢, both halves positive — and
  then died on the expensive one. See below.

### Why the Mentions cell died, and what it taught

The cell had **1 YES hit in 175 markets**. Expanding to all leads gives
**n=627 with 28 hits — a 4.47% realized rate, Jeffreys CI [3.05%, 6.30%]**.
The 3h slice was a lucky tail draw; split-half passed because *both* halves
undersampled the tail, which is the failure mode split-half cannot catch.

The calibration curve is the real result, and it is strikingly consistent —
across **every** lead band and price band, realized YES lands **3–4.5pp below
the market mid**:

| lead | band | mkt mid | realized | gap | breakeven |
|---|---|---|---|---|---|
| 1–4h | 0.06–0.10 | 0.078 | 0.043 | −3.5pp | 0.030 |
| 4–10h | 0.06–0.10 | 0.079 | 0.036 | −4.3pp | 0.038 |
| 10–24h | 0.06–0.10 | 0.078 | 0.033 | −4.5pp | 0.041 |
| 24–30h | 0.10–0.20 | 0.151 | 0.113 | −3.8pp | 0.116 |

**The bias is real, stable, and almost exactly the size of the spread plus
fees.** The breakeven column sits on top of the realized column; cells land
a few tenths of a point either side of zero, which is why the aggregate
looked positive in one slice and negative in another.

This also explains the live `fade_retail` result (−$— / 200 positions)
without contradiction: that arm entered at a **median cost of 0.79** — i.e.
multi-day leads where a 6¢ market is genuinely live — and its 0.05–0.10 band
resolved YES **27.8%** of the time. Long-lead longshots and near-close
longshots are different animals; the panel cell was never the trade that lost
the money.

### Direct answers to the two prompts

- **Stock market / Financials (8,627 settled) and Economics (2,775)**: no
  CI-positive cell in either, at any band or direction. The best is
  0.90–0.98 YES at +1.4% / +1.8%, not significant — the same
  favourite-longshot bias seen from the favourite side. **Commodities are
  negative everywhere** (−2.5% to −3.7%). Previously I asserted "sharps" for
  these categories during the series sweep; that was an assumption, and it is
  now a measurement.
- **Real-time news**: unchanged and still negative for *racing* — free RSS
  detection lag was measured at a ~150-minute median, which is why
  `news_capture` is stopped. Nothing in this scan revives it.

### The one strategically useful thing

We now have a **measured, venue-wide, persistent 3–4pp mispricing that is
exactly consumed by execution cost.** That reframes the constraint: the
binding problem is no longer "find a signal" — we have one — it is "stop
paying the spread." A maker who earns the spread instead of paying it turns
a 3–4pp bias from breakeven into profit. That is precisely the
`docs/compounding-capital-cycle.md` / Kalshi-MM-program direction, and it is
the first time the log can point at a *quantified* edge that the maker side
would capture. **Caveat, honestly**: market-making was simulated before
(`maker_sim.py`, 06-29) and failed — but in the liquid sports corner, without
rebates, and against a different bias. This is a different corner (fan-priced
tails) with a now-measured edge. Worth a fresh pre-registered simulation
before anything is built; NOT worth a live arm on today's evidence.

## 2026-09-09 (late-3) — PRE-REGISTRATION: maker simulation in the fan-priced tail

Written BEFORE running, per the log's own rule. Prompted by the venue-wide
scan (late-2): a real 3–4.5pp longshot bias that spread+fees exactly consumes.

### Why this is not a repeat of maker_sim.py (06-29)

The prior sim died and its conclusion stands *for what it tested*. Two
differences make this a distinct experiment:

1. **Different corner.** That sim replayed the **120 busiest MLB markets**
   (1.7M trades) — liquid, HFT-watched, with informed in-game flow. This one
   runs on **fan-priced mention tails**, where the counterparty is buying a
   lottery ticket, not reacting to a play.
2. **Different objective.** That sim was a *maker who flattens*, marked to
   the near-future mid, so any adverse mid-move is a loss. This is a **maker
   who holds to resolution**: we are not scalping the spread, we are
   acquiring a measurably mispriced position more cheaply. Adverse selection
   only hurts here if filled markets genuinely resolve YES more often — which
   is a different (and directly measurable) question than "did the mid move".

### Substrate

`mention_capture` — 1.69M quote rows at ~10s resolution with **real
cumulative volume** (the candle panel's volume is unpopulated venue-wide, so
it cannot support a fill model). Outcomes joined from `candle_panel.markets`.

### The simulated quote

Rest a NO bid one tick inside — equivalently a YES ask at
`A = yes_ask(t0) − 0.01`. Acquisition cost if filled is `1 − A`, versus a
taker's `1 − yes_bid`; the saving is `spread − 1¢`. Hold to resolution.

### Fill model (the crux)

Filled when, at some later snapshot, **Δvolume > 0 AND last ≥ A** — a print
at or above our ask level while we are the best offer. Queue is handled
conservatively: require Δvolume ≥ 10 contracts before crediting a fill, and
cap our fill at 1 contract. Fees are the standard Kalshi formula on both
sides — **no maker rebate assumed**, so the MM program can only improve this.

### Pre-registered decision rule

- **Primary metric**: mean P&L per contract, clustered per event, 2,000-sample
  bootstrap CI.
- **DEAD if** the per-event CI includes zero.
- **DEAD if** adverse selection eats the edge: `YES rate | filled` exceeds the
  unconditional YES rate in the same band by more than the spread captured.
  This is the specific way the 06-29 sim died and it gets its own test.
- **Graduation**: CI-positive with ≥40 event clusters, AND filled-vs-
  unconditional YES rate statistically indistinguishable → then and only then
  a paper arm. No live arm on a simulation, ever.

### What would make this a waste of time, said now

The likeliest death is that fills cluster exactly where the longshot is
coming true — we get lifted because someone knows the word is about to be
said. That is the 06-29 finding wearing different clothes, and the
filled-vs-unconditional test is designed to expose it in one number.

### RESULT (same day) — maker simulation is DEAD on both pre-registered criteria

Ran exactly as pre-registered above. Substrate: 518,753 `mention_capture`
quote rows joined to outcomes, 1,535 settled markets, 80 event clusters.

**Headline: the maker's advantage exists only under an optimistic fill model,
and dies monotonically as the fill model becomes realistic.**

Best cell (YES mid 0.02–0.10, entry ≤6h lead, 604 quoted opportunities,
68 event clusters). "Queue proxy" = contracts of flow required before we
credit a fill — a stand-in for queue depth and for our own size:

| queue proxy | fill % | maker per-trade | 95% CI | adverse sel. |
|---|---|---|---|---|
| 10 | 79% | **+1.90¢** | [−1.18, +4.64] | +1.5pp |
| 50 | 67% | +0.08¢ | [−4.65, +3.57] | +2.4pp |
| 200 | 46% | −5.81¢ | [−12.84, +0.19] | +6.2pp |
| 1000 | 14% | **−29.3¢** | [−43.8, −15.5] | **+26.7pp** |

(Taker on the same 604 opportunities: −1.75¢, CI [−4.26, +0.47].)

**Both kill criteria fire.** Every CI includes zero at any plausible fill
assumption. And adverse selection — the specific way the 06-29 sim died —
rises from +1.5pp to **+26.7pp** as the required flow grows, far exceeding
the 1–3¢ of spread captured.

**The mechanism, plainly:** on a 5¢ longshot, the flow big enough to sweep
through your resting offer is precisely the flow that knows the word is about
to be said. You are filled on the ones that stay worthless and missed on the
ones that come in. The +1.9¢ at the generous end is what a one-contract order
at the front of a thin queue earns; it is not a strategy, it is a rounding
error with no capacity.

**Maker rebates do not save it.** The simulation already assumed none, and
the gap at realistic queue depth is ~6¢/contract — orders of magnitude beyond
any Kalshi MM rebate.

### What this falsifies — including one of our own conclusions

The late-2 scan proposed that **"the binding constraint is execution cost,
not signal."** That is now **FALSIFIED**. It is not that we pay too much to
trade the 3–4pp bias; it is that the spread *is the fair price* for the
information risk carried by whoever lifts us. The bias and the spread are not
two independent quantities where beating one wins — they are the same
quantity viewed from two sides.

This extends the 06-29 capstone rather than contradicting it. That finding
("the spread is fair compensation for adverse selection we'd be on the wrong
side of") was established in the liquid MLB corner and might plausibly have
failed to generalise. **It generalises.** Fan-priced tails, where the
counterparty was supposed to be a lottery-ticket buyer, show the same
signature — milder in level, identical in shape.

**Decision: no paper arm, no live arm, nothing built.** The maker direction in
`docs/compounding-capital-cycle.md` and the MM rung of the pro ladder are
closed for this corner at this size. Revisit only with either a genuine
information advantage (so we are the informed side of the fill) or a venue
where flow is demonstrably uninformed — neither of which we have.

## 2026-09-09 (late-4) — RESULT: candidate A is DEAD. The last thesis closes.

Ran the pre-registered analysis (§2026-08-24 late-3). **Kill rule fires on its
first clause.** Full post-mortem in `docs/failed-experiments.md`.

**Gate**: 47 material advisories across 10 storms vs a gate of 15 — and note
the earlier "9/15" reported for weeks was **my error**, counting only the wind
third of a three-part criterion. Two parsing traps found on the way, both
recorded because they each produced a plausible-looking wrong answer:
NHC forecast valid times sit on a 12-hour grid while advisories are 6-hourly,
so **consecutive advisories share zero common valid times** and a naive track
comparison silently returns 0 shifts; and the storm-name regex matched
"TROPICAL STORM CENTER LOCATED NEAR…", naming every storm "Center", until it
was anchored on the `FORECAST/ADVISORY NUMBER` header.

**Reprice curve** — median |Δmid| after a material advisory: **0.0000 at every
horizon out to 90 min** (mean 1.4¢, 15% move ≥2¢). Restricted to genuinely
uncertain markets (0.05–0.95; only 18% of the sample was pinned, so this is
not a settled-market artifact) the 90-min median rises only to **0.5¢** —
still a quarter of the 2¢ threshold.

**Captured edge** at Δ=2min, crossing the stored book net fees, clustered per
storm: **+1.14¢/contract, CI [−3.46, +7.89]** — spans zero, short of the +3¢
graduation bar, 5 storm clusters against a required 10. Capacity ~55 contracts
at the touch.

**Why, and it rhymes with candidate D.** The information is not absent, it is
concentrated and unpredictable: 15% of pairs move ≥2¢ and those move a median
5.5¢ (max 41.5¢) — but nothing tells us in advance *which* market that will
be. Structurally, a hurricane market is a **cumulative** claim ("will it ever
reach Cat 3") while an advisory is an **incremental** update; most advisories
confirm the standing forecast, which the book already holds. Eight days ago
the earnings 8-K failed because the market had already read the document.
Candidate A fails the same way. **Coverage and window are not independent —
a document legible enough to trade on is legible enough that the market has
already traded on it.** That is now the sharpest statement of the venue
problem this log contains, and it should be the first question asked of any
future candidate.

**Consequences**: `advisory-capture.service` stopped per the pre-registration
(state retained; restart with `systemctl --user start`). The season ledger
closes now rather than Nov 30. **This was the last live edge candidate.** The
project has no open thesis for the first time since it started.

## 2026-09-16 — Three structurally-different candidates, all dead. The spread finding is now overwhelming.

After candidate A closed, the project had no open thesis. This scan
deliberately avoided the shape that has failed every time (read a public
document faster/better than the crowd) and tested three ideas that differ in
**kind**. Two were standing never-executed items in this log.

### 1. New-listing drift — DEAD (log line 132, flagged and never run)

*"Do fresh listings systematically misprice (anchor-at-50, longshot drift)?"*
Measured over **367,024 settled markets** with their opening two-sided quote
(93% of markets have their full life inside the candle window, so "first
observation" genuinely is near listing).

The calibration bias is real and consistent — at listing, YES is **underpriced
in every band**: +1.0pp at 2–5¢, +2.5pp at 5–15¢, +3.4pp at 15–35¢, +2.1pp at
35–65¢. And **no direction is tradeable**: buying at the opening ask, held to
resolution, net fees, clustered per event — **0 of 12 cells CI-positive**,
every one negative, most decisively so (e.g. 15–35¢ YES: −2.63¢, CI [−3.34,
−1.90]).

**The "anchor at 50" effect is an artifact of EMPTY BOOKS.** 35% of the
0.35–0.65 bucket sits at exactly mid 0.50 with a **median spread of 0.990** —
no quotes at all, and my `(bid+ask)/2` manufactures a "price" from nothing.
Those resolve YES 45%. Restricted to genuinely tight books at 0.50, realized
is **0.507** — perfectly calibrated. There was never a mispricing, only an
absence of a price.

### 2. Cross-venue Kalshi vs Polymarket — no gap where it counts (zero prior hits in this log)

Both clients have existed all along and the comparison was never made. On the
flagship comparable market (Sept 2026 FOMC, **$—M** Polymarket volume):

| outcome | Kalshi | Polymarket |
|---|---|---|
| Hike 25bps | 0.85 / 0.86 | 0.855 |
| No change | 0.14 / 0.15 | 0.135 |
| Hike >25 / Cut 25 / Cut >25 | ≤0.01 ask | 0.006 / 0.0015 / 0.0005 |

Identical to within the 1¢ spread. This is one market — the most-watched one,
where agreement is most expected — so it is not a closure of the idea, but it
is the natural first place a gap would show and there is none. **Also note we
cannot execute on Polymarket at all** (no wallet auth built), so a gap would
be information, not income, until that changes.

### 3. Multi-outcome field overround — normal bookmaker margin, DEAD

Pure arithmetic, no information required. 23,166 multi-leg events (3–20 legs)
snapshotted ~6h pre-close; 10,717 resolved with exactly one winner.

- Buy the whole field at ask: median cost **1.040**, mean **1.380**, for
  something that pays exactly 1.00. Net of per-leg fees: **−0.4481/field**,
  CI [−0.467, −0.429], only 6.9% profitable.
- Sell the whole field at bid: you receive a median **0.970** against a 1.00
  liability. Negative before the per-leg fees.

Both sides of the field are priced with a conventional overround. Worth
recording separately: **only 46% of multi-leg fields resolve with exactly one
winner** — 3,105 had ZERO and 2,439 had two or more. The golf tie-rule trap
generalises across the venue, and any future field-based idea must handle it.

### The convergent finding, now on four independent measurements

| measurement | bias found | tradeable? |
|---|---|---|
| near-close longshot (09-09) | 3–4.5pp | no — equals spread+fees |
| maker simulation (09-09) | price improvement 1–3¢ | no — adverse selection +26.7pp at size |
| new-listing drift (09-16) | 1–3.4pp | no — 0/12 cells CI-positive |
| field overround (09-16) | 4–38% gross | no — that IS the margin |

**Kalshi's spread is an accurate price for Kalshi's own inefficiency.** Every
bias we can measure is real, and every one is the same size as the cost of
correcting it. This is not a series of unlucky results; it is one structural
fact observed four ways, and it extends the 06-29 capstone from "no timing
edge" to "no *pricing* edge accessible from either side, at listing, at
near-close, in the field, or across venues."

**What is NOT ruled out** (and is now the entire remaining space): a genuine
information advantage — something known that the market does not know, from
domain expertise or non-public-but-legal data — as opposed to a faster or
cleverer read of what everyone can see. Every dead thesis in this log is the
latter. None has ever been the former.

## 2026-10-21 — Preregistration: funded near-term source-overlap census

**Question.** Does any currently open, exchange-index-0 Kalshi series have a
repeatable information source that fixes its contract outcome before trading
closes, while the book still offers executable uncertainty at small size?
This is a *coverage* census, not a claim that public-source speed is an edge.
The settlement-source lag tested 2026-10-13 is dead for the verified CPI,
employment, claims, SOFR, and daily NWS-final-report cases. Do not revive
those by changing the lead or using preliminary values. The threshold-ladder
arbitrage is also dead after the all-pairs, volume-aware review; by-date
nesting is the same arithmetic.

**Universe and statistic, fixed before live query.** Page the public Kalshi
open-markets endpoint with `mve_filter=exclude` until the cursor is exhausted;
if interrupted, report the universe as censored, never as complete. Keep only
`exchange_index=0` and scheduled `close_time` within seven days of the fetch.
Group by `event_ticker` and series ticker. Record market count, independent
events, median spread, 24-hour traded volume, and the rule's named settlement
source. A one-page, unfiltered API connectivity probe made immediately before
this registration is **not** part of the sample; it returned 100 combo markets
on shard 1 and no candidate decision.

**Coverage gate.** A candidate needs (1) at least 20 independent events per
30 days in the observed/current listing cadence, (2) a documented source
publication timestamp earlier than the market's last trading time, (3) a
contract rule showing that the published value fixes the outcome without a
later revision or discretionary branch, and (4) at least one market with
positive 24-hour volume, a two-sided quote, spread at most 10 cents, and an
index-0 best-book level for at least one contract. Treat empty or zero-depth
books as missing, not 50-cent probabilities. Estimate event/month only from
the event schedule; do not count strikes as independent hits. A series whose
source or timestamp cannot be verified is `NEEDS RULE/TIMESTAMP DATA`, not a
coverage hit.

**Economic test for survivors, preregistered now.** At the first source-known
quote after publication, buy the winning side at YES ask or NO ask `1-yes_bid`;
subtract `ceil(0.07*P*(1-P)*C*100)/100` dollars per C contracts (0.035 for
sports, which are excluded from this screen). Require a contemporaneous
book depth walk for ten contracts or less and compute net dollars/event.
No fill is assumed from a zero-volume historical candle. Cluster inference
by independent underlying event (and by common source release where several
series share one release). Minimum 30 events across at least two months and
95% event-cluster bootstrap lower bound above zero for a claim of edge.

**Materiality kill rule.** Kill a candidate if its coverage rate is below
20 independent opportunities/month, or if feasible depth × net edge implies
less than $10/month at ten contracts or fewer, even when a rare positive quote
exists. If no series survives the coverage gate in the complete live census,
verdict is `NO CURRENT CANDIDATE`; do not build a trading arm. Any survivor is
`MEASURE NEXT`, not `ALIVE`, until the prospective economic test passes.

**Execution constraint.** Read-only public API and SQLite `mode=ro` only.
Scripts and results live in `research/edge_2026_10_21/` for git review. No
order or trading module imports, no writes under `data/`, no deploy changes.

**Second preregistration, written before incentive query.** A current public
liquidity reward could change the failed 2026-09-09 maker arithmetic because
it pays for resting time even without a fill. Fetch every active public
liquidity program, retain only ordinary index-0 markets, and inspect the
program's period reward, target size, discount factor, and live two-sided
depth. Compute a deliberately optimistic ten-contract payout bound as
`reward_per_day * min(1, 10/target_size)`; this assumes our order receives
full distance credit, all snapshots qualify, and no competing orders beyond
the minimum target, so it is an upper bound, not expected profit. The
coverage/materiality kill rule is fewer than five independent active
index-0 markets with a bound of at least $1/day each and existing target-size
depth on both sides, or fewer than $—/day total across those markets. If it
passes, verdict is `MEASURE NEXT`: a prospective resting-order exposure and
fill-adverse-selection measurement is required before any edge claim. Do not
place an order in this read-only session. Program reward changes, eligibility,
and missing orderbook depth are recorded as limitations, not filled in.

**Incentive follow-up preregistration, before detailed book query.** The
first screen may overstate reward because it ignores the $1 minimum payout,
one-sided score normalization, price priority, and large incumbent orders at
the best level. For every index-0 market with optimistic ten-contract period
reward at least $— and target depth on both sides, fetch the full live book.
Count a ten-contract bid as *plausibly scorable* only if there is a one-cent
price improvement available without crossing the opposite bid, or the best
same-side level has fewer than one-fifth of target contracts ahead. Set the
optimistic one-sided reward ceiling to half the earlier period bound and
discard it below the $1 payout minimum. Kill the incentive idea for tonight
if fewer than five independent events survive or the sum of those optimistic
ceilings is below $— per active program period. This is still a necessary
condition; winning requires measured reward credits minus adverse selection.

### Measurements (2026-10-21 evening, America/Chicago)

| Rank | Candidate and losing counterparty | Closest dead work / concrete difference | Funded coverage and cost | Verdict |
|---|---|---|---|---|
| 1 | YouTube artist weekly maximum: a resting counterparty might quote before a new official daily value appears. | Public-source speed tests died; this is a monotone **already hit** contract, not a forecast of a future hit. | `KXYTVIEWSW`: 16 events/7d, eight with volume/depth-gated quotes; 13 liquid strikes, 2¢ median quoted spread. Source timestamp lookup ~1 hour. | **NEEDS SOURCE/TIMESTAMP DATA**: no documented publication time for the exact New York-IP artist daily value, so no source-known executable quote can be labeled. |
| 2 | Liquidity reward: the exchange pays for qualifying resting orders even if the counterparty never fills them. | 2026-09-09 maker simulation charged +26.7pp adverse selection; a cash reward is a distinct offset, subject to size and scoring rules. | 6,860 active public programs; 1,325 funded markets with a 10-contract optimistic bound ≥$1/day; API/depth audit ~1 hour. | **DEAD at 10 contracts tonight** by the registered event-materiality rule below. |
| 3 | Album equivalents: a published Luminate value could settle before traders move. | Same settlement-source-lag idea tested 2026-10-13. | Five events/7d had liquid quotes, but these rules specify Luminate as of 10:00 ET Sunday, the scheduled close time. | **DEAD on window**. |
| 4 | Rotten Tomatoes scores after review publication. | Same public-primary-source idea as the NHC/NWS/8-K tests. | Three events/7d had liquid quotes; sampled rules close at the stated score-check time. | **DEAD on frequency/window**. |

The completed non-combo public-market cursor walk took 163 pages and saw
162,043 open markets: 127,851 index 0; 38,469 index 0 closing within seven
days; 7,613 after excluding known sports and crypto series; 1,987 with a
two-sided spread ≤10¢; 1,139 also with positive 24-hour traded volume and
best-level depth ≥1 contract. These span 292 series. No published timestamp
was found that proves the exact `KXYTVIEWSW` settlement value is available
while its book is open. [Kalshi's market rule](https://kalshi.com/markets/kxytviewsw/highest-daily-view-count-this-week/kxytviewsw-ye26oct25)
names YouTube Charts and a New York IP, but gives no publication clock;
[YouTube's Charts help](https://support.google.com/youtube/answer/9014376)
describes daily chart updates and weekly artist charts without a timestamp for
this exact artist daily-view figure. This fails the *documented source time*
gate. It is not a negative P&L estimate.

For incentives, the [public API](https://docs.kalshi.com/api-reference/incentive-programs/get-incentives)
returned 6,860 active liquidity programs in seven pages. Using the SDK's
centi-cent reward unit, 1,335 programs had the preregistered optimistic
10-contract bound ≥$1/day; 1,325 were on index 0 and all 1,325 books were
retrieved. Only 501 met target size on **both** sides at the depth snapshot.
The 501-market sum of **optimistic** reward-per-day bounds was $—;
it is not realizable profit and must not be interpreted that way. Most were
$100-period / 1,000-target programs, where a one-sided 10-contract score has
at most a roughly $0.50-period ceiling before competition and the $1 minimum
payout. The [scoring and payout rules](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)
require both-sided target depth, normalize each side separately, exclude
unqualified snapshots, and pay nothing below $1 per program.

Fourteen markets had initial ten-contract *period* bounds ≥$— and two-sided
target depth. The detailed price-priority check retained five markets across
**three independent events** (Trump approval one, Ontario rain one, Onion Act
one), with a summed one-sided **optimistic ceiling** of $— across different
program periods. It fails the registered minimum of five independent events;
one event with three strikes is one exposure. The 15-minute FX programs had
a $50 pool and 300-contract target, giving $— before the one-sided
normalization and about $0.83 after it, below the $1 payout minimum for a
10-contract order. Exact reward would be lower when price distance,
competition, excluded snapshots, or fills matter. The first detailed-book
probe exposed ascending price arrays; the script was corrected to select the
numeric best price and the reported result is from the corrected rerun. That
rerun occurred after some 15-minute programs expired; the FX payout-minimum
failure follows from the published pool and target arithmetic even if those
books had remained open.

**Verdict: no proven executable edge tonight.** The source candidate has no
verified publication timestamp; the incentive candidate fails the stated
small-order event-materiality rule. We made no trades. No event-level P&L
sample exists here (`n=0`), so a P&L confidence interval is **not applicable**;
claiming an ALIVE edge from these coverage bounds would be unsupported. A
timestamped New York-IP capture of the actual YouTube artist daily-view figure,
paired with volume-confirmed index-0 quotes, is the specific missing
instrument. Kill it if it yields fewer than 20 independent source-known
events/month or less than $10/month feasible net at ≤10 contracts; require
30 events over two months and an event-cluster 95% lower CI above zero for an
edge claim. It is not an instruction to trade at the current quote.

**Reproduction.** Run the read-only scripts
[`scan_live.py`](../research/edge_2026_10_21/scan_live.py),
[`scan_incentives.py`](../research/edge_2026_10_21/scan_incentives.py),
[`measure_incentives.py`](../research/edge_2026_10_21/measure_incentives.py),
then [`check_incentive_priority.py`](../research/edge_2026_10_21/check_incentive_priority.py).
Their timestamped outputs are `live_census.json`, `active_liquidity_programs.json`,
`incentive_depth_screen.json`, and `incentive_priority_screen.json` in the same
directory. The intermediate `incentive_markets_cache.json` avoids re-fetching
market metadata. Public data and rewards change quickly; rerun before using
the counts for any later decision.

## 2026-10-21 — Follow-up: can we buy the favorite the instant a market lists?

**Operational answer: yes, detection is possible.** Kalshi's authenticated
[`market_lifecycle_v2` WebSocket channel](https://docs.kalshi.com/websockets/market-and-event-lifecycle)
emits `created` with `market_ticker`, `event_ticker`, and `exchange_index`.
The [orderbook channel](https://docs.kalshi.com/websockets/orderbook-updates)
then supplies a snapshot and deltas for selected tickers; the public
[`GET /markets`](https://docs.kalshi.com/api-reference/market/get-markets)
endpoint with `min_created_ts` is a slower read-only fallback. A creation
message is not a price. The favorite is identifiable only after a real book
exists, and an empty 50¢ mid with a 99¢ spread is not an opportunity. For a
field, define the favorite from the first contemporaneous executable quotes
across its legs, not from the eventual winner or a later price.

**Economic answer: the measured opening-quote strategy is DEAD.** The
2026-09-16 new-listing test used the first available two-sided quote on
367,024 settled markets. YES was 1–3.4pp cheap versus mid at listing, but
buying at the opening ask, paying fees, and clustering by event left **0/12
price-band cells with a positive 95% CI**. The tested 15–35¢ YES cell lost
2.63¢ per contract, CI [−3.34¢, −1.90¢]. The separate favorite-hold experiment
in `docs/failed-experiments.md` was also retired. Merely reacting faster to
the listing has no demonstrated payoff and should not trigger an auto-buyer.

**Narrow unresolved question:** the candle panel is hourly, so its first
two-sided observation cannot rule out a quote lasting seconds immediately
after creation. This is a *measurement gap*, not an open edge thesis; prior
speed experiments make its prior low. The next useful work, if
pursuing it, is a **read-only, index-0 opening-seconds capture** in the
research directory. For each non-combo creation, store exchange and receive
timestamps, rules, event/series, the first nonempty book and each update in
the first five minutes, displayed bid/ask and depth, and matching trade
prints. Preserve empty-book and vanished-quote cases. Treat a favorite as
known only when the complete relevant field has contemporaneous executable
quotes; exclude ambiguous tie/void fields or score them separately under
their actual rules. Do not import order or trading modules and do not place
orders for this measurement.

**Decision rule before any trading work:** buy only at the displayed favorite
ask in the replay, use actual available depth up to ten contracts, subtract
the size-scaled fee, and cluster by event. Do not claim an edge before at
least 30 independent events across two months and a positive event-cluster
95% lower confidence bound. Kill the sub-minute idea if the observed cadence
and executable net profit imply less than **$10/month** at the ~$— bankroll,
even if one transient quote looks profitable. Record how many first quotes
were still present after the measurement latency; no hypothetical fill from
a quote that vanished. Only a passing, repeatable measurement would justify
considering an execution arm.

## 2026-10-25 — Preregistration: open-interest confirmation on the candle panel

**Why this is not another speed test.** Every closed thesis in this log
measured a faster or cleverer read of *public information* — and the capstone
null result (four independent measurements) is that Kalshi's spread is the
fair price for its own inefficiency. Open interest is a different object. It
is not a price and not a news feed: it is the **count of contracts currently
held**, i.e. a trace of who has committed capital and how much. It has never
been studied in this project. The `open_interest` column exists in 23.9M
candles and appears in no prior entry here and in no code outside the fetcher
and its tests.

**Question.** At a fixed lead before close, does a sharp rise in open interest
*accompanied by* a price drift carry information about the outcome **beyond
the contemporaneous mid**, by a margin larger than the half-spread plus fees?

**Mechanism being tested.** Open-interest change is direction-blind on its own
(every opened contract has a buyer and a seller), so raw ΔOI cannot be a
signal. The testable object is the standard futures *confirmation* pattern:
rising OI with upward drift means new capital is opening on the YES side;
rising OI with downward drift means new capital is opening on the NO side.
The claim under test is that this commitment is informed more often than the
mid already reflects. If OI is merely noise or merely a liquidity proxy, the
cells will straddle the mid and this dies at stage 1.

**Universe, fixed before any label is read.** `candle_panel.sqlite`, markets
with `fetched_at IS NOT NULL` and `resolved_yes IS NOT NULL`. The panel is
already volume-selected (`MIN_VOLUME = 50`) and that selection is **not**
removable retrospectively; record it as a survivorship limitation, not a
neutral filter. Open interest exists only from 2026-10-08 (the 10-13 fix in
`c6f43ef` began populating it; the historical backfill never carried it), so
the window is 2026-10-08 → fetch date. Exclude categories `Sports` and
`Crypto`: those sit on exchange_index 3 and 2, whose account balances are
**$0**, so no result there is executable without an intra-exchange transfer.
Category is our own bucket, not the venue's shard field — using it as a shard
proxy is a stated limitation. Report Sports/Crypto separately and label any
finding there `TRANSFER-GATED`, never tradeable.

**Signal definition, fixed now.** At lead `L` hours before `close_time`, take
the candle nearest `close - L` within 1h (`load_panel` semantics). Let `W` be
the lookback window. Define, using only candles at or before that point:

- `oi_prev` = open interest at the candle nearest `close - (L + W)`
- `oi_growth = (oi_now - oi_prev) / max(oi_prev, 25)`
- `mid = (yes_bid + yes_ask) / 2`; `drift = mid_now - mid_prev`
- signal fires only when `oi_growth` is in the **top tercile** of its
  `band × lead` cell and `|drift| >= 0.01`; side = YES if `drift > 0`, else NO

Require `yes_bid > 0 and yes_ask > 0` at the entry candle and a quoted spread
`<= 0.05`. Terciles are data-derived, which is why the OOS split below is
mandatory rather than optional.

**Primary cells, fixed now to control multiplicity.** With 3,505 independent
events the risk is not power, it is that something looks significant by
chance. The **primary** family is a single lead, `L = 12h`, `W = 6h`, top
OI-growth tercile, 5 price bands (5–15, 15–35, 35–65, 65–85, 85–95¢) × 2
drift signs = **10 primary cells**, Holm-corrected across those 10. Leads
`L ∈ {2, 24}` and the low/mid terciles are **exploratory**: they may motivate
a future preregistration and may not support any claim made here.

**Stage 1 kill rule (information).** Per cell, compare the realized YES rate
to the mean contemporaneous mid, with a 95% bootstrap CI clustered on
`event_id` (one event with many strikes is one exposure, not many). A cell
survives only if `|realized - mean_mid| >= 0.02` **and** its Holm-corrected
95% CI excludes zero. The 2pp floor is not arbitrary: a typical 2–3¢ spread
plus the Kalshi fee means a deviation under 2pp can never be executable, so a
statistically real sub-2pp cell is still economically dead. If no primary cell
survives, verdict is **DEAD on information** and no arm is built.

**Stage 2 (executability), preregistered now.** Survivors only. Buy the
signalled side at the contemporaneous executable quote — YES at `yes_ask`, NO
at `1 - yes_bid` — and subtract `ceil(0.07 * P * (1-P) * C * 100) / 100`
dollars for `C = 10` contracts. Cluster by `event_id`. A claim requires a 95%
event-cluster bootstrap lower bound **above zero** in net dollars per event.

**Hard ceiling on what stage 2 can ever prove.** Candles are hourly *closes*
of bid and ask and carry **no depth**. Stage 2 is therefore an upper bound on
executability — it assumes a fill at the displayed quote for 10 contracts,
which the data cannot confirm. A positive stage 2 is `MEASURE NEXT` (forward
depth capture required before any order), **never** `ALIVE`. This is the same
rule that governed every prior candidate here.

**Out-of-sample split, mandatory.** Markets with `close_time` on or before
2026-10-16 are IN-sample; after 2026-10-16 are OUT. Report both separately.
A claim requires the same cell to survive in **both** halves with the same
sign. A cell positive in one half only is noise, and is reported as noise.

**Materiality kill rule.** Kill if the observed signal frequency and feasible
net imply less than **$10/month** at 10 contracts or fewer against the ~$—
bankroll, even if a cell is statistically clean. Also kill if fewer than 30
independent events fire in the OUT half.

**Execution constraint.** Read-only: SQLite `mode=ro`, no venue calls, no
writes under `data/`, no import of `kalshi_orders` / `execution`, no deploy
change. Script and timestamped output live in `research/edge_2026_10_25/`.
No order is placed in this session under any outcome.

### Measurements (2026-10-25 evening, America/Chicago)

**Verdict: DEAD on information at the preregistered stage-1 rule. No arm is
built. No order was placed.** Open interest is now a *measured* dead end
rather than an unexamined column, which is the result this entry set out to
produce either way.

**Coverage, and why it is far thinner than the census suggested.** The
structural pre-count found 17,667 funded-shard markets with OI and a ≤5¢
spread. The study requires OI at **both** the entry candle (`close − 12h`) and
the prior candle (`close − 18h`), and that pair collapses the funded sample to
**2,363 rows**. Totals by family: 23,130 rows at the 12h lead (2,363 funded),
33,836 at 2h (3,772 funded), 9,714 at 24h (930 funded). Open interest has
existed only since 2026-10-08, and funded-shard markets are a minority of the
panel, so the nine primary cells hold n = 15–61 each. This is a materiality
failure independent of the statistics.

| Primary cell (lead 12h, top OI tercile) | n / events | realized YES | mean mid | dev (pp) | Holm p |
|---|---|---|---|---|---|
| 5–15¢ up | 21 / 21 | 0.000 | 0.083 | −8.29 | **0.0000** |
| 5–15¢ down | 61 / 46 | 0.049 | 0.086 | −3.66 | 0.808 |
| 15–35¢ up | 40 / 36 | 0.200 | 0.245 | −4.45 | 1.000 |
| 15–35¢ down | 44 / 32 | 0.205 | 0.230 | −2.53 | 1.000 |
| 35–65¢ up | 42 / 39 | 0.524 | 0.501 | +2.25 | 1.000 |
| 35–65¢ down | 41 / 29 | 0.439 | 0.490 | −5.13 | 1.000 |
| 65–85¢ up | 56 / 48 | 0.696 | 0.743 | −4.65 | 1.000 |
| 65–85¢ down | 15 / 12 | 0.800 | 0.724 | +7.60 | 1.000 |
| 85–95¢ up | 37 / 30 | 0.919 | 0.903 | +1.64 | 1.000 |

**The one surviving cell is an artifact, and is reported as one.** 5–15¢/up
has a Holm-corrected p of 0.0000 and a dev CI of [−9.43, −7.26]pp — and it is
worthless, for four independent reasons, any one of which is disqualifying:

1. **Degenerate-outcome bootstrap collapse.** Realized YES is **0 of 21**.
   When the outcome vector is constant, a cluster bootstrap of `y − mid`
   resamples only the variation in `mid`, so the interval measures how much
   the *price* varied, not how uncertain the *rate* is. The honest interval
   for 0/21 is Jeffreys [0.0000, 0.1115], which **contains** the mean mid of
   0.083. There is no evidence of mispricing here.
2. **Wrong sign for the hypothesis.** The signal fired "OI growth + upward
   drift ⇒ YES," and YES realized *below* mid. The cell contradicts the
   mechanism under test; it does not support it.
3. **Already-falsified content.** A cheap longshot resolving YES less often
   than mid is the favourite-longshot / retail-longshot bias, retired after a
   −$— live result. It is not a new finding and it was not tradeable then.
4. **Loses money anyway.** Stage-2 net at the executable quote is
   **−$0.99/event** for 10 contracts. Even taking the deviation at face value,
   buying it is negative.

It also fails the preregistered OOS requirement outright: the IN half had
fewer than 10 observations, so only the OUT half exists (13 events) against a
required minimum of 30.

**Exploratory leads confirm noise rather than signal.** The 2h and 24h
families produce "significant" cells in **both directions** — 65–85¢/up at
+14.28pp [+4.29, +22.46] but 35–65¢/up at 24h lead at −42.68pp on 11 events,
and 85–95¢/down at +8.46pp from a degenerate 12-of-12 all-YES cell with the
same collapse described above. Nothing holds its sign across leads. Across all
26 measured cells, stage-2 net was **9 positive / 17 negative, median
−$0.13 per event**. There is no consistent money and, per the preregistration,
exploratory cells cannot support a claim regardless of how they came out.

**What this adds to the capstone.** The four prior measurements showed Kalshi's
spread is the fair price for a *faster or cleverer read of public information*.
This is the first measurement of a **positioning** variable rather than an
informational one, and the mid already contains it: knowing that capital
committed, and which way price drifted while it committed, does not beat the
quote. Open interest is not a back door into informed flow at this fidelity.

**Reusable methodological trap, worth more than the result.** *A bootstrap CI
computed on `outcome − prediction` silently becomes a CI about the prediction
when the outcome is degenerate.* It produced the tightest, most significant
interval in the entire run from the single least informative cell. Any future
study in this repo that scores a rate against a price must check for a
constant outcome vector and fall back to a Jeffreys/Wilson interval on the
count. This is the same family of error as the standing rule *count hits, not
observations*.

**Revival condition, stated precisely so it is not a vague "rerun later".**
`candle-panel.service` keeps running, so funded-shard OI coverage grows by
roughly 2,300 paired rows per 17 days. This entry is closed, not paused: a
re-test requires a **fresh preregistration** and may only proceed when the
primary family holds at least 30 independent events per cell in **both** OOS
halves — roughly 12× the current funded coverage, i.e. not before ~2027-02 on
the observed accrual rate. Do not re-run this script against a slightly larger
panel and read the cells again; that is the multiplicity failure this design
exists to prevent.

**Reproduction.** Read-only script
[`oi_confirmation.py`](../research/edge_2026_10_25/oi_confirmation.py), output
`oi_confirmation.json` in the same directory. SQLite `mode=ro`, no venue
calls, no writes under `data/`, no trading imports. Seeded RNG (20261025) so
the bootstrap is reproducible.
