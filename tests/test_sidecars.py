"""Smoke tests for the sidecar protocol + registry."""

from __future__ import annotations

import types
from datetime import UTC, datetime, timezone

import pytest

from pm.core import sidecars


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test gets a clean registry; restore on exit so prod imports
    that happen later in the session aren't blown away."""
    saved = dict(sidecars._REGISTRY)
    sidecars._REGISTRY.clear()
    yield
    sidecars._REGISTRY.clear()
    sidecars._REGISTRY.update(saved)


def _make_dummy(name: str = "dummy", display: str = "Dummy Sidecar"):
    mod = types.SimpleNamespace()
    mod.NAME = name
    mod.DISPLAY_NAME = display
    mod.health = lambda: sidecars.SidecarHealth(status="ok", detail="all good")
    mod.stats = lambda: {"rows": 7}
    mod.recent_activity = lambda limit=20: []
    return mod


def test_register_and_lookup():
    mod = _make_dummy()
    sidecars.register_sidecar(mod)
    assert sidecars.get_sidecar("dummy") is mod
    assert mod.name == "dummy"
    assert mod.display_name == "Dummy Sidecar"


def test_list_sidecars_sorted_by_display_name():
    a = _make_dummy("a_sidecar", "Zeta")
    b = _make_dummy("b_sidecar", "Alpha")
    sidecars.register_sidecar(a)
    sidecars.register_sidecar(b)
    names = [s.name for s in sidecars.list_sidecars()]
    assert names == ["b_sidecar", "a_sidecar"]  # Alpha sorts before Zeta


def test_missing_name_rejected():
    mod = _make_dummy()
    del mod.NAME
    with pytest.raises(ValueError, match="must define"):
        sidecars.register_sidecar(mod)


def test_missing_callable_rejected():
    mod = _make_dummy()
    mod.stats = "not callable"
    with pytest.raises(ValueError, match="missing required callable"):
        sidecars.register_sidecar(mod)


def test_safe_health_swallows_exceptions():
    def boom() -> sidecars.SidecarHealth:
        raise RuntimeError("upstream API down")

    result = sidecars.safe_health(boom)
    assert result.status == "down"
    assert "upstream API down" in result.detail
    assert "RuntimeError" in result.detail


def test_safe_health_passes_through_ok():
    def good() -> sidecars.SidecarHealth:
        return sidecars.SidecarHealth(status="ok", detail="fine")

    assert sidecars.safe_health(good).status == "ok"


def test_protocol_runtime_check():
    mod = _make_dummy()
    sidecars.register_sidecar(mod)
    # runtime_checkable protocol — duck-typed check against the module
    assert isinstance(mod, sidecars.Sidecar)


def test_now_utc_is_tz_aware():
    ts = sidecars._now_utc()
    assert ts.tzinfo is not None
    # round-trip via SidecarActivityEvent
    ev = sidecars.SidecarActivityEvent(ts=ts, kind="test", message="hi")
    assert ev.ts.tzinfo == UTC


def test_re_register_overwrites():
    a = _make_dummy("same", "Old")
    b = _make_dummy("same", "New")
    sidecars.register_sidecar(a)
    sidecars.register_sidecar(b)
    assert sidecars.get_sidecar("same") is b
    assert len(sidecars.list_sidecars()) == 1
