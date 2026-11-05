"""Tests for the longshot scalper: pure decision logic + a monkeypatched
run_cycle exercising open and take-profit. No network."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd
import pytest

import execution
import longshot_scalper as ls
import portfolio as pf
import position_meta as pm


class _FakeBroker:
    """Capturing paper broker for the flip/freeroll unit tests — records
    buy/sell kwargs so assertions can inspect what the daemon ordered."""

    is_live = False
    closes_settle_on_venue = False
    rests_take_profit = False

    def __init__(self):
        self.buys: list[dict] = []
        self.sells: list[dict] = []

    def buy(self, **k):
        self.buys.append(k)
        return execution.OrderResult(ok=True, status="paper",
                                     ticker=k.get("ticker", ""), side="yes",
                                     action="buy",
                                     requested_count=k.get("count", 0),
                                     filled_count=k.get("count", 0))

    def sell(self, **k):
        self.sells.append(k)
        return execution.OrderResult(ok=True, status="paper",
                                     ticker=k.get("ticker", ""), side="yes",
                                     action="sell",
                                     requested_count=k.get("count", 0),
                                     filled_count=k.get("count", 0))

    def get_positions(self):
        return pd.DataFrame()

    def available_cash(self, bankroll):
        return bankroll


class _LiveFakeBroker(_FakeBroker):
    """Live-capable fake: rests take-profits, records cancels/fills, and
    serves a settable positions frame — for the exit-coordination tests."""

    is_live = True
    closes_settle_on_venue = True
    rests_take_profit = True

    def __init__(self):
        super().__init__()
        self.resting: list[dict] = []
        self.canceled: list[str] = []
        self.fills: dict = {}
        self.positions_df = pd.DataFrame()

    def get_positions(self):
        return self.positions_df

    def place_resting_take_profit(self, *, ticker, outcome, count, tp_price):
        oid = f"tp-{ticker}"
        self.resting.append({"ticker": ticker, "count": count,
                             "tp_price": tp_price, "order_id": oid})
        return execution.OrderResult(ok=True, status="resting", ticker=ticker,
                                     side="yes", action="sell",
                                     requested_count=count, order_id=oid)

    def cancel(self, order_id):
        self.canceled.append(order_id)
        return True

    def fills_for_order(self, *, ticker, order_id):
        return self.fills.get(order_id, (0.0, None))


def _book(bid: float | None = None, ask: float | None = None) -> pd.DataFrame:
    rows = []
    if bid is not None:
        rows.append({"side": "bid", "price": bid, "size": 100.0})
    if ask is not None:
        rows.append({"side": "ask", "price": ask, "size": 100.0})
    return pd.DataFrame(rows)


# ─── Pure helpers ─────────────────────────────────────────────────────────

class TestBestBidAsk:
    def test_extracts_best_each_side(self):
        book = pd.DataFrame([
            {"side": "bid", "price": 0.30, "size": 1},
            {"side": "bid", "price": 0.28, "size": 1},
            {"side": "ask", "price": 0.33, "size": 1},
            {"side": "ask", "price": 0.35, "size": 1},
        ])
        assert ls.best_bid_ask(book) == (0.30, 0.33)

    def test_empty(self):
        assert ls.best_bid_ask(pd.DataFrame()) == (None, None)
        assert ls.best_bid_ask(None) == (None, None)

    def test_one_sided(self):
        assert ls.best_bid_ask(_book(ask=0.20)) == (None, 0.20)


class TestTeamFromTicker:
    def test_suffix(self):
        assert ls.team_from_ticker("KXMLBGAME-26MAY281810NYMMIA-NYM") == "NYM"

    def test_no_hyphen(self):
        assert ls.team_from_ticker("ABC") == "ABC"


class TestSelectLongshot:
    def test_picks_in_band(self):
        quotes = {"FAV": (0.68, 0.70), "DOG": (0.28, 0.30)}
        assert ls.select_longshot(quotes, min_price=0.10, max_price=0.35) == "DOG"

    def test_both_in_band_picks_cheaper(self):
        quotes = {"A": (0.30, 0.32), "B": (0.20, 0.22)}
        assert ls.select_longshot(quotes, min_price=0.10, max_price=0.35) == "B"

    def test_none_in_band(self):
        quotes = {"FAV": (0.68, 0.70), "DOG": (0.66, 0.68)}
        assert ls.select_longshot(quotes, min_price=0.10, max_price=0.35) is None

    def test_ignores_missing_ask(self):
        quotes = {"A": (0.20, None), "B": (0.18, 0.25)}
        assert ls.select_longshot(quotes, min_price=0.10, max_price=0.35) == "B"


class TestSelectCloseGameSide:
    BAND = dict(min_prob=0.40, max_prob=0.60, min_edge=0.05)

    def test_buys_side_trading_below_forecast(self):
        # Coin-flip game: home forecast 0.52, away 0.48. Home ask 0.45 is
        # 7pp below its forecast → underpriced → buy home.
        quotes = {"HOME": (0.43, 0.45), "AWAY": (0.49, 0.51)}
        fc = {"HOME": 0.52, "AWAY": 0.48}
        assert ls.select_close_game_side(quotes, fc, **self.BAND) == "HOME"

    def test_picks_larger_edge(self):
        quotes = {"HOME": (0.44, 0.46), "AWAY": (0.40, 0.42)}  # home edge .06, away .06?
        fc = {"HOME": 0.52, "AWAY": 0.48}  # home edge .06, away edge .06
        # tie → first-seen-with-strictly-greater wins; make away clearly larger
        quotes["AWAY"] = (0.36, 0.38)  # away edge = .48-.38 = .10 > home .06
        assert ls.select_close_game_side(quotes, fc, **self.BAND) == "AWAY"

    def test_skips_lopsided_game(self):
        # Home forecast 0.65 (out of band) → not a coin flip even if cheap.
        quotes = {"HOME": (0.50, 0.52), "AWAY": (0.30, 0.32)}
        fc = {"HOME": 0.65, "AWAY": 0.35}
        assert ls.select_close_game_side(quotes, fc, **self.BAND) is None

    def test_skips_when_not_below_forecast(self):
        # Coin flip but market is at/above forecast → no edge.
        quotes = {"HOME": (0.51, 0.53), "AWAY": (0.49, 0.51)}
        fc = {"HOME": 0.52, "AWAY": 0.48}
        assert ls.select_close_game_side(quotes, fc, **self.BAND) is None

    def test_skips_when_forecast_missing(self):
        quotes = {"HOME": (0.43, 0.45)}
        assert ls.select_close_game_side(quotes, {"HOME": None}, **self.BAND) is None

    def test_price_floor_skips_deep_underdog(self):
        # The KC@$0.07 failure: model calls it a coin flip (0.45) and the
        # huge gap looks like a 38pp "edge", but a side priced at $0.07 is a
        # longshot the market is screaming about — the floor rejects it.
        quotes = {"HOME": (0.06, 0.07), "AWAY": (0.90, 0.93)}
        fc = {"HOME": 0.45, "AWAY": 0.55}
        assert ls.select_close_game_side(quotes, fc, **self.BAND, min_ask=0.25) is None

    def test_price_floor_allows_near_coinflip(self):
        # A side at $0.45 (genuine toss-up, trading a touch low) clears the
        # 0.25 floor and is bought.
        quotes = {"HOME": (0.43, 0.45), "AWAY": (0.49, 0.51)}
        fc = {"HOME": 0.52, "AWAY": 0.48}
        assert ls.select_close_game_side(quotes, fc, **self.BAND, min_ask=0.25) == "HOME"

    def test_price_floor_defaults_off(self):
        # Default min_ask=0.0 preserves prior behavior — a cheap side still
        # qualifies when no floor is requested.
        quotes = {"HOME": (0.06, 0.07), "AWAY": (0.90, 0.93)}
        fc = {"HOME": 0.45, "AWAY": 0.55}
        assert ls.select_close_game_side(quotes, fc, **self.BAND) == "HOME"


class TestSelectFavoriteSide:
    BAND = dict(min_prob=0.55, max_prob=0.70)

    def test_buys_favorite_in_band(self):
        # HOME forecast 0.62 sits in [0.55,0.70] → buy HOME (the favorite).
        quotes = {"HOME": (0.58, 0.60), "AWAY": (0.40, 0.42)}
        fc = {"HOME": 0.62, "AWAY": 0.38}
        assert ls.select_favorite_side(quotes, fc, **self.BAND) == "HOME"

    def test_ignores_price_only_forecast_matters(self):
        # No edge requirement: even a fairly/expensively priced favorite is
        # bought as long as the forecast is in band.
        quotes = {"HOME": (0.64, 0.66), "AWAY": (0.34, 0.36)}
        fc = {"HOME": 0.62, "AWAY": 0.38}
        assert ls.select_favorite_side(quotes, fc, **self.BAND) == "HOME"

    def test_skips_coinflip(self):
        # Neither side is a moderate favorite (both ~0.50) → no trade.
        quotes = {"HOME": (0.49, 0.51), "AWAY": (0.49, 0.51)}
        fc = {"HOME": 0.51, "AWAY": 0.49}
        assert ls.select_favorite_side(quotes, fc, **self.BAND) is None

    def test_skips_heavy_favorite(self):
        # 0.80 is above the band ceiling — too lopsided, skip.
        quotes = {"HOME": (0.78, 0.80), "AWAY": (0.20, 0.22)}
        fc = {"HOME": 0.80, "AWAY": 0.20}
        assert ls.select_favorite_side(quotes, fc, **self.BAND) is None

    def test_picks_stronger_when_both_in_band(self):
        # Wide-ish band where both qualify → take the higher forecast.
        quotes = {"HOME": (0.55, 0.57), "AWAY": (0.56, 0.58)}
        fc = {"HOME": 0.56, "AWAY": 0.58}
        assert ls.select_favorite_side(quotes, fc, **self.BAND) == "AWAY"

    def test_skips_missing_forecast_or_ask(self):
        quotes = {"HOME": (0.58, None), "AWAY": (0.40, 0.42)}
        fc = {"HOME": 0.62, "AWAY": None}
        assert ls.select_favorite_side(quotes, fc, **self.BAND) is None


class TestEvaluateExit:
    CFG = ls.ScalpConfig(take_profit_pct=0.25, flip_drop_pct=0.15)

    def test_take_profit(self):
        # Entry 0.20 → sell ~0.27 is well past +25% net even after fees.
        entry = {"entry_price": 0.20}
        assert ls.evaluate_exit(entry=entry, side_price=0.28, cfg=self.CFG,
                                ticker="KXMLBGAME-X-NYM") == "scalp_take_profit"

    def test_flip_on_drop(self):
        entry = {"entry_price": 0.30}
        # 0.30 → 0.24 is -20% gross, well past the -15% net flip threshold.
        assert ls.evaluate_exit(entry=entry, side_price=0.24, cfg=self.CFG,
                                ticker="KXMLBGAME-X-NYM") == "flip"

    def test_favorite_mode_never_exits_early(self):
        # favorite mode holds to resolution: neither a big gain nor a big
        # loss triggers a managed exit.
        cfg = ls.ScalpConfig(mode="favorite", take_profit_pct=0.25,
                             flip_drop_pct=0.15)
        assert ls.evaluate_exit(entry={"entry_price": 0.60}, side_price=0.90,
                                cfg=cfg, ticker="KXMLBGAME-X-NYM") is None
        assert ls.evaluate_exit(entry={"entry_price": 0.60}, side_price=0.30,
                                cfg=cfg, ticker="KXMLBGAME-X-NYM") is None

    def test_flipped_position_does_not_flip_again(self):
        entry = {"entry_price": 0.70, "from_flip": True}
        # Big drop, but from_flip guards a second reversal.
        assert ls.evaluate_exit(entry=entry, side_price=0.50, cfg=self.CFG,
                                ticker="KXMLBGAME-X-NYM") is None

    def test_flipped_position_can_still_take_profit(self):
        entry = {"entry_price": 0.70, "from_flip": True}
        assert ls.evaluate_exit(entry=entry, side_price=0.95, cfg=self.CFG,
                                ticker="KXMLBGAME-X-NYM") == "scalp_take_profit"

    def test_hold_when_flat(self):
        entry = {"entry_price": 0.30}
        assert ls.evaluate_exit(entry=entry, side_price=0.305, cfg=self.CFG,
                                ticker="KXMLBGAME-X-NYM") is None

    def test_zero_entry_guards(self):
        assert ls.evaluate_exit(entry={"entry_price": 0}, side_price=0.5,
                                cfg=self.CFG, ticker="X") is None

    def test_flip_disabled_when_drop_pct_none(self):
        # close_game mode runs with flip_drop_pct=None → big drop, no flip.
        cfg = ls.ScalpConfig(flip_drop_pct=None, take_profit_pct=0.25)
        assert ls.evaluate_exit(entry={"entry_price": 0.50}, side_price=0.30,
                                cfg=cfg, ticker="KXMLBGAME-X-NYM") is None

    def test_freeroll_position_does_not_flip(self):
        entry = {"entry_price": 0.30, "from_freeroll": True}
        assert ls.evaluate_exit(entry=entry, side_price=0.24, cfg=self.CFG,
                                ticker="KXMLBGAME-X-NYM") is None

    # ── stop-loss (close-game A/B arm) ───────────────────────────────────
    STOP_CFG = ls.ScalpConfig(take_profit_pct=0.25, flip_drop_pct=None,
                              cg_stop_loss_pct=0.25)

    def test_stop_loss_fires_past_threshold(self):
        # 0.40 → 0.28 is -30% gross, past the -25% net stop.
        entry = {"entry_price": 0.40}
        assert ls.evaluate_exit(entry=entry, side_price=0.28, cfg=self.STOP_CFG,
                                ticker="KXMLBGAME-X-NYM") == "stop_loss"

    def test_stop_loss_holds_inside_threshold(self):
        # 0.40 → 0.37 is only -7.5%, inside the -25% stop → hold.
        entry = {"entry_price": 0.40}
        assert ls.evaluate_exit(entry=entry, side_price=0.37, cfg=self.STOP_CFG,
                                ticker="KXMLBGAME-X-NYM") is None

    def test_stop_loss_off_by_default(self):
        # Default config has cg_stop_loss_pct=None → a deep drop holds.
        entry = {"entry_price": 0.40}
        cfg = ls.ScalpConfig(take_profit_pct=0.25, flip_drop_pct=None)
        assert ls.evaluate_exit(entry=entry, side_price=0.10, cfg=cfg,
                                ticker="KXMLBGAME-X-NYM") is None

    def test_take_profit_precedes_stop_loss(self):
        # A position can't be both up enough to bank and down enough to stop;
        # TP is checked first regardless.
        entry = {"entry_price": 0.20}
        assert ls.evaluate_exit(entry=entry, side_price=0.28, cfg=self.STOP_CFG,
                                ticker="KXMLBGAME-X-NYM") == "scalp_take_profit"

    def test_stop_loss_precedes_flip(self):
        # When both a stop and a flip are configured, the stop (cut to cash)
        # wins over the flip (reverse) on the downside.
        entry = {"entry_price": 0.40}
        cfg = ls.ScalpConfig(take_profit_pct=0.25, flip_drop_pct=0.15,
                             cg_stop_loss_pct=0.25)
        assert ls.evaluate_exit(entry=entry, side_price=0.28, cfg=cfg,
                                ticker="KXMLBGAME-X-NYM") == "stop_loss"


# ─── position_meta extra flag ─────────────────────────────────────────────

class TestPositionMetaExtra:
    def test_stores_from_flip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POLYMARKET_POSITION_META_FILE",
                           str(tmp_path / "meta.json"))
        pm.record_entry("KX-OPP", "Yes", entry_price=0.7,
                        extra={"from_flip": True})
        assert pm.get_entry("KX-OPP", "Yes")["from_flip"] is True

    def test_reserved_keys_not_overridable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POLYMARKET_POSITION_META_FILE",
                           str(tmp_path / "meta.json"))
        pm.record_entry("KX-A", "Yes", entry_price=0.3,
                        extra={"entry_price": 0.99, "tag": "x"})
        e = pm.get_entry("KX-A", "Yes")
        assert e["entry_price"] == 0.3   # not clobbered
        assert e["tag"] == "x"


# ─── _execute_flip ────────────────────────────────────────────────────────

@dataclass
class _State:
    game_pk: int = 1
    inning: int = 6
    half: str = "top"
    home_abbr: str = "MIA"
    away_abbr: str = "NYM"
    score_diff: int = 0


class TestExecuteFlip:
    def test_buys_opponent_with_proceeds(self, monkeypatch):
        cfg = ls.ScalpConfig()
        broker = _FakeBroker()
        monkeypatch.setattr(ls.pm, "record_entry",
                            lambda *a, **k: {"extra": k.get("extra")})
        monkeypatch.setattr(ls, "_log_event", lambda *a, **k: None)
        opp_of = {"DOG": "FAV"}
        quotes = {"FAV": (0.68, 0.70)}
        game_of = {"FAV": _State()}
        cash = ls._execute_flip(
            profile_name="p", broker=broker, cfg=cfg, sold_ticker="DOG",
            opp_of=opp_of,
            game_of=game_of, quotes=quotes, proceeds=10.0, remaining_cash=0.0,
            net=-0.2,
        )
        assert len(broker.buys) == 1
        assert broker.buys[0]["ticker"] == "FAV"
        assert broker.buys[0]["price"] == 0.70
        # Spent ~$10 of proceeds; cash near zero.
        assert cash == pytest.approx(0.0, abs=1e-6)

    def test_no_opponent_keeps_cash(self, monkeypatch):
        cfg = ls.ScalpConfig()
        broker = _FakeBroker()
        cash = ls._execute_flip(
            profile_name="p", broker=broker, cfg=cfg, sold_ticker="DOG",
            opp_of={},
            game_of={}, quotes={}, proceeds=10.0, remaining_cash=5.0, net=-0.2,
        )
        assert broker.buys == []
        assert cash == pytest.approx(15.0)

    def test_no_opponent_quote_keeps_cash(self, monkeypatch):
        cfg = ls.ScalpConfig()
        broker = _FakeBroker()
        cash = ls._execute_flip(
            profile_name="p", broker=broker, cfg=cfg, sold_ticker="DOG",
            opp_of={"DOG": "FAV"},
            game_of={}, quotes={"FAV": (None, None)}, proceeds=8.0,
            remaining_cash=2.0, net=-0.2,
        )
        assert broker.buys == []
        assert cash == pytest.approx(10.0)


# ─── _execute_freeroll ────────────────────────────────────────────────────

class TestExecuteFreeroll:
    def _patch(self, monkeypatch):
        broker = _FakeBroker()
        entries: list = []
        monkeypatch.setattr(ls.pm, "record_entry",
                            lambda *a, **k: entries.append((a, k)))
        monkeypatch.setattr(ls, "_log_event", lambda *a, **k: None)
        return broker, entries

    def test_buys_opposite_in_band_with_profit(self, monkeypatch):
        broker, entries = self._patch(monkeypatch)
        cfg = ls.ScalpConfig()  # band 0.10–0.35
        cash, did = ls._execute_freeroll(
            profile_name="p", broker=broker, cfg=cfg, sold_ticker="DOG",
            opp_of={"DOG": "OPP"}, game_of={"OPP": _State()},
            quotes={"OPP": (0.28, 0.30)}, amount=3.0, remaining_cash=100.0,
        )
        assert did is True
        assert len(broker.buys) == 1 and broker.buys[0]["ticker"] == "OPP"
        assert broker.buys[0]["price"] == 0.30
        # Only the $3 profit was redeployed; principal stays in cash.
        assert cash == pytest.approx(97.0)
        # Tagged from_freeroll so it never flips/free-rolls again.
        assert entries[0][1]["extra"] == {"from_freeroll": True}

    def test_skips_when_opposite_out_of_band(self, monkeypatch):
        broker, _ = self._patch(monkeypatch)
        cfg = ls.ScalpConfig()
        cash, did = ls._execute_freeroll(
            profile_name="p", broker=broker, cfg=cfg, sold_ticker="DOG",
            opp_of={"DOG": "FAV"}, game_of={}, quotes={"FAV": (0.68, 0.70)},
            amount=3.0, remaining_cash=100.0,
        )
        assert did is False
        assert broker.buys == []
        assert cash == pytest.approx(100.0)  # profit stays in cash

    def test_skips_when_no_opponent(self, monkeypatch):
        broker, _ = self._patch(monkeypatch)
        cfg = ls.ScalpConfig()
        cash, did = ls._execute_freeroll(
            profile_name="p", broker=broker, cfg=cfg, sold_ticker="DOG",
            opp_of={},
            game_of={}, quotes={}, amount=3.0, remaining_cash=50.0,
        )
        assert did is False and broker.buys == [] and cash == pytest.approx(50.0)

    def test_skips_when_profit_below_min_stake(self, monkeypatch):
        broker, _ = self._patch(monkeypatch)
        cfg = ls.ScalpConfig(min_stake=0.50)
        cash, did = ls._execute_freeroll(
            profile_name="p", broker=broker, cfg=cfg, sold_ticker="DOG",
            opp_of={"DOG": "OPP"}, game_of={}, quotes={"OPP": (0.28, 0.30)},
            amount=0.20, remaining_cash=100.0,
        )
        assert did is False and broker.buys == []


# ─── run_cycle integration (monkeypatched live data + temp profile) ───────

@pytest.fixture
def temp_profile(tmp_path, monkeypatch):
    """Pin all profile files to tmp + stub profiles.* so run_cycle doesn't
    touch the real data dir, and make _pin_profile_env a no-op (env already
    points where we want)."""
    for kind in ("PORTFOLIO", "FORECASTS", "ALERTS"):
        monkeypatch.setenv(f"POLYMARKET_{kind}_FILE",
                           str(tmp_path / f"{kind.lower()}.json"))
    monkeypatch.setenv("POLYMARKET_POSITION_META_FILE",
                       str(tmp_path / "meta.json"))
    monkeypatch.setattr(ls.profiles, "initialize_profiles", lambda *a, **k: None)
    monkeypatch.setattr(ls.profiles, "list_profiles",
                        lambda: ["agentic_longshot_scalp"])
    monkeypatch.setattr(ls.profiles, "create_profile", lambda *a, **k: True)
    monkeypatch.setattr(ls, "_pin_profile_env", lambda name: {})
    monkeypatch.setattr(ls, "_restore_profile_env", lambda env: None)
    monkeypatch.setattr(ls.emergency_stop, "is_active", lambda: False)
    # Redirect the two log paths that are NOT env-overridable to tmp, so the
    # integration tests don't leak rows into the real shared/profile files:
    #   - _log_event → SCALP_LOG_PATH (module global)
    #   - _log_exit  → profiles.profile_dir(name)/exits.jsonl
    monkeypatch.setattr(ls, "SCALP_LOG_PATH", tmp_path / "scalp_log.jsonl")
    monkeypatch.setattr(ls.profiles, "profile_dir", lambda name: tmp_path)
    return tmp_path


def _patch_live(monkeypatch, *, state, tickers, quotes):
    monkeypatch.setattr(ls, "_live_games", lambda: [state])
    from baseball import analysis
    monkeypatch.setattr(analysis, "tickers_for_game", lambda gpk: tickers)
    monkeypatch.setattr(ls, "_ticker_quote",
                        lambda t, *, now, max_age_s: quotes.get(t, (None, None)))
    # No finished games.
    import baseball
    monkeypatch.setattr(baseball, "resolve_pending_tickers", lambda tk: {})


class TestRunCycleIntegration:
    def test_opens_a_longshot(self, temp_profile, monkeypatch):
        st = _State(inning=5, score_diff=1)
        tickers = ["KXMLBGAME-X-NYM", "KXMLBGAME-X-MIA"]
        quotes = {"KXMLBGAME-X-NYM": (0.66, 0.70),   # favorite
                  "KXMLBGAME-X-MIA": (0.28, 0.30)}   # longshot
        _patch_live(monkeypatch, state=st, tickers=tickers, quotes=quotes)

        cfg = ls.ScalpConfig(profile_name="agentic_longshot_scalp")
        stats = ls.run_cycle(cfg)
        assert stats.opened == 1
        pos = pf.get_positions()
        assert list(pos["condition_id"]) == ["KXMLBGAME-X-MIA"]
        assert pos.iloc[0]["avg_price"] == pytest.approx(0.30)

    def test_take_profit_closes_position(self, temp_profile, monkeypatch):
        st = _State(inning=5, score_diff=1)
        tickers = ["KXMLBGAME-X-NYM", "KXMLBGAME-X-MIA"]
        # Cycle 1: open the longshot at 0.30.
        q1 = {"KXMLBGAME-X-NYM": (0.66, 0.70),
              "KXMLBGAME-X-MIA": (0.28, 0.30)}
        _patch_live(monkeypatch, state=st, tickers=tickers, quotes=q1)
        cfg = ls.ScalpConfig(profile_name="agentic_longshot_scalp")
        ls.run_cycle(cfg)
        assert not pf.get_positions().empty

        # Cycle 2: longshot popped to bid 0.40 → +33% gross, past +25% net.
        q2 = {"KXMLBGAME-X-NYM": (0.56, 0.60),
              "KXMLBGAME-X-MIA": (0.40, 0.42)}
        _patch_live(monkeypatch, state=st, tickers=tickers, quotes=q2)
        stats = ls.run_cycle(cfg)
        assert stats.take_profits == 1
        assert pf.get_positions().empty  # fully closed

    def test_freeroll_buys_opposite_after_take_profit(self, temp_profile, monkeypatch):
        st = _State(inning=5, score_diff=1)
        tickers = ["KXMLBGAME-X-NYM", "KXMLBGAME-X-MIA"]
        cfg = ls.ScalpConfig(profile_name="agentic_longshot_scalp",
                             freeroll_profit=True)
        # Cycle 1: open MIA longshot at 0.30.
        q1 = {"KXMLBGAME-X-NYM": (0.66, 0.70),
              "KXMLBGAME-X-MIA": (0.28, 0.30)}
        _patch_live(monkeypatch, state=st, tickers=tickers, quotes=q1)
        ls.run_cycle(cfg)

        # Cycle 2: MIA pops (sell at bid 0.40 → take-profit) AND the opposite
        # NYM has dropped INTO the band (ask 0.30) — so profit free-rolls into NYM.
        q2 = {"KXMLBGAME-X-NYM": (0.28, 0.30),
              "KXMLBGAME-X-MIA": (0.40, 0.42)}
        _patch_live(monkeypatch, state=st, tickers=tickers, quotes=q2)
        stats = ls.run_cycle(cfg)
        assert stats.take_profits == 1
        assert stats.freerolls == 1
        pos = pf.get_positions()
        # MIA closed, NYM opened from the recycled profit.
        assert list(pos["condition_id"]) == ["KXMLBGAME-X-NYM"]
        assert pos.iloc[0]["avg_price"] == pytest.approx(0.30)
        # The free-roll position is tagged so it won't flip back.
        assert ls.pm.get_entry("KXMLBGAME-X-NYM", "Yes")["from_freeroll"] is True

    def test_close_game_mode_buys_underpriced_coinflip_side(self, temp_profile,
                                                            monkeypatch):
        st = _State(inning=2, score_diff=0)  # early innings, tied
        tickers = ["KXMLBGAME-X-NYM", "KXMLBGAME-X-MIA"]
        # Coin flip: NYM forecast 0.53, but NYM ask 0.45 → 8pp below → buy NYM.
        quotes = {"KXMLBGAME-X-NYM": (0.43, 0.45),
                  "KXMLBGAME-X-MIA": (0.49, 0.51)}
        _patch_live(monkeypatch, state=st, tickers=tickers, quotes=quotes)
        fc = {"KXMLBGAME-X-NYM": 0.53, "KXMLBGAME-X-MIA": 0.47}
        monkeypatch.setattr(ls, "_forecast_for_ticker",
                            lambda t, src: fc.get(t))
        cfg = ls.ScalpConfig(profile_name="agentic_closegame_scalp",
                             mode="close_game", flip_drop_pct=None)
        stats = ls.run_cycle(cfg)
        assert stats.opened == 1
        pos = pf.get_positions()
        assert list(pos["condition_id"]) == ["KXMLBGAME-X-NYM"]
        assert pos.iloc[0]["avg_price"] == pytest.approx(0.45)

    def test_close_game_mode_skips_lopsided(self, temp_profile, monkeypatch):
        st = _State(inning=2, score_diff=0)
        tickers = ["KXMLBGAME-X-NYM", "KXMLBGAME-X-MIA"]
        quotes = {"KXMLBGAME-X-NYM": (0.28, 0.30),
                  "KXMLBGAME-X-MIA": (0.68, 0.70)}
        _patch_live(monkeypatch, state=st, tickers=tickers, quotes=quotes)
        # Lopsided: NYM forecast 0.30 (out of [0.40,0.60]) even though cheap.
        fc = {"KXMLBGAME-X-NYM": 0.30, "KXMLBGAME-X-MIA": 0.70}
        monkeypatch.setattr(ls, "_forecast_for_ticker", lambda t, src: fc.get(t))
        cfg = ls.ScalpConfig(profile_name="agentic_closegame_scalp",
                             mode="close_game", flip_drop_pct=None)
        stats = ls.run_cycle(cfg)
        assert stats.opened == 0
        assert pf.get_positions().empty


class TestStopGuard:
    """Re-entry-after-stop whipsaw guard (the CWS@MIN 4-stop bug)."""

    def test_roundtrip_and_ttl(self, temp_profile):
        assert ls._is_stopped(123) is False
        ls._mark_stopped(123)
        assert ls._is_stopped(123) is True
        assert ls._is_stopped(999) is False
        assert ls._is_stopped(None) is False

    def test_expired_entry_ignored(self, temp_profile, monkeypatch):
        ls._mark_stopped(123)
        # Jump the clock past the 24h TTL → guard lapses.
        import longshot_scalper as mod
        real = mod._STOP_GUARD_TTL_S
        monkeypatch.setattr(mod, "_STOP_GUARD_TTL_S", -1)
        assert ls._is_stopped(123) is False
        monkeypatch.setattr(mod, "_STOP_GUARD_TTL_S", real)


class TestResolvedGuard:
    """Re-entry-after-resolution guard (the 163-dup-exit bug, 2026-06-17):
    a finished game whose `games.status` lags the final score must not be
    re-entered after its position closes at $1/$0."""

    def test_roundtrip_and_ttl(self, temp_profile):
        assert ls._is_resolved("KXMLBGAME-X-ATL") is False
        ls._mark_resolved("KXMLBGAME-X-ATL")
        assert ls._is_resolved("KXMLBGAME-X-ATL") is True
        assert ls._is_resolved("KXMLBGAME-X-SF") is False
        assert ls._is_resolved(None) is False
        assert ls._is_resolved("") is False

    def test_expired_entry_ignored(self, temp_profile, monkeypatch):
        ls._mark_resolved("T-1")
        import longshot_scalper as mod
        real = mod._STOP_GUARD_TTL_S
        monkeypatch.setattr(mod, "_STOP_GUARD_TTL_S", -1)
        assert ls._is_resolved("T-1") is False
        monkeypatch.setattr(mod, "_STOP_GUARD_TTL_S", real)

    def test_stop_blocks_same_cycle_reentry(self, temp_profile, monkeypatch):
        st = _State(inning=2, score_diff=0)
        tickers = ["KXMLBGAME-X-NYM", "KXMLBGAME-X-MIA"]
        cfg = ls.ScalpConfig(profile_name="agentic_closegame_scalp",
                             mode="close_game", flip_drop_pct=None,
                             cg_stop_loss_pct=0.25)
        fc = {"KXMLBGAME-X-NYM": 0.53, "KXMLBGAME-X-MIA": 0.47}
        monkeypatch.setattr(ls, "_forecast_for_ticker", lambda t, src: fc.get(t))
        # Cycle 1: open NYM at ask 0.45 (coin-flip side trading a touch low).
        q1 = {"KXMLBGAME-X-NYM": (0.43, 0.45), "KXMLBGAME-X-MIA": (0.49, 0.51)}
        _patch_live(monkeypatch, state=st, tickers=tickers, quotes=q1)
        ls.run_cycle(cfg)
        assert not pf.get_positions().empty

        # Cycle 2: NYM bid craters to 0.30 (past -25% from 0.45) → stop. The
        # ask (0.32) is still attractive, so WITHOUT the guard it would
        # immediately re-open and whipsaw. The guard must block that.
        q2 = {"KXMLBGAME-X-NYM": (0.30, 0.32), "KXMLBGAME-X-MIA": (0.49, 0.51)}
        _patch_live(monkeypatch, state=st, tickers=tickers, quotes=q2)
        stats = ls.run_cycle(cfg)
        assert stats.stop_losses == 1
        assert stats.opened == 0                 # whipsaw re-entry blocked
        assert pf.get_positions().empty
        assert ls._is_stopped(st.game_pk) is True


class TestRestingTakeProfit:
    """Live cash-out limit + exit coordination (paper path stays polled)."""

    def test_arm_noop_in_paper(self, temp_profile):
        broker = _FakeBroker()  # rests_take_profit False
        pm.record_entry("T", "Yes", entry_price=0.40)
        ls._arm_resting_tp(broker, ls.ScalpConfig(mode="close_game"),
                           ticker="T", outcome="Yes", entry_price=0.40,
                           count=3, question="q")
        assert pm.get_entry("T", "Yes").get("tp_order_id") is None

    def test_arm_noop_in_favorite_mode(self, temp_profile):
        broker = _LiveFakeBroker()
        pm.record_entry("T", "Yes", entry_price=0.60)
        ls._arm_resting_tp(broker, ls.ScalpConfig(mode="favorite"),
                           ticker="T", outcome="Yes", entry_price=0.60,
                           count=3, question="q")
        assert broker.resting == []
        assert pm.get_entry("T", "Yes").get("tp_order_id") is None

    def test_arm_places_and_records_in_live(self, temp_profile):
        broker = _LiveFakeBroker()
        pm.record_entry("T", "Yes", entry_price=0.40)
        ls._arm_resting_tp(broker,
                           ls.ScalpConfig(mode="close_game", take_profit_pct=0.25),
                           ticker="T", outcome="Yes", entry_price=0.40,
                           count=3, question="q")
        assert len(broker.resting) == 1
        assert broker.resting[0]["tp_price"] == pytest.approx(0.50)
        assert pm.get_entry("T", "Yes")["tp_order_id"] == "tp-T"

    def test_reconcile_logs_and_clears_on_fill(self, temp_profile, monkeypatch):
        broker = _LiveFakeBroker()
        pm.record_entry("T", "Yes", entry_price=0.40)
        pm.set_field("T", "Yes", "tp_order_id", "tp-T")
        pm.set_field("T", "Yes", "question", "q")
        broker.fills["tp-T"] = (3.0, 0.50)
        logged: list = []
        monkeypatch.setattr(ls, "_log_exit", lambda **k: logged.append(k))
        stats = ls.ScalpStats(cycle_started=datetime.now(UTC))
        ls._reconcile_resting_tps(profile_name="p", broker=broker, stats=stats)
        assert stats.take_profits == 1
        assert logged[0]["exit_reason"] == "scalp_take_profit"
        assert logged[0]["exit_price"] == pytest.approx(0.50)
        assert pm.get_entry("T", "Yes") is None  # cleared

    def test_reconcile_skips_unfilled(self, temp_profile, monkeypatch):
        broker = _LiveFakeBroker()
        pm.record_entry("T", "Yes", entry_price=0.40)
        pm.set_field("T", "Yes", "tp_order_id", "tp-T")  # no fill recorded
        monkeypatch.setattr(ls, "_log_exit",
                            lambda **k: pytest.fail("should not log"))
        stats = ls.ScalpStats(cycle_started=datetime.now(UTC))
        ls._reconcile_resting_tps(profile_name="p", broker=broker, stats=stats)
        assert pm.get_entry("T", "Yes") is not None

    def test_manage_live_skips_poll_take_profit(self, temp_profile):
        broker = _LiveFakeBroker()
        broker.positions_df = pd.DataFrame([
            {"condition_id": "T", "outcome": "Yes", "question": "q", "shares": 3}])
        pm.record_entry("T", "Yes", entry_price=0.40)
        pm.set_field("T", "Yes", "tp_order_id", "tp-T")
        cfg = ls.ScalpConfig(mode="close_game", take_profit_pct=0.25,
                             flip_drop_pct=None, cg_stop_loss_pct=0.25)
        quotes = {"T": (0.55, 0.57)}  # +37% → poll would TP; live must skip
        stats = ls.ScalpStats(cycle_started=datetime.now(UTC))
        ls._manage_positions(profile_name="p", broker=broker, cfg=cfg,
                             opp_of={}, game_of={}, quotes=quotes,
                             remaining_cash=0.0, stats=stats)
        assert broker.sells == []        # venue's resting TP owns the upside
        assert broker.canceled == []

    def test_manage_live_stop_cancels_resting_tp(self, temp_profile):
        broker = _LiveFakeBroker()
        broker.positions_df = pd.DataFrame([
            {"condition_id": "T", "outcome": "Yes", "question": "q", "shares": 3}])
        pm.record_entry("T", "Yes", entry_price=0.40)
        pm.set_field("T", "Yes", "tp_order_id", "tp-T")
        cfg = ls.ScalpConfig(mode="close_game", take_profit_pct=0.25,
                             flip_drop_pct=None, cg_stop_loss_pct=0.25)
        quotes = {"T": (0.28, 0.30)}  # -30% → stop
        stats = ls.ScalpStats(cycle_started=datetime.now(UTC))
        ls._manage_positions(profile_name="p", broker=broker, cfg=cfg,
                             opp_of={}, game_of={"T": _State()}, quotes=quotes,
                             remaining_cash=0.0, stats=stats)
        assert "tp-T" in broker.canceled   # resting TP killed before the stop
        assert len(broker.sells) == 1      # marketable stop sell placed
        assert stats.stop_losses == 1


class TestSettlementReconcile:
    """Live safety net: catch positions that settled on the venue before the
    in-cycle resolver caught them (the orphaned-loss track-record gap)."""

    def _settlements(self, monkeypatch, rows):
        import kalshi_client as kc
        monkeypatch.setattr(kc, "get_settlements", lambda **k: pd.DataFrame(rows))

    def test_logs_orphaned_loss(self, temp_profile, monkeypatch):
        broker = _LiveFakeBroker()
        pm.record_entry("LAA", "Yes", entry_price=0.60)  # no question stored
        self._settlements(monkeypatch, [
            {"ticker": "LAA", "market_result": "no", "yes_count": 3,
             "no_count": 0, "revenue_cents": 0}])
        logged: list = []
        monkeypatch.setattr(ls, "_log_exit", lambda **k: logged.append(k))
        stats = ls.ScalpStats(cycle_started=datetime.now(UTC))
        ls._reconcile_settlements(profile_name="p", broker=broker, stats=stats)
        assert logged[0]["exit_reason"] == "resolution_lost"
        assert logged[0]["exit_price"] == 0.0
        assert logged[0]["shares"] == 3
        assert logged[0]["question"] == "LAA"   # falls back to ticker
        assert pm.get_entry("LAA", "Yes") is None
        assert stats.resolved_closed == 1

    def test_logs_orphaned_win(self, temp_profile, monkeypatch):
        broker = _LiveFakeBroker()
        pm.record_entry("SEA", "Yes", entry_price=0.60)
        self._settlements(monkeypatch, [
            {"ticker": "SEA", "market_result": "yes", "yes_count": 3,
             "no_count": 0, "revenue_cents": 300}])
        logged: list = []
        monkeypatch.setattr(ls, "_log_exit", lambda **k: logged.append(k))
        stats = ls.ScalpStats(cycle_started=datetime.now(UTC))
        ls._reconcile_settlements(profile_name="p", broker=broker, stats=stats)
        assert logged[0]["exit_reason"] == "resolution_won"
        assert logged[0]["exit_price"] == 1.0
        assert pm.get_entry("SEA", "Yes") is None

    def test_skips_unsettled(self, temp_profile, monkeypatch):
        broker = _LiveFakeBroker()
        pm.record_entry("LAA", "Yes", entry_price=0.60)
        self._settlements(monkeypatch, [])  # nothing settled yet
        monkeypatch.setattr(ls, "_log_exit",
                            lambda **k: pytest.fail("nothing settled → no log"))
        stats = ls.ScalpStats(cycle_started=datetime.now(UTC))
        ls._reconcile_settlements(profile_name="p", broker=broker, stats=stats)
        assert pm.get_entry("LAA", "Yes") is not None  # still tracked

    def test_paper_is_noop(self, temp_profile, monkeypatch):
        broker = _FakeBroker()  # is_live False → paper closes via _close_resolved
        pm.record_entry("LAA", "Yes", entry_price=0.60)
        monkeypatch.setattr(ls, "_log_exit",
                            lambda **k: pytest.fail("paper must not settle-reconcile"))
        stats = ls.ScalpStats(cycle_started=datetime.now(UTC))
        ls._reconcile_settlements(profile_name="p", broker=broker, stats=stats)
        assert pm.get_entry("LAA", "Yes") is not None


class TestForecastSourceDispatch:
    """_forecast_for_ticker routes each --forecast-source to the right backend."""

    def test_consensus_routes_to_edges(self, monkeypatch):
        import edges
        monkeypatch.setattr(edges, "sports_consensus_for_ticker",
                            lambda t, **k: 0.57)
        assert ls._forecast_for_ticker("KXMLBGAME-X-HOU", "consensus") == 0.57

    def test_consensus_none_passthrough(self, monkeypatch):
        import edges
        monkeypatch.setattr(edges, "sports_consensus_for_ticker",
                            lambda t, **k: None)
        assert ls._forecast_for_ticker("KXMLBGAME-X-HOU", "consensus") is None

    def test_unknown_source_returns_none(self):
        assert ls._forecast_for_ticker("KXMLBGAME-X-HOU", "bogus") is None
