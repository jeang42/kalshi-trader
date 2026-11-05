"""Unit tests for hdhomerun.py — pure URL building + mocked network/subprocess."""
from pm.mention import hdhomerun as hdhr

# ─── stream_url (pure) ──────────────────────────────────────────────────────

def test_stream_url_default_tuner_and_ip():
    assert hdhr.stream_url("CBS") == "http://192.0.2.50:5004/tuner3/v11.1"


def test_stream_url_network_name_case_insensitive():
    assert hdhr.stream_url("fox") == "http://192.0.2.50:5004/tuner3/v4.1"
    assert hdhr.stream_url("Fox") == "http://192.0.2.50:5004/tuner3/v4.1"


def test_stream_url_numeric_channel_passthrough():
    assert hdhr.stream_url("11.1", tuner=2) == "http://192.0.2.50:5004/tuner2/v11.1"


def test_stream_url_custom_ip():
    assert hdhr.stream_url("NBC", ip="10.0.0.5") == "http://10.0.0.5:5004/tuner3/v5.1"


def test_stream_url_unknown_channel_name_passthrough():
    # Not a known DFW network key and not touched — passed through as-is.
    assert hdhr.stream_url("99.1") == "http://192.0.2.50:5004/tuner3/v99.1"


def test_pipeline_tuner_default_is_3():
    assert hdhr.PIPELINE_TUNER == 3


def test_dfw_channels_known_networks():
    assert hdhr.DFW_CHANNELS["CBS"] == "11.1"
    assert hdhr.DFW_CHANNELS["FOX"] == "4.1"
    assert hdhr.DFW_CHANNELS["NBC"] == "5.1"
    # 8.8, not 8.1: WFAA's ATSC 1.0 feed moved off the 8.1-8.4 mux in the
    # DFW NextGen TV repack. 8.1 is still listed by /lineup.json but has no
    # SignalStrength/SignalQuality field and streams nothing.
    assert hdhr.DFW_CHANNELS["ABC"] == "8.8"


# ─── discover (mocked HTTP) ─────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def test_discover_success(monkeypatch):
    def fake_get(url, timeout=None):
        assert url == "http://192.0.2.50/discover.json"
        return _FakeResp({"FriendlyName": "HDHomeRun FLEX QUATRO", "TunerCount": 4, "DeviceID": "ABC123"})

    monkeypatch.setattr(hdhr.requests, "get", fake_get)
    info = hdhr.discover()
    assert info["FriendlyName"] == "HDHomeRun FLEX QUATRO"
    assert info["TunerCount"] == 4


def test_discover_network_failure_returns_none(monkeypatch):
    def fake_get(url, timeout=None):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(hdhr.requests, "get", fake_get)
    assert hdhr.discover() is None


def test_discover_bad_status_returns_none(monkeypatch):
    def fake_get(url, timeout=None):
        return _FakeResp({}, status=500)

    monkeypatch.setattr(hdhr.requests, "get", fake_get)
    assert hdhr.discover() is None


# ─── channel_present (mocked HTTP) ──────────────────────────────────────────

def test_channel_present_found(monkeypatch):
    def fake_get(url, timeout=None):
        assert url == "http://192.0.2.50/lineup.json"
        return _FakeResp([
            {"GuideNumber": "11.1", "GuideName": "KTVT"},
            {"GuideNumber": "4.1", "GuideName": "KDFW"},
        ])

    monkeypatch.setattr(hdhr.requests, "get", fake_get)
    assert hdhr.channel_present("CBS") is True
    assert hdhr.channel_present("11.1") is True


def test_channel_present_not_found(monkeypatch):
    def fake_get(url, timeout=None):
        return _FakeResp([{"GuideNumber": "11.1", "GuideName": "KTVT"}])

    monkeypatch.setattr(hdhr.requests, "get", fake_get)
    assert hdhr.channel_present("ABC") is False


def test_channel_present_network_failure_returns_false(monkeypatch):
    def fake_get(url, timeout=None):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(hdhr.requests, "get", fake_get)
    assert hdhr.channel_present("CBS") is False


# ─── acquire_lock / release_lock ────────────────────────────────────────────

def test_acquire_lock_no_binary_returns_none_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(hdhr.shutil, "which", lambda name: None)
    with caplog.at_level("INFO"):
        result = hdhr.acquire_lock()
    assert result is None
    assert any("hdhomerun_config not installed" in r.message for r in caplog.records)


def test_acquire_lock_with_binary_but_no_device(monkeypatch):
    monkeypatch.setattr(hdhr.shutil, "which", lambda name: "/usr/bin/hdhomerun_config")
    monkeypatch.setattr(hdhr, "discover", lambda ip=None: None)
    assert hdhr.acquire_lock() is None


def test_acquire_lock_success(monkeypatch):
    monkeypatch.setattr(hdhr.shutil, "which", lambda name: "/usr/bin/hdhomerun_config")
    monkeypatch.setattr(hdhr, "discover", lambda ip=None: {"DeviceID": "ABC123"})

    calls = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output=None, text=None, timeout=None):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(hdhr.subprocess, "run", fake_run)
    result = hdhr.acquire_lock(tuner=3)
    assert result is not None
    assert calls[0][:4] == ["hdhomerun_config", "ABC123", "set", "/tuner3/lockkey"]


def test_release_lock_no_binary_is_noop(monkeypatch):
    monkeypatch.setattr(hdhr.shutil, "which", lambda name: None)
    # Should not raise even though there's nothing to release.
    hdhr.release_lock("somekey")


def test_acquire_lock_subprocess_failure_returns_none(monkeypatch):
    monkeypatch.setattr(hdhr.shutil, "which", lambda name: "/usr/bin/hdhomerun_config")
    monkeypatch.setattr(hdhr, "discover", lambda ip=None: {"DeviceID": "ABC123"})

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "error setting lockkey"

    monkeypatch.setattr(hdhr.subprocess, "run", lambda *a, **k: _Result())
    assert hdhr.acquire_lock() is None
