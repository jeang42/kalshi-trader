# API surface — key function signatures

Split out of `docs/file-map.md` on 2026-08-23 (R-25). This is the signature reference
for every public entry point in the root modules and the sidecar packages,
annotated with the contracts and gotchas that are not visible from the type
alone.

**Keep it in sync.** If you change a public signature, change it here in the
same commit — `docs/file-map.md` points here and carries no signatures of its own.
Grouped by module, in dependency-ish order: venue clients first, then the
per-profile ledgers, then the analysis layers, then the sidecars.

```python
# polymarket_client.py
parse_json_field(val) -> list                            # safe JSON-string → list
get_markets(limit=500, active=True, order="volume24hr") -> pd.DataFrame
get_market(condition_id: str) -> dict                    # uses condition_ids filter
# kalshi_client.py — closing-soon + lookup helpers (Kalshi-specific)
get_markets_closing_within(max_days, *, limit=500, min_days=0.0) -> pd.DataFrame
# Active markets closing within max_days, sorted closing-soonest first.
# Uses /markets with min_close_ts/max_close_ts filters. Categories
# joined from /events scan with keyword fallback when an event sits
# beyond the scan window.
lookup_markets_by_id(market_or_event_id: str) -> pd.DataFrame
# Resolves a ticker, event-ticker, or kalshi.com URL into a normalized
# DataFrame. Single ticker → one row; event ticker → every sub-market.
# Used by the Kelly Sizer "🔎 Look up by ticker/URL" expander to bring
# specific markets into scope when they aren't in the volume-ordered
# default fetch.
get_events(limit=500, active=True, order="volume24hr") -> pd.DataFrame
enrich_categories_from_events(df_markets, df_events) -> pd.DataFrame
get_closed_markets(limit=100) -> pd.DataFrame             # for Tier 3 backfill
get_closed_markets_in_range(*, since_days, until_days=0.0,
                             max_items=10000,
                             progress_callback=None) -> pd.DataFrame
# Kalshi-specific: cursor-paginated /markets?status=settled with
# min/max_close_ts filters. Use for big historical backtest samples.
# KALSHI QUIRK: the query param is `status=settled` (returns 400 on
# "finalized" even though the response status IS "finalized" — Kalshi
# has separate vocabularies for the query and the response field).
# Adds: resolved_yes (bool|None), closed_time, closed_time_dt
market_price_at_offset_before_close(token_id, closed_at, lead_hours,
    fidelity=60, window_days=90) -> float | None
get_market_calibration_data(df_closed, lead_hours=24, max_markets=100,
    progress_callback=None) -> pd.DataFrame
# Cols: predicted_prob, resolved_yes, actual (0/1), brier_term, lead_hours
get_price_history(market_id: str, days=30, fidelity=60) -> pd.DataFrame
# kalshi_client.py — candlestick + executed-trades feed (Kalshi-specific)
get_candlesticks(ticker, days=30, fidelity=60) -> pd.DataFrame
# Same fetch as get_price_history but keeps the columns it discards:
# yes_bid_close, yes_ask_close, volume, open_interest. Use for flow /
# liquidity gating; get_price_history when you only need [timestamp, price].
get_trades(ticker=None, *, limit=1000, min_ts=None, max_ts=None, cursor=None) -> dict
# One page of the public executed-trades tape (markets/trades). Raw dict
# with "trades" + "cursor". ticker=None → cross-market firehose. No auth.
get_all_trades(ticker, *, since_ts=None, until_ts=None, max_trades=100000,
               progress_callback=None) -> pd.DataFrame
# Cursor-paginated, normalized. Cols: trade_id, ticker, ts (UTC dt),
# ts_unix, count, yes_price, no_price, taker_side ("yes"/"no" aggressor).
# Both a live feed AND a historical backfill source (min_ts/max_ts).
get_order_book(token_id: str) -> dict         # raw API response
parse_order_book(book: dict) -> pd.DataFrame   # cols: side, price, size, cumulative_size
order_book_stats(df_book) -> dict              # best_bid/ask, spread, total sizes, level counts
get_midpoint(token_id: str) -> float | None
scan_narrow_spreads(df, max_spread=0.02, min_liquidity=500) -> pd.DataFrame
scan_price_divergence(df, min_divergence=0.05, min_liquidity=500) -> pd.DataFrame
scan_multi_outcome_gaps(df, min_deviation=0.03, min_liquidity=200) -> pd.DataFrame
scan_cross_market_arb(df, min_markets=3, min_total_liquidity=1000,
                       plausible_band=(0.7, 1.3), min_deviation=0.02) -> pd.DataFrame
# Tier 2: groups markets by event_id, sums YES prices, flags deviations from 1.0
# Mutual exclusivity NOT verified automatically — plausibility band filters
# obvious non-arbs; user must read sub-market questions to confirm
kelly_sizing(market_price: float, your_probability: float) -> dict
# kelly dict keys: edge, direction ("YES"|"NO"), kelly_fraction,
#   half_kelly_fraction, expected_value
# Result is fully JSON-serializable (no lambdas). For dollar amounts:
#   bankroll * result["kelly_fraction"]

# portfolio.py — transaction-based
add_buy(question, condition_id, outcome, shares, price) -> dict
add_sell(question, condition_id, outcome, shares, price) -> dict
record_transaction(tx_type, question, condition_id, outcome, shares, price) -> dict
delete_transaction(index: int) -> bool
add_position(...)          # backward-compat alias for add_buy
remove_position(index)     # backward-compat alias for delete_transaction
get_transactions() -> pd.DataFrame
get_positions() -> pd.DataFrame
get_realized_pnl() -> pd.DataFrame
enrich_with_current_prices(positions_df, markets_df) -> pd.DataFrame
add_snapshot(cost_basis, current_value, unrealized_pnl, realized_pnl,
             open_positions=0) -> dict
get_snapshots() -> pd.DataFrame
maybe_snapshot(cost_basis, current_value, unrealized_pnl, realized_pnl,
               open_positions=0, min_interval_hours=1.0) -> bool

# calibration.py
add_forecast(question, condition_id, outcome, market_price, your_probability,
             *, source="user", notes="") -> dict
# source tags forecasts so the Calibration page can compute per-source Brier
# ("user", "llm:<model>", "crypto_spot"). Legacy records without a source
# field are read as source="user" — no on-disk migration.
brier_by_source(df) -> {source: {"brier": float|None, "n": int}}
auto_resolve_pending(closed_lookup) -> list[str]
# closed_lookup: {condition_id: resolved_yes_bool}. Already-resolved
# forecasts are skipped silently — never overwritten. Returns the IDs
# updated. Caller (Calibration page) builds the lookup by unioning
# get_closed_markets() across providers; this stays venue-agnostic.
record_resolution(forecast_id: str, outcome_yes: bool) -> bool
delete_forecast(forecast_id: str) -> bool
get_forecasts() -> pd.DataFrame
brier_score(df: pd.DataFrame) -> float | None
calibration_bins(df: pd.DataFrame, n_bins: int = 10) -> pd.DataFrame
summary_stats(df: pd.DataFrame) -> dict
# Market-side (Tier 3a) — same shape, different columns (predicted_prob, actual)
market_brier_score(df) -> float | None
market_brier_by_category(df) -> {category: brier | None}
market_calibration_bins(df, n_bins=10) -> pd.DataFrame

# alerts.py
add_alert(question, condition_id, outcome, direction, threshold) -> dict
# direction: "above" | "below"; threshold ∈ [0, 1]
delete_alert(alert_id: str) -> bool
reset_alert(alert_id: str) -> bool          # un-trigger
get_alerts() -> pd.DataFrame
check_alerts(df_markets: pd.DataFrame) -> list[dict]   # newly-triggered only
summary_stats() -> {"total", "active", "triggered"}

# risk.py — Tier 1 of the recommendations roadmap
@dataclass class Settings(bankroll, max_kelly_fraction,
    max_market_position_pct, max_category_exposure_pct, drawdown_pause_pct)
load_settings() -> Settings                  # tolerant: bad JSON → defaults
save_settings(s: Settings) -> None
calibration_adjusted_kelly_multiplier(brier: float | None) -> (multiplier, explanation_str)
current_drawdown(df_snaps) -> {peak, current, amount, pct, in_drawdown}
should_pause_for_drawdown(drawdown, threshold_pct) -> bool
category_exposure_pct(df_pos) -> {category: pct}
concentrated_categories(exposure, threshold_pct) -> [(cat, pct), ...]
position_exceeds_limit(cost_dollars, bankroll, max_pct) -> bool

# backtest.py — Tier 3b
@dataclass MarketSnapshot      # condition_id, question, category, as_of, prices
                               # .latest_price, .price_at_lookback(hours)
@dataclass Resolution          # condition_id, resolved_yes, closed_time
@dataclass Signal              # direction "YES"|"NO", probability, fraction_override
@dataclass BacktestResult      # n_trades, hit_rate, hit_rate_ci, total_pnl, roi,
                               # sharpe_per_trade, max_drawdown, brier_score,
                               # trades, equity_curve, by_category, warnings
build_dataset(provider, df_closed, *, lead_hours, history_days, fidelity,
              max_markets, progress_callback) -> list[(MarketSnapshot, Resolution)]
# Lookahead-safe: prices truncated strictly-before as_of; Resolution kept
# out of MarketSnapshot so strategies cannot peek at the outcome.
run_backtest(strategy, dataset, *, bankroll=1000.0, kelly_multiplier=0.5,
             half_spread=0.005, fee_pct=0.0, fee_fn=None,
             portfolio_constrained=True, max_position_pct=None,
             strategy_name=None) -> BacktestResult
# portfolio_constrained=True processes markets in as_of order with
# shared cash state — concurrent open positions compete for bankroll.
# Off recovers the legacy "each trade vs full bankroll" baseline.
# max_position_pct (e.g. 0.05) caps each trade's stake at that fraction
# of bankroll — mirrors strategy_runner's --max-position-pct flag so
# backtests measure what realistic live configs would deploy. Applied
# BEFORE the portfolio constraint.
# Single-shot: one trade per market at `closed_time - lead_hours`,
# hold to resolution.

run_backtest_walk_forward(strategy, dataset, *, bankroll=1000.0,
                            kelly_multiplier=0.5, half_spread=0.005,
                            fee_pct=0.0, eval_step_hours=24.0,
                            portfolio_constrained=True,
                            strategy_name=None) -> BacktestResult
# portfolio_constrained interleaves every market's eval + resolution
# events into one time-ordered queue with shared cash. Off reverts to
# per-market isolation (each market sees the full bankroll).
# Tier 3b v2: walks each market's price history at fixed steps, calls
# the strategy at each step, tracks one in-or-out position per market,
# marks to market on signal flip, exits at $1/$0 on resolution. Same
# BacktestResult shape; trade rows gain entry_time / exit_time /
# exit_reason ("signal_flip" | "resolution") / holding_period_days.
# half_spread (absolute price units per side, default 0.005) shifts the
# effective entry price worse than mid for whichever side is being
# bought. Both reduces payoff AND shrinks Kelly fraction (spread
# eating the edge → fraction → 0 → trade skipped). fee_pct is a
# fraction-of-stake fee deducted from PnL. Set both to 0 to recover
# the pre-cost "ideal mid-fill" behaviour.
market_baseline_brier(dataset) -> (brier|None, n)   # gate: "just trust latest price"
list_strategies() -> [(key, display_name)]
get_strategy(key) -> Strategy
register_strategy(key, display_name=None) -> decorator
# Used by user plugins in user_strategies/*.py
discover_user_strategies(*, package="user_strategies",
                          search_dir=None) -> int
# Imports every non-underscore-prefixed .py under user_strategies/
# so @register_strategy decorators fire. Idempotent; broken plugins
# are logged + skipped. Called once at app.py startup.
# Built-ins: strategy_market_consensus, strategy_always_yes_flat,
#            strategy_momentum, strategy_mean_reversion,
#            strategy_oscillation_revert  (registered by oscillation_scanner)
strategy_oscillation_revert(snap, *, z_threshold=1.0, edge=0.05,
                             min_range_score=0.5, min_cross_freq=0.5,
                             min_range_width=0.05,
                             min_history_points=20,
                             min_avg_price=0.05,
                             max_avg_price=0.95) -> Signal | None
# Requires snap.prices to be populated with real history. In live runs,
# pass `--snapshot-history-days N` to strategy_runner so the snapshot
# builder fetches it. In backtests, `build_dataset` already populates
# it from the closed-market history fetch.

# kalshi_fees.py — venue-accurate per-trade fee model
SPORTS_PREFIXES = ("KXMLB", "KXNBA", "KXNFL", "KXNHL", "KXWNBA",
                    "KXMLS", "KXMVESPORTS")
STANDARD_FEE_RATE = 0.07; SPORTS_FEE_RATE = 0.035
fee_for_trade(price, contracts, *, fee_rate=0.07) -> float
# Dollars per trade. ceil(rate × P × (1-P) × C × 100) / 100, with a
# round-to-9-decimals snap to avoid float-rep ceiling bugs.
fee_pct_of_stake(price, *, fee_rate=0.07) -> float    # = rate × (1-P)
fee_rate_for_series(ticker_or_series) -> float        # 0.035 for sports, else 0.07
round_trip_fees(entry_price, exit_price, contracts, *,
                 ticker="", fee_rate=None) -> float   # entry + exit legs

# strategy_runner.py — agentic-loop exit management
@dataclass ExitPolicy           # take_profit_pct, stop_loss_pct,
                                # take_profit_price, trail_stop_pct,
                                # trail_stop_atr_mult,
                                # trail_vol_window_hours,
                                # tp_to_trail_stop_pct, time_stop_days,
                                # disable_mgmt
# Two-stage TP: tp_to_trail_stop_pct (e.g. 0.05) with a TP threshold
# ARMS the position into a tight trail instead of selling — winners
# ride, reversals exit on first 5% giveback. position_meta.tp_armed
# persists the arming flag across cycles. _evaluate_exit returns the
# sentinel "arm_tp" which the caller translates to a meta flip.
# Vol-aware trail: trail_stop_atr_mult (K, e.g. 1.5) fires when
# peak-to-current drawdown exceeds K × σ_recent. σ is std of pct
# returns over trail_vol_window_hours (default 48h). Composes with
# fixed trail_stop_pct — whichever fires first wins. Requires per-
# position price-history fetch each cycle; cached for the cycle so
# duplicate tickers share one call.
# Exit reasons in exits.jsonl: stop_loss_pct | time_stop |
# take_profit_pct | take_profit_price | trail_stop | trail_stop_atr |
# tp_to_trail | resolution_won | resolution_lost.

_compute_recent_vol(provider, ticker, *, window_hours=48.0,
                    fidelity=60) -> float | None
# std-dev of pct returns over recent history. Returns None on too-
# little history (< 5 points) or fetch failure.
# All thresholds optional. disable_mgmt=True bypasses the entire phase
# (legacy hold-to-resolution behavior — the unmanaged sibling profiles
# keep this on). Precedence: stop_loss → time_stop → take_profit_pct →
# take_profit_price → trail_stop. Resolution-close always pre-empts.
run_cycle(*, strategy_key, bankroll, kelly_multiplier, half_spread,
           max_position_pct, min_gap, max_markets, profile_name,
           strategy_kwargs=None, closing_within_days=None,
           exit_policy=None, history_days=0,
           history_fidelity=60) -> RunnerStats
# history_days > 0 → snapshot builder fetches real price history per
# market via provider.get_price_history. Required by history-aware
# strategies (momentum, mean_reversion, oscillation_revert).
# RunnerStats gains: managed_exits, exit_reasons ({reason: count}).
# Each closure (resolution OR managed) is appended to
# data/<profile>/exits.jsonl with entry_price/exit_price/peak/
# gross_pnl/fees_paid/pnl (NET)/reason.
# TP/SL thresholds gate on NET P&L (after Kalshi entry+exit fees via
# kalshi_fees.round_trip_fees) — a gross gain that doesn't clear the
# fee drag won't fire a take-profit.

# position_meta.py — entry + peak tracking for the exit phase
record_entry(condition_id, outcome, *, entry_price, opened_at=None) -> dict
# Idempotent on (condition_id, outcome) — preserves original entry on
# re-buys so trail/peak math stays interpretable.
update_peak(condition_id, outcome, *, current_price, now=None) -> dict|None
# Peak ratchets up only — never down. That's what makes a trailing
# stop "trailing."
get_entry(condition_id, outcome) -> dict | None
clear_entry(condition_id, outcome) -> bool   # called when position fully closes
all_entries() -> dict[str, dict]

# exits_analysis.py — analytics over data/<profile>/exits.jsonl
load_exits(profile=None) -> pd.DataFrame
exit_reason_summary(profile=None) -> pd.DataFrame
# Cols: exit_reason, n, total_pnl_dollars, mean_pnl_pct, win_rate,
#       mean_held_days
counterfactual_pnl(df_exits, closed_lookup) -> pd.DataFrame
# Adds counterfactual_pnl_dollars / _pct / exit_alpha_dollars.
# closed_lookup is {condition_id: resolved_yes}; caller builds via
# provider.get_closed_markets across registered providers.
compare_profiles(managed_profile, unmanaged_profile, *,
                  closed_lookup=None) -> dict
# Top-level A/B card data. When closed_lookup is provided, includes
# managed_counterfactual {n_resolved, managed_pnl_on_resolved,
# held_pnl_on_resolved, exit_alpha}.

# oscillation_scanner.py — mean-reversion candidate finder
@dataclass OscillationMetrics    # condition_id, question, category,
                                 # current_price, n_history, days_covered,
                                 # avg_price, min/max/range_width,
                                 # drift_abs, drift_pct, detrended_std,
                                 # range_score, mean_cross_freq
compute_metrics(df_prices, *, condition_id="", question="", category="",
                 min_history_points=20) -> OscillationMetrics | None
# Pure function — no provider. Fits a linear trend, takes detrended std
# and slope, computes range_score = detrended_std / max(|drift|, eps).
# Pairs with mean_cross_freq (running-mean crossings per day) for a
# secondary check — true range-bound markets cross repeatedly.
scan_markets(provider, df_markets, *, days=30, fidelity=60,
              max_markets=200, min_history_points=20,
              min_avg_price=0.05, max_avg_price=0.95,
              min_cross_freq=0.0, min_range_width=0.0,
              progress_callback=None) -> pd.DataFrame
# Sorted-by-range_score DataFrame. Use min_cross_freq + min_range_width
# to filter out jittery cheap markets that score high on the ratio but
# have no tradeable absolute movement. CLI defaults: 0.3 / 0.05.

# oscillation_backtest.py — retrospective evaluation of the scanner
@dataclass BucketStats         # bucket_index, range_score_min/max,
                                # n_markets, n_strategy_fired,
                                # hit_rate, total_pnl, brier_score,
                                # avg_mean_cross_freq, avg_range_width
@dataclass OscillationBacktestResult
                                # n_dataset, n_oscillation_qualified,
                                # strategy (BacktestResult),
                                # baseline_consensus (BacktestResult),
                                # by_range_score_bucket [BucketStats],
                                # market_baseline_brier, market_baseline_n
run_oscillation_backtest(provider, *, max_markets=200, since_days=None,
                          until_days=0.0, lead_hours=72,
                          history_days=30, fidelity=60, bankroll=1000.0,
                          kelly_multiplier=0.5, half_spread=0.005,
                          strategy_kwargs=None, n_buckets=5,
                          max_workers=1, use_cache=True,
                          progress_callback=None) -> OscillationBacktestResult
# since_days set → uses provider.get_closed_markets_in_range for the
# big-sample historical path (cursor-paginated). max_workers > 1
# parallelizes the price-history phase (ThreadPoolExecutor). use_cache
# pickles the built dataset to data/shared/backtest_cache/ so re-running
# with different strategy params doesn't refetch.
format_report(result) -> str   # human-readable report (CLI uses this)

# crypto_data.py — Tier 4 spot-price + model layer
get_spot_price(asset) -> float | None             # Coinbase Exchange /ticker
get_klines(asset, *, days=90) -> pd.DataFrame      # paginates past Coinbase 300-row cap
spot_at(asset, ts) -> float | None                 # historical close at-or-before
realized_volatility(df, *, window=30) -> float | None              # annualized
realized_volatility_at(asset, ts, *, window=30) -> float | None
lognormal_above/below(spot, strike, vol, days) -> float | None     # terminal-on-date
barrier_hit_above/below(spot, strike, vol, days) -> float | None   # one-touch any-time

# llm_data.py — Tier 4 local-LLM forecasting layer (Ollama)
@dataclass LLMForecast                  # model, probability (0..1), reasoning
is_available(timeout=2.0) -> bool       # /api/tags health
list_models() -> list[str]              # populated by /api/tags
build_user_prompt(*, question, category, market_yes_price,
                   days_to_deadline, as_of) -> str
# market_yes_price is accepted but NOT included in the prompt (blind
# forecast). The LLM must form an independent estimate; gap vs market
# is computed afterward in the strategy layer.
get_forecast(*, question, model, category=None, market_yes_price=None,
             days_to_deadline=None, as_of=None, options=None,
             bypass_cache=False, cache_ttl_seconds=7*86400) -> LLMForecast | None
# Cached by default. Key (v2): model, question, category, quantized
# days_to_deadline. market_yes_price is NOT in the key or prompt.
# as_of is also not in the key (changes per call).
SYSTEM_PROMPT: str                       # independent-forecaster instructions
# Env: OLLAMA_API_BASE (default http://localhost:11434), OLLAMA_HTTP_TIMEOUT (120s)

# sports_data.py — Tier 4 ESPN consensus-odds layer
@dataclass TeamSide                   # name, short_name, abbreviation, is_home
@dataclass SportsEvent                # league, event_id, name, state,
                                      # home, away, home_winprob, away_winprob
LEAGUES = {"MLB": (..), "NBA": (..), ...}    # configured league set
moneyline_to_prob(ml) -> float | None        # American-odds → implied
devig(p_home, p_away) -> (h, a)              # normalize to sum=1
get_scoreboard(league) -> list[dict]
get_event_summary(league, event_id) -> dict | None
get_events_with_probabilities(league) -> list[SportsEvent]
# Env: ESPN_API_BASE (default site.api.espn.com/apis/site/v2)

# macro_data.py — Tier 4 FRED macro layer
@dataclass SeriesMeta                 # series_id, cadence, display_name, is_percent
SERIES = {"unemployment": SeriesMeta("UNRATE", "monthly", ...), ...}
get_observations(series_id) -> pd.DataFrame   # cols: date, value
latest_value(series_id) -> (value, observed_at) | None
value_at(series_id, as_of) -> (value, observed_at) | None
# at-or-before lookup for backtest honesty. Caveat: FRED observation_date
# ≠ publication_date; ≤1 publication cycle of forward leak.
realized_volatility(df, *, cadence, window=24) -> float | None  # cadence ∈ {"monthly","daily"}
realized_volatility_for(series_key, *, window=24) -> float | None
realized_volatility_at(series_key, as_of, *, window=24) -> float | None
normal_above(level, threshold, vol_ann, days, *, drift=0) -> float | None
normal_below(level, threshold, vol_ann, days, *, drift=0) -> float | None
# Env: FRED_GRAPH_BASE (default https://fred.stlouisfed.org/graph), FRED_HTTP_TIMEOUT

# llm_cache.py — persistent K/V cache backing the LLM wrappers
DEFAULT_CACHE_PATH = Path("data/shared/llm_cache.sqlite")
make_key(*parts) -> str                # SHA-256 of "|".join(parts)
get(key) -> dict | None                # None on miss or expired
set(key, value, *, ttl_seconds=None)   # ttl_seconds=None never expires
purge_expired() -> int                 # rows removed
clear() -> int
stats() -> dict                        # entries, expired, ages, file_bytes
# Env: LLM_CACHE_PATH (override default file location)

# llm_matcher.py — Tier 4 LLM-assisted semantic matching
@dataclass MatchVerdict               # same_question, confidence (0..1),
                                      # reasoning, model
verify_match(venue_question, candidate_question, *,
              model="llama3.1:latest", options=None,
              bypass_cache=False, cache_ttl_seconds=30*86400) -> MatchVerdict | None
# Cached by default; cache key sorts the question pair canonically so
# (A, B) and (B, A) share an entry.
SYSTEM_PROMPT: str                    # strict-by-default discrimination prompt
# Used by find_manifold_edges and strategy_manifold_community as an
# opt-in step after Jaccard pre-filtering. Reuses llm_data.OLLAMA_BASE
# and llm_data._session; no separate config.

# manifold_data.py — Tier 4 cross-platform forecast layer
@dataclass ManifoldMarket             # market_id, question, probability,
                                      # volume, is_resolved, close_time_ms
search_markets(query, *, limit=5, only_open=True) -> list[ManifoldMarket]
get_market(market_id) -> ManifoldMarket | None
jaccard_similarity(a, b) -> float     # token-set Jaccard, stopwords filtered
best_match(query, candidates) -> (ManifoldMarket, score) | None
# Env: MANIFOLD_API_BASE (default https://api.manifold.markets/v0)
# CAVEAT: matching is direction-blind — "cut rates" and "hike rates"
# share most tokens. Eyeball the matched question on every flag.

# edges/ — Tier 4 framework + one module per source (was edges.py until R-24;
#          every name below is still importable as edges.<name>)
@dataclass Edge                       # source, market_id, question, category,
                                      # market_yes_price, model_implied, gap,
                                      # direction, severity, note
@dataclass ParsedCryptoQuestion       # asset, strike_usd, direction,
                                      # deadline, model_type ("barrier"|"terminal")
parse_crypto_question(q, *, deadline) -> ParsedCryptoQuestion | None
# Verb-aware: "dip to $X" → below + barrier; "be above $X on DATE" → above + terminal
strategy_crypto_spot_lognormal(snap, *, min_gap=0.10, vol_window_days=30,
                                min_days_to_expiry=1.0) -> Signal | None
strategy_llm_forecast(snap, *, model="llama3.1:latest", min_gap=0.10,
                       min_days_to_expiry=1.0) -> Signal | None
# Both registered in backtest.STRATEGIES (as "crypto_spot_lognormal" and "llm_forecast").
find_crypto_edges(df_markets, *, min_gap=0.10, vol_window_days=30,
                  min_days_to_expiry=1.0) -> list[Edge]
find_llm_edges(df_markets, *, min_gap=0.10, model="llama3.1:latest",
                max_markets=25, min_days_to_expiry=1.0,
                progress_callback=None) -> list[Edge]
find_sports_edges(df_markets, *, min_gap=0.05, leagues=None,
                   min_days_to_expiry=0.0, progress_callback=None) -> list[Edge]
strategy_sports_winprob(snap, *, min_gap=0.05, leagues=None) -> Signal | None
sports_consensus_for_ticker(ticker, *, league="MLB") -> float | None
# De-vigged ESPN consensus P(YES) for a Kalshi MLB ticker (parses ticker →
# matches live ESPN event by team abbr). The close-game scalper's
# `--forecast-source consensus` gate. None when no live line.
find_baseball_edges(df_markets, *, min_gap=0.05,
                     min_days_to_expiry=0.0, progress_callback=None) -> list[Edge]
strategy_baseball_winprob(snap, *, min_gap=0.05) -> Signal | None
# source="baseball" in calibration log; backtest key "baseball_winprob".
# Adapter calls baseball.predict_game(...) — sidecar's only legitimate
# market-app touchpoint. Runs in parallel to find_sports_edges (which
# uses ESPN consensus) so we get head-to-head Brier on the same games.
find_wnba_edges(df_markets, *, min_gap=0.05,
                 min_days_to_expiry=0.0, progress_callback=None) -> list[Edge]
strategy_wnba_winprob(snap, *, min_gap=0.05) -> Signal | None
# source="wnba"; backtest key "wnba_winprob". Thin adapter — calls
# wnba.predict_for_ticker(...) (the sidecar's ticker bridge does the
# matching). Runs parallel to find_sports_edges for head-to-head Brier.
find_tennis_edges(df_markets, *, min_gap=0.05,
                   min_days_to_expiry=0.0, progress_callback=None) -> list[Edge]
strategy_tennis_winprob(snap, *, min_gap=0.05) -> Signal | None
# source="tennis"; key "tennis_winprob". Calls tennis.predict_for_ticker(
# ticker, question) — the question carries the player name the bridge
# needs. Elo model. Parallel to consensus for head-to-head Brier.
find_manifold_edges(df_markets, *, min_gap=0.10, min_similarity=0.30,
                     min_volume=100.0, max_markets=30,
                     min_days_to_expiry=1.0,
                     llm_verify=False, llm_verify_model="llama3.1:latest",
                     llm_min_confidence=0.6,
                     progress_callback=None) -> list[Edge]
strategy_manifold_community(snap, *, min_gap=0.10, min_similarity=0.30,
                             min_volume=100.0, min_days_to_expiry=1.0,
                             llm_verify=False, llm_verify_model="llama3.1:latest",
                             llm_min_confidence=0.6) -> Signal | None
parse_macro_question(question, *, deadline) -> ParsedMacroQuestion | None
find_all_edges(df_markets, *, sources=None, min_gap=0.10,
                min_days_to_expiry=1.0, per_source_kwargs=None,
                on_source_start=None, on_source_done=None) -> dict[str, list[Edge]]
# Runs every registered source against the same df and returns
# {source_key: [Edge,...]}. One broken source never breaks the run.
# Used by the 🎯 Recommendations page; safe to call from notebooks.
edges_severity(edge) -> float                # |gap|, for ranking
find_macro_edges(df_markets, *, min_gap=0.10,
                  min_days_to_expiry=1.0) -> list[Edge]
strategy_macro_threshold(snap, *, min_gap=0.10,
                          min_days_to_expiry=1.0) -> Signal | None
# Registered as "sports_winprob" in backtest.STRATEGIES. Live-only —
# ESPN's scoreboard is a current-events endpoint so backtest runs on
# closed historical markets return None until an archival source lands.
log_edges_to_calibration(edges) -> int   # bulk-record edges as forecasts;
                                          # source becomes "llm:<model>" or bare
edges_to_dataframe(edges) -> pd.DataFrame
list_sources() -> [(key, display_name)]
get_finder(key) -> callable

# calibration.py — new for Tier 1
brier_score_by_category(df_forecasts, df_markets) -> {category: brier | None}
# None for categories with < 5 resolved forecasts (sample size too small)

# profiles.py — multi-user (Option A)
initialize_profiles(project_root=None) -> None    # idempotent; safe on every render
list_profiles() -> list[str]
active_profile() -> str
set_active_profile(name: str) -> None
create_profile(name: str) -> bool                  # raises ValueError on bad names
reset_profile(name: str) -> int                    # wipes files; returns count
file_path(kind: str, profile: str | None = None) -> Path
# kind ∈ {"portfolio", "forecasts", "alerts", "settings"}

# shared.py — paper-trading framing
is_sandbox_active() -> bool                       # safe; never raises
paper_trading_banner(show_help=True) -> None      # no-op if not sandbox
simulated_watermark(fig) -> None                  # mutates Plotly figure; no-op if not sandbox
SANDBOX_PROFILE = "sandbox"                       # must equal a profile name in DEFAULT_PROFILES

# Venue-aware labels for page titles / CSV filenames / copy
venue_name() -> str        # short identifier, e.g. "kalshi"
venue_display() -> str     # human-readable, e.g. "Kalshi"

# provider.py — venue-agnostic data layer
class MarketProvider(Protocol):                   # see provider.py for the full method list
    name: str
    display_name: str
    get_markets / get_market / get_events / enrich_categories_from_events /
    get_price_history / get_order_book / get_midpoint /
    list_outcome_tokens(market_id) -> list[{"token_id","outcome"}] /
    get_closed_markets / get_market_calibration_data

register_provider(provider: MarketProvider) -> None
list_provider_names() -> list[str]
list_providers() -> list[MarketProvider]
get_provider(name: str | None = None) -> MarketProvider   # name=None → active
active_provider_name() -> str
set_active_provider(name: str) -> None            # via MARKET_PROVIDER env var

# sidecars.py — Sidecar protocol + registry
@dataclass SidecarHealth                  # status ("ok"|"degraded"|"down"),
                                          # last_update_ts, next_update_ts,
                                          # errors_24h, detail
@dataclass SidecarActivityEvent           # ts, kind, message, detail
@runtime_checkable Sidecar(Protocol)      # name, display_name,
                                          # health() / stats() /
                                          # recent_activity(limit=20)
register_sidecar(module_or_name) -> None  # called at sub-package import time
list_sidecars() -> list[Sidecar]
get_sidecar(name) -> Sidecar | None
safe_health(fn) -> SidecarHealth          # wraps a health-compute fn so
                                          # exceptions become status=down

# baseball/ — first sidecar instance
# Public domain surface (in baseball/__init__.py):
NAME = "baseball"; DISPLAY_NAME = "⚾ Baseball"
predict_game(home, away, game_date=None) -> dict | None
# Returns {home_win_prob, home_team, away_team, as_of, inputs, source} or None.
# `home`/`away` accept abbreviation OR short_name (case-insensitive).
# Prefers stored prediction for today; falls back to fresh compute.
team_ratings_table(season=None) -> list[dict]
# All rated teams sorted by regressed-Pythagorean strength desc.
# Cols: abbreviation, name, short_name, league, division, wins, losses,
#       win_pct, pythagorean, run_diff_per_game, strength
today_schedule() -> list[dict]            # today's games + predictions joined
refresh_all(force=False) -> dict          # runs one runner cycle (blocking,
                                          # ~5-30s for typical slate)
resolve_pending_tickers(tickers) -> dict[str, bool]
# Sidecar protocol hook (see docs/sidecar-protocol.md). Short-circuits
# Kalshi's 4-day settlement window: for any Kalshi MLB ticker whose
# game is Final in baseball state with known scores, resolves the
# forecast immediately. Called by auto_resolve_runner alongside
# get_closed_markets + Kalshi settlements. Cuts resolution latency
# from ~4 days to ~15 min (live-daemon sync + cron interval).
predict_for_ticker(ticker) -> dict | None
# Ticker-aware convenience wrapper used by inline UI integrations
# (Markets page model column, Kelly Sizer prefill, Price History overlay,
# Deep Analysis prompt enrichment, Portfolio model-opinion column).
# Returns {yes_prob, home_win_prob, home, away, yes_team, yes_side,
#          opponent, game_date, source}. None if not a recognized MLB
# Kalshi ticker or the game isn't in sidecar state.

# baseball.model — pure functions
MODEL_VERSION = "0.6.0"
PRIOR_WEIGHT = 40                         # virtual games at .500
HOME_FIELD_LOGODDS = ln(0.52/0.48)       # halved in v0.6 (was 0.54/0.46)
LEAGUE_AVG_ERA = 4.30
ERA_LOGODDS_PER_RUN = 0.0                # DISABLED v0.6 — zero signal at n=42
BULLPEN_LOGODDS_PER_RUN = 0.0            # DISABLED v0.6 — double-counted team quality
LINEUP_LOGODDS_PER_OPS = 2.25            # 0.75x of original (was 3.0)
regressed_pythagorean(rs, ra, gp) -> float        # Bayesian-regressed
pitcher_logodds_adj(era) -> float                 # always 0 while disabled
predict_matchup(*, home_strength, away_strength,
                 home_pitcher_era=None, away_pitcher_era=None,
                 home_team_name="Home", away_team_name="Away",
                 home_field=True) -> Prediction

# baseball.state — SQLite at data/shared/baseball_state.sqlite
# Env override: BASEBALL_STATE_PATH
SCHEMA_VERSION = 4  # v4: predictions dedup — one row per game_pk
migrate() -> None                                   # idempotent, called by runner
upsert_teams(teams) / upsert_team_season(stats) /
upsert_pitcher(pitcher, season) / upsert_game(g) /
record_prediction(game_pk, home_win_prob, home, away, model_version, inputs)
# Upserts on game_pk — one prediction per game, updated each cycle.
# Previously (game_pk, computed_at) PK caused ~100 dupes per game.
settle_prediction(game_pk, actual_winner)
log_activity(kind, message, detail=None)            # bounded ring (1000 events)
get_brier_summary(season) -> {"n": int, "brier": float|None}

# baseball.runner — cron entry
run_cycle(*, force=False) -> dict          # one full refresh pass

# baseball.live — long-lived daemon
fetch_live_feed(game_pk, *, timeout=10.0) -> dict | None    # MLB feed/live
run_cycle() -> dict                        # one polling pass
get_recent_plays(game_pk, *, limit=50) -> list[dict]
live_plays_summary(*, since_hours=24.0) -> dict
                                          # {plays_last_24h, games_last_24h, total_plays}
# New table live_plays (game_pk, play_id, ts, half_inning, inning,
#   event_type, description, home_score_after, away_score_after,
#   home_win_prob, raw_json, fetched_at). PRIMARY KEY (game_pk, play_id)
#   makes inserts idempotent.

# baseball.park_factors — per-park run/HR multipliers
@dataclass ParkFactor                      # team_id, abbreviation, runs_factor,
                                          # hr_factor, n_games, raw_*
PRIOR_WEIGHT = 30                          # virtual games at 1.0 (Bayesian)
compute_park_factors(*, min_games=5) -> {team_id: ParkFactor}
get_park_factors(*, refresh=False) -> {team_id: ParkFactor}  # cached
park_neutralize_strength(team_id, rs, ra, gp) -> (rs', ra')
                                          # halve-home approximation;
                                          # no-op when factor not yet
                                          # estimated for the team

# baseball.recent_form — rolling-window blend
DEFAULT_WINDOW_DAYS = 15
TARGET_GAMES_FOR_FULL_WEIGHT = 12
MAX_RECENT_WEIGHT = 0.30
get_recent_team_stats(team_id, *, days=15, as_of=None) -> dict | None
recent_weight(recent_gp) -> float          # linear ramp to MAX, capped
blended_strength(*, season_strength, recent_strength, recent_gp)
                                          # -> (blended, alpha_used)
get_recent_form_table(*, days=15, min_gp=3) -> list[dict]
                                          # hot/cold sorted

# baseball.analysis — lead-lag join queries
@dataclass BookPoint                       # ticker, snapshot_ts,
                                          # seconds_from_play (signed),
                                          # best_bid, best_ask, mid
tickers_for_game(game_pk) -> list[str]    # bridges game_pk → Kalshi
                                          # tickers via Eastern-date math
book_around_play(game_pk, play_id, *, window_minutes=5.0)
                                          # -> list[BookPoint]
reaction_summary(event_type, *, window_minutes=10.0, min_pct_change=0.02)
                                          # -> dict; reaction_pct = None
                                          # when n_played_with_book < 5
coverage_summary() -> dict                 # total plays, total books,
                                          # collection start
event_type_counts(*, since_ts=None) -> list[dict]

# baseball.insights — Stats-page aggregates + LLM context
@dataclass-style dicts:
league_leaders_offense(*, limit=10) -> list[dict]    # R/G
league_leaders_pitching(*, limit=10) -> list[dict]   # RA/G (asc)
team_event_leaders(event_type, side='batting', *, limit=10) -> list[dict]
one_run_games(*, limit=10) -> list[dict]
data_coverage() -> dict
current_edges_for_date(date_iso, *, ttl=300) -> list[dict]
                                          # 5-min cached Kalshi pull
context_for_llm(*, max_teams=30, max_edges_each=5) -> dict
                                          # bundles everything the
                                          # chat LLM should know about

# baseball.llm_chat — Ollama-backed chat
DEFAULT_MODEL = os.getenv("BASEBALL_CHAT_MODEL", "llama3.1:latest")
DEFAULT_TIMEOUT = float(os.getenv("BASEBALL_CHAT_TIMEOUT", "60"))
list_available_models() -> list[str]
is_available() -> bool
build_system_prompt(context=None) -> str   # injects full state
chat(messages, *, model=None, temperature=0.4, timeout=None) -> dict
                                          # {ok, reply, model, error}

# baseball.backfill — historical play-by-play loader
run_backfill(*, start, end, sleep_ms=200, games_only=False) -> dict
# CLI: --days N | --start --end | --games-only | --sleep-ms

# baseball.web — FastAPI app on :8770
app: FastAPI                               # routes: /, /games, /game/{pk},
                                          # /standings, /stats, /analysis,
                                          # /chat, /chat/send (POST),
                                          # /refresh (POST), /health.json,
                                          # /api/predict?home=X&away=Y

# mlb_book_poller — market-app daemon (Kalshi MLB books)
run_cycle() -> dict                        # one polling pass
# CLI: --loop --interval N | single cycle by default
```

