"""Fragments archived from live test files when fade_nearclose +
back_nearclose retired (2026-08-24). The five WHOLE test files for the
arms moved to attic/tests/ as-is; these are the pieces that lived inside
files that still cover live code. Reproduced as data, not runnable tests.
"""

# --- tests/test_shutdown.py: TestArmsHonourStop archived ---
# class TestArmsHonourStop:
#     """Once a stop is requested, the arms' per-market loops stop before the
#     next network call — the check is at the top of each loop, not a hope."""
#
#     @pytest.mark.parametrize("mod,env,row_sql", [
#         ("pm.arms.fade_nearclose", "FADE_NEARCLOSE_DB",
#          "INSERT INTO positions (ticker,event_id,no_entry_cost,count,opened_at,close_time) "
#          "VALUES ('KXT-1','E',0.85,11,1,'2020-01-01T00:00:00Z')"),
#         ("pm.arms.back_nearclose", "BACK_NEARCLOSE_DB",
#          "INSERT INTO positions (ticker,event_id,entry_cost,count,opened_at,close_time) "
#          "VALUES ('KXT-1','E',0.94,10,1,'2020-01-01T00:00:00Z')"),
#     ])
#     def test_resolve_loop_breaks_without_touching_the_venue(self, tmp_path, monkeypatch,
#                                                             mod, env, row_sql):
#         import importlib
#         from unittest.mock import patch
#         monkeypatch.setenv(env, str(tmp_path / f"{mod}.sqlite"))
#         m = importlib.import_module(mod)
#         con = m._con()
#         con.execute(row_sql)
#         con.commit()
#         shutdown._handler(signal.SIGTERM, None)
#         with patch.object(m.kc, "lookup_markets_by_id",
#                           side_effect=AssertionError("venue must not be called")), \
#              patch.object(m.kc, "get_market", side_effect=AssertionError("venue"),
#                           create=True):
#             assert m._resolve(con) == 0
#         # the row is still open — nothing was half-done
#         assert con.execute("SELECT resolved FROM positions").fetchone() == (0,)

# --- tests/test_store_schemas.py: STORES -= both arms ---
#     ("pm.arms.fade_nearclose", "FADE_NEARCLOSE_DB", "_con", 3),
#     ("pm.arms.back_nearclose", "BACK_NEARCLOSE_DB", "_con", 3),

# --- tests/test_store_schemas.py: LEGACY -= both arms ---
#     "pm.arms.fade_nearclose": (
#         "FADE_NEARCLOSE_DB",
#         """CREATE TABLE positions (
#             ticker TEXT PRIMARY KEY, event_id TEXT, category TEXT, question TEXT,
#             yes_bid REAL, yes_mid REAL, no_entry_cost REAL, count INTEGER,
#             opened_at INTEGER, close_time TEXT,
#             resolved INTEGER DEFAULT 0, resolved_yes INTEGER, pnl REAL, closed_at INTEGER)""",
#         [("A", "E1", "Crypto", "q", 0.15, 0.15, 0.85, 11, 100, "t", 1, 0, 1.35, 200),
#          ("B", "E2", "Crypto", "q", 0.18, 0.18, 0.82, 12, 100, "t", 0, None, None, None)],
#         "INSERT INTO positions (ticker,event_id,no_entry_cost,count,opened_at) VALUES ('X','E',1.5,1,1)",
#     ),
#     "pm.arms.back_nearclose": (
#         "BACK_NEARCLOSE_DB",
#         """CREATE TABLE positions (
#             ticker TEXT PRIMARY KEY, event_id TEXT, category TEXT, kind TEXT,
#             question TEXT, yes_ask REAL, yes_mid REAL, entry_cost REAL,
#             spread REAL, count INTEGER, stake REAL,
#             opened_at INTEGER, close_time TEXT,
#             resolved INTEGER DEFAULT 0, resolved_yes INTEGER,
#             pnl REAL, closed_at INTEGER)""",
#         [("A", "E1", "Crypto", "crypto", "q", 0.94, 0.94, 0.94, 0.01, 10, 10.0, 100, "t", 1, 1, 0.53, 200),
#          ("B", "E2", "Crypto", "crypto", "q", 0.95, 0.95, 0.95, 0.01, 10, 10.0, 100, "t", 0, None, None, None)],
#         "INSERT INTO positions (ticker,event_id,entry_cost,count,opened_at) VALUES ('X','E',0.9,0,1)",
#     ),

