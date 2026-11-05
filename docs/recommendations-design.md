# Recommendations — design doc

Living spec for adding "recommendation" features to the app, ordered by ROI and risk. The honest framing for this whole effort is: most "alpha" claims for individual users are illusions, and the highest-value features are the ones that *prevent losses* rather than *generate gains*. The order in which we build matters more than what we build, because building alpha strategies before having a backtest framework means we can't tell which ones actually work.

## Status legend

- ✅ Shipped
- 🟢 Ready to build
- 🟡 Deferred — wait for a prerequisite
- 🔴 Not yet — deliberately not built

---

## The problem statement

A "recommendation" in a prediction-market context is, implicitly, a probability estimate that disagrees with the market consensus. So every recommendation has the form *"the true probability is closer to X than the market's current price of Y."* If the recommendation is wrong, the user loses money.

This means **honest recommendation features need two things side by side**:
1. A method that produces probability estimates
2. A measurement of whether those estimates have been historically accurate

Building (1) without (2) is how recommendation engines lose people money. The Calibration page (already shipped) is the foundation for (2); the **Backtest framework** (Tier 3 below) extends it to systematic strategies.

---

## Approaches

### A. Risk-management "anti-recommendations" (🟢 Tier 1)

The least sexy and most valuable. Most retail traders lose money to sizing/exposure errors, not bad picks. The app already has all the data needed; it just doesn't act on it.

- **Calibration-adjusted Kelly** — if the user's Brier score in category X is mediocre, auto-cap Kelly in that category. Sharp = full Kelly allowed; mediocre = ½ Kelly; bad = ¼ Kelly; worse-than-market = match the market.
- **Position-size limits** — hard ceilings: max N% per market, max M% per category, defaults configurable.
- **Drawdown circuit breaker** — after a >X% peak-to-trough drawdown, surface a warning and (optionally) hide the Kelly bet-size recommendations until the user acknowledges.
- **Sector concentration warning** — flag when exposure to one category exceeds a threshold.

These produce *anti*-recommendations: "don't bet more than X" and "don't bet right now." Boring. Bulletproof. Build first.

### B. Arbitrage detection (🟢 Tier 2)

The only honest "the market is wrong" claim. Multi-outcome gap detection is already in the Scanner. Extend to:

- **Cross-market constraints**: markets that should mathematically relate (e.g., "Trump wins 2028" + "Harris wins 2028" + ... must sum to ≈ 1). Flag inconsistencies. Requires Polymarket event groupings.
- **Spot-implied vs. Polymarket** for crypto markets: if BTC futures imply 12% probability of $150k by some date and Polymarket says 30%, surface the gap.

Real edge but tiny in dollars on liquid markets; gets arbed away in seconds on the biggest ones. Worth building because it's also a satisfying test of the scanner framework.

### C. Backtest framework (🟡 Tier 3 — gates Tier 4+)

Before trusting any new strategy, replay it through historical CLOB price data and report Sharpe / max drawdown / hit rate / sample size. **This is the gate.** No new alpha strategy ships without a measured backtest.

Inputs:
- Strategy function: takes a market snapshot, returns `(direction, probability)` or `None`
- Historical price data: from `pm.get_price_history` for each market over the backtest period

