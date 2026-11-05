"""Tests for the pure helpers in shared.py.

Streamlit-rendering functions (paper_trading_banner, simulated_watermark,
live_price_panel) aren't exercised here — they require a running Streamlit
context. We test the underlying logic via is_sandbox_active() and by
checking that simulated_watermark mutates a plain Plotly figure when sandbox
is active and leaves it alone otherwise.
"""

from unittest.mock import patch

import plotly.graph_objects as go
import pytest

from pm.core import profiles
from pm.ui import shared


@pytest.fixture
def profiles_root(tmp_path, monkeypatch):
    """Redirect PROFILES_ROOT to a tmp directory for each test, matching the
    pattern used in test_profiles.py."""
    monkeypatch.setattr(profiles, "PROFILES_ROOT", tmp_path / "data")
    monkeypatch.setattr(profiles, "ACTIVE_PROFILE_FILE",
                        tmp_path / "data" / ".active_profile")
    profiles.initialize_profiles(project_root=tmp_path)
    return tmp_path / "data"


class TestIsSandboxActive:
    def test_default_jeff_is_not_sandbox(self, profiles_root):
        assert shared.is_sandbox_active() is False

    def test_sandbox_profile_is_sandbox(self, profiles_root):
        profiles.set_active_profile("sandbox")
        assert shared.is_sandbox_active() is True

    def test_dex_is_not_sandbox(self, profiles_root):
        profiles.set_active_profile("dex")
        assert shared.is_sandbox_active() is False

    def test_safe_when_profiles_uninitialised(self, monkeypatch):
        """If profiles can't be queried for any reason, returns False — never
        raises. Pages should fall through to non-sandbox UX."""
        def boom():
            raise OSError("simulated profile system failure")
        monkeypatch.setattr(profiles, "active_profile", boom)
        assert shared.is_sandbox_active() is False


class TestSimulatedWatermark:
    """The watermark function mutates a Plotly Figure in-place by appending
    an annotation. We can assert that without needing a Streamlit context."""

    def test_noop_when_not_sandbox(self, profiles_root):
        fig = go.Figure()
        shared.simulated_watermark(fig)
        assert len(fig.layout.annotations) == 0

    def test_adds_annotation_when_sandbox(self, profiles_root):
        profiles.set_active_profile("sandbox")
        fig = go.Figure()
        shared.simulated_watermark(fig)
        # Should have added exactly one annotation
        assert len(fig.layout.annotations) == 1
        ann = fig.layout.annotations[0]
        assert ann.text == "SIMULATED"
        assert ann.xref == "paper"
        assert ann.yref == "paper"
        # Visible-but-faint: alpha < 0.5
        assert "0.25" in ann.font.color or "rgba" in ann.font.color.lower()

    def test_custom_text(self, profiles_root):
        profiles.set_active_profile("sandbox")
        fig = go.Figure()
        shared.simulated_watermark(fig, text="DEMO")
        assert fig.layout.annotations[0].text == "DEMO"

    def test_safe_with_none_figure(self, profiles_root):
        profiles.set_active_profile("sandbox")
        # Pages may guard against empty figures; calling with None must not raise.
        shared.simulated_watermark(None)   # should be a no-op, not raise


class TestSandboxConstant:
    def test_matches_profiles_default(self):
        """The sandbox name in shared must match the one in DEFAULT_PROFILES."""
        assert shared.SANDBOX_PROFILE in profiles.DEFAULT_PROFILES
