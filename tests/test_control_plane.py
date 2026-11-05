"""Offline tests for the control-plane collector (no systemd/network needed)."""

from __future__ import annotations

from control_plane import collect


class TestParseSystemctl:
    def test_parses_blocks(self):
        text = (
            "Id=arb-scan.service\nActiveState=active\nSubState=running\n"
            "UnitFileState=enabled\nActiveEnterTimestamp=Wed 2026-06-19 10:00:00\n"
            "\n"
            "Id=news-capture.service\nActiveState=inactive\nSubState=dead\n"
            "UnitFileState=disabled\nActiveEnterTimestamp=\n"
        )
        out = collect._parse_systemctl_show(text)
        assert out["arb-scan.service"]["ActiveState"] == "active"
        assert out["news-capture.service"]["ActiveState"] == "inactive"

    def test_empty(self):
        assert collect._parse_systemctl_show("") == {}


class TestCollectorsDegradeGracefully:
    """Every collector must return something even when its source explodes."""

    def test_service_states_returns_all_known(self, monkeypatch):
        # No systemd → all unknown, but still one row per configured service.
        monkeypatch.setattr(collect.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("no systemd")))
        rows = collect.service_states()
        expected = sum(len(v) for v in collect.SERVICES.values())
        assert len(rows) == expected
        assert all(r["active"] == "unknown" and not r["ok"] for r in rows)

    def test_panel_stats_handles_broken_source(self, monkeypatch):
        from pm.capture import near_close_capture
        monkeypatch.setattr(near_close_capture, "stats",
                            lambda: (_ for _ in ()).throw(RuntimeError("db gone")))
        panels = collect.panel_stats()
        nc = [p for p in panels if p["name"] == "Near-close panel"][0]
        assert nc["ok"] is False and nc["count"] == "—"

    def test_arm_pnl_shape(self, monkeypatch):
        import pandas as pd

        from pm.ledger import exits_analysis as ea
        monkeypatch.setattr(ea, "load_exits",
                            lambda p: pd.DataFrame({"pnl_dollars": [1.0, -2.0, 3.0]}))
        arms = collect.arm_pnl()
        assert arms and arms[0]["n"] == 3
        assert arms[0]["net"] == 2.0 and arms[0]["win"] == 67

    def test_snapshot_keys(self, monkeypatch):
        monkeypatch.setattr(collect, "service_states", lambda: [])
        monkeypatch.setattr(collect, "panel_stats", lambda: [])
        monkeypatch.setattr(collect, "arm_pnl", lambda: [])
        monkeypatch.setattr(collect, "sidecar_health", lambda: [])
        monkeypatch.setattr(collect, "live_account", lambda: None)
        d = collect.snapshot()
        for k in ("generated", "services", "panels", "arms", "services_up"):
            assert k in d