Outputs:
- Hit rate (% of signals that ended up in the right direction)
- Sharpe ratio, max drawdown, total P&L
- Per-category breakdown
- Sample-size warnings (a strategy with 8 trades isn't a strategy)

### D. External-data correlations (🟡 Tier 4 — needs C)

Where serious quants get edge. Compare external data sources to Polymarket prices and surface gaps. Free data sources, real edge potential.

| Category | Source | Notes |
|---|---|---|
| Sports | ESPN, SofaScore APIs (live win-prob models) | Easiest to start; well-defined win prob models exist |
| Politics | 538, RCP, polling aggregators | Slow-moving, often heavily discounted |
| Crypto | Coinbase / Binance / Kraken spot + futures | Crypto markets on Polymarket often lag spot moves |
| Macro | FRED, ECB data | For Fed/FOMC markets |
| Weather | NOAA / Open-Meteo | For weather markets |

Format the output as flags, not commandments: *"Polymarket prices Heat over Lakers at 35%; ESPN BPI says 48%; 13pp gap."* User still decides.

Each integration ships **only after a backtest** showing the source's predictions have historically beaten the market on that category.

### E. LLM-based qualitative scoring (🟡 Tier 4 — needs C, proceed with care)

Have an LLM read recent news for each open market and produce a probability estimate. Log every one. Score with the existing Calibration system.

**Hard requirement**: shipping the LLM as a recommendation source requires its calibration data to show it beats the market on that category. Until then, LLM forecasts are visible to the user (with their own Brier shown alongside) but not surfaced as recommendations. LLMs are bad calibrators by default; treat their output as candidate evidence, not signal.

Cheap to implement. Easy to fool yourself with if not gated on Brier.

### F. Order-placement automation (🔴 Tier 5 — deliberately not yet)

Auto-execution against Polymarket order book given a recommendation. Requires:
- Polymarket API key + Polygon wallet
- Robust retry / partial-fill / cancel logic
- Strict caps that can't be overridden by buggy strategy code

Out of scope for v0.x. The app stays advisory; the user executes manually. Once Tiers 1-4 have run live for months and calibration data shows real edge, *then* discuss automation. Not before.

---

## Tier order and why

| Tier | What | Why this order |
|---|---|---|
| 1 | Risk management (A) | Highest ROI, lowest snake-oil. Uses existing data. Failure mode is "warns too much," not "loses money." |
| 2 | Arbitrage detection (B) | Honest signals. Small but real. Validates the scanner framework. |
| 3 | Backtest framework (C) | **Gates everything after this.** Without it, we can't tell which strategies work. |
| 4 | External data + LLM scoring (D, E) | Where real money lives. Only ship strategies whose backtest beats the market. |
| 5 | Order automation (F) | Months out. Risky. Only after Tiers 1-4 have a long track record. |

Most projects skip 1 and 3 because they're boring. That's why most projects lose their users money.

---

## Tier 1 scope (current work)

### Goals
- Cap Kelly automatically by category based on the user's historical accuracy in that category
- Enforce per-market and per-category position limits
- Warn on drawdown
- Warn on sector concentration

### New module: `pm/ledger/risk.py`

```python
@dataclass
class Settings:
    bankroll: float                # user's total trading capital
    max_kelly_fraction: float      # global cap (e.g. 0.25)
    max_market_position_pct: float # e.g. 0.05 (5% of bankroll)
    max_category_exposure_pct: float  # e.g. 0.30
    drawdown_pause_pct: float      # e.g. 0.15

# Storage: settings.json (gitignored)
def load_settings() -> Settings: ...
def save_settings(s: Settings) -> None: ...

# Pure functions
def calibration_adjusted_kelly_multiplier(brier: float | None) -> tuple[float, str]:
    """Brier → (multiplier in [0, 1], explanation string)."""
    # ≤ 0.10 → 1.0 (full Kelly OK)
    # 0.10 – 0.20 → 0.5 (half Kelly)
    # 0.20 – 0.25 → 0.25 (quarter Kelly)
    # > 0.25 → 0.0 (match market; no Kelly bet)
    # None → 0.5 (half Kelly default until calibration data exists)

def current_drawdown(df_snaps: pd.DataFrame) -> dict:
    """Returns {peak, trough, current_drawdown_pct} from the snapshot log."""

def category_exposure_pct(df_pos: pd.DataFrame) -> dict[str, float]:
    """{category: pct_of_total_cost_basis}."""
```

### Extension to `pm/ledger/calibration.py`

```python
def brier_score_by_category(
    df_forecasts: pd.DataFrame,
    df_markets: pd.DataFrame,
) -> dict[str, float]:
    """Brier score per market category. Joins forecasts to markets by
    condition_id, then groups by category. Bins with < 5 resolved forecasts
    return None (sample size too small)."""
```

### UI changes

**Kelly Sizer page:**
- For the selected market's category, display the user's Brier score (if computable from resolved forecasts).
- Apply the calibration-adjusted multiplier to the displayed Kelly fraction.
- Always show both: raw Kelly (mathematical optimum) and adjusted Kelly (with explanation, e.g. *"½ Kelly applied — your Brier on Sports is 0.14 (decent)"* or *"¼ Kelly applied — your Brier on Crypto is 0.23 (mediocre)"*).
- If multiplier is 0, surface a clear note: *"Your Brier in this category exceeds 0.25 — the recommendation is to match the market price, not bet your own number."*

**Portfolio page:**
- Drawdown banner if current drawdown > `drawdown_pause_pct`.
- Sector concentration warning if any category > `max_category_exposure_pct`.

**New Settings access:**
- Either a 🛡️ Settings page or a sidebar expander where the user can adjust bankroll + thresholds. Simpler v1: a sidebar expander.

### Tests
- `calibration_adjusted_kelly_multiplier`: each Brier bucket, None input
- `current_drawdown`: empty snapshots, single snapshot, monotone-rising, monotone-falling, V-shaped
- `category_exposure_pct`: empty df, single category, multiple categories
- `brier_score_by_category`: forecasts join to markets correctly; under-5 categories return None

### Out of scope for Tier 1
- The Settings UI for editing thresholds (use defaults + env overrides for v1)
- Position size limit *enforcement* in the Buy form (warning only; no hard block — that needs more thought about user workflow)

---

## Tier 2 sketch (next)

`scan_cross_market_arb(df_markets, df_events)` — finds groups of related markets via `event_id` and flags pricing that violates `Σ prices = 1` or other implied constraints. Outputs a DataFrame like the existing scanner functions, with columns `event_title`, `markets`, `sum_prices`, `gap`, `total_liquidity`.

Spot-vs-implied comparison would come as a separate scanner: needs external data integration → realistically Tier 4 territory once we have that scaffolding.

---

## Tier 3 sketch (gates Tiers 4+)

`pm/research/backtest.py`:

```python
def run_backtest(
    strategy: Callable[[MarketSnapshot], Signal | None],
    markets: list[str],            # condition_ids to test on
    start: datetime,
    end: datetime,
    initial_bankroll: float,
) -> BacktestResult:
    ...
```

`BacktestResult` has `total_pnl`, `sharpe`, `max_drawdown`, `hit_rate`, `n_trades`, `per_category_breakdown`, `equity_curve`.

A Backtest page lets the user pick a strategy + date range, runs it, displays the results.

---

## Open questions

- Should the drawdown circuit breaker just warn, or actually hide the Kelly bet-size numbers until acknowledged?
- Should the calibration-adjusted Kelly fall back to half-Kelly when there's *no* calibration data, or should it warn and recommend zero?
- Where should `bankroll` live? Settings file (persistent) or Kelly Sizer input (per-session)?
- For Tier 4 external data, should there be a single "edge candidates" page that aggregates all sources, or separate scanner-style outputs per source?