```python
# pm/core/logctx.py — structured logging + correlation ids (R-23)
new_id(prefix: str) -> str            # "cyc-3f2a8b1c" — 8 hex chars of uuid4
current() -> dict[str, str]           # snapshot of bound fields ({} outside bind)
bind(**fields) -> ContextManager      # add fields for a block; nests additively,
                                      #   restores on exception; values
                                      #   stringified once at bind time
scope(_id_field="cycle", _id_prefix="cyc", **fields)
                                      # decorator: bind(fields + a fresh id) per
                                      #   call — made for the arms' run_cycle
setup(level=None, *, stream=None) -> None
                                      # THE logging-config call (replaces
                                      #   basicConfig everywhere): text with a
                                      #   [k=v ...] suffix only when bound, or
                                      #   JSONL when POLYMARKET_LOG_FORMAT=json.
                                      #   Idempotent like basicConfig — existing
                                      #   root handlers => adjusts level only.
# ContextFilter / JsonFormatter exposed for custom handlers.
# kalshi_orders._audit embeds current() into orders.log rows ("ctx" key).
```

## `pm/ops/settlement_check.py`

```python
check(tickers: set[str] | None = None, *, limit: int = 200) -> dict
```
Settlement state for `tickers`; `None` means everything this system placed
(`live_status.placed_tickers()`, read from `orders.log`). Returns
`{"settled": [{ticker, result, revenue_cents, settled_time}], "pending": [...],
"realized_cents": int}`. A placed ticker absent from the settlements feed is
**pending** — normal before close, alarming long after. Read-only.

```python
format_report(res: dict) -> str
```
Human-readable rendering of `check()`'s result.

CLI: `python -m pm.ops.settlement_check --profile <owner> [--ticker T]... [--notify]`
`[--wait-hours N --poll-minutes M]` polls until nothing is pending before notifying —
use this instead of guessing a settlement time in a one-shot timer.