# --- tests/test_oos_ledger.py: arm-params test archived ---
#     def test_fade_nearclose_declares_its_selection_params(self):
#         from pm.arms import fade_nearclose
#         p = fade_nearclose._decision_params()
#         assert {"band_lo", "band_hi", "excluded_series_prefixes"} <= set(p)

# --- tests/test_provenance.py: provenance arm test archived ---
#     def test_arm_position_row_carries_commit_sha(self, tmp_path, monkeypatch):
#         # Was fade_props until 2026-08-23; repointed to back_nearclose when
#         # that arm was retired to attic/. The arm here is only a REPRESENTATIVE
#         # of the shared "stamp the row you open" contract, so any live arm
#         # serves — but one of them must, or the contract goes untested.
#         from pm.arms import back_nearclose as bn
#         monkeypatch.setenv("BACK_NEARCLOSE_DB", str(tmp_path / "bn.sqlite"))
#         con = bn._con()
#         # the arm's own INSERT path is what stamps; exercise it through _open
#         # with one qualifying candidate row of the discovery frame's shape
#         import pandas as pd
#         df = pd.DataFrame([{"id": "KXBTCD-1-T50", "condition_id": "KXBTCD-1-T50",
#                             "event_id": "EV1", "question": "Q?", "category": "Crypto",
#                             "best_bid": 0.93, "best_ask": 0.95, "yes_mid": 0.94,
#                             "volume": 500, "last_trade_price": 0.94,
#                             "close_time": "2099-01-01T00:00:00Z"}])
#         assert bn._open(con, df) == 1
#         got = con.execute("SELECT commit_sha FROM positions").fetchall()
#         assert got and got[0][0] == pv.label()

# --- tests/test_underlying_data.py: TestZForEntryRouting ---
# class TestZForEntryRouting:
#     CLOSE = "2099-01-01T12:00:00Z"      # far future -> hours_to_close > 0
#
#     def test_commodity_routes_to_underlying_data(self, monkeypatch):
#         import time
#         monkeypatch.setattr(ud, "get_spot", lambda r: (4399.7, 60.0))
#         monkeypatch.setattr(ud, "hourly_sigma", lambda r, **k: 0.002)
#         spot, sigma, z = fn.z_for_entry("KXGOLDH-26AUG0400-T4350", self.CLOSE)
#         assert spot == 4399.7 and sigma == 0.002
#         assert z is not None and z > 0            # spot above strike
#
#     def test_stale_commodity_quote_refused(self, monkeypatch):
#         monkeypatch.setattr(ud, "get_spot",
#                             lambda r: (4399.7, fn.MAX_SPOT_AGE_S + 1))
#         monkeypatch.setattr(ud, "hourly_sigma", lambda r, **k: 0.002)
#         assert fn.z_for_entry("KXGOLDH-26AUG0400-T4350", self.CLOSE) == \
#             (None, None, None)
#
#     def test_crypto_still_routes_to_coinbase(self, monkeypatch):
#         from pm.feeds import crypto_data
#         monkeypatch.setattr(crypto_data, "get_spot_price", lambda a: 64963.0)
#         monkeypatch.setattr(crypto_data, "hourly_sigma", lambda a, **k: 0.003)
#         called = []
#         monkeypatch.setattr(ud, "get_spot", lambda r: called.append(r))
#         spot, _, z = fn.z_for_entry("KXBTCD-26AUG0117-T62749.99", self.CLOSE)
#         assert spot == 64963.0 and z is not None
#         assert called == []                        # never touched Yahoo
#
#     def test_unsupported_asset_no_network(self, monkeypatch):
#         hit = []
#         monkeypatch.setattr(ud, "get_spot", lambda r: hit.append(r))
#         assert fn.z_for_entry("KXRAIN-26AUG01-T5", self.CLOSE) == \
#             (None, None, None)
#         assert hit == []

# --- tests/test_oos_ledger.py: TestArmsWiring ---
# class TestArmsWiring:
#     """The shipped arms must expose selection parameters — a tracker nothing
#     registers with tracks nothing."""
#
#
#     def test_back_nearclose_declares_its_selection_params(self):
#         from pm.arms import back_nearclose
#         p = back_nearclose._decision_params()
#         assert {"band_lo", "band_hi", "excluded_underlyings"} <= set(p)
#
#     def test_changing_an_exclusion_changes_the_fingerprint(self):
#         """The specific re-fit that motivated all of this."""
#         from pm.arms import back_nearclose
#         base = back_nearclose._decision_params()
#         widened = dict(base, excluded_underlyings=sorted(
#             set(base["excluded_underlyings"]) | {"BRENT"}))
#         assert oos.fingerprint(base) != oos.fingerprint(widened)
#
