"""shutdown — cooperative SIGTERM handling (R-20)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from pm.core import shutdown


@pytest.fixture(autouse=True)
def _fresh():
    shutdown.reset()
    yield
    shutdown.reset()


class TestFlag:
    def test_not_requested_by_default(self):
        assert shutdown.requested() is False
        assert shutdown.sleep(0.01) is False   # slept the full time, no stop

    def test_handler_sets_flag_and_sleep_returns_early(self):
        shutdown._handler(signal.SIGTERM, None)
        assert shutdown.requested() is True
        t = time.monotonic()
        assert shutdown.sleep(5) is True
        assert time.monotonic() - t < 1

    def test_second_signal_escalates(self):
        shutdown._handler(signal.SIGTERM, None)
        with pytest.raises(SystemExit) as ei:
            shutdown._handler(signal.SIGTERM, None)
        assert ei.value.code == 128 + signal.SIGTERM


class TestDeferred:
    def test_signal_inside_deferred_is_held_then_honoured(self, caplog):
        with caplog.at_level("WARNING"):
            with shutdown.deferred("live order"):
                shutdown._handler(signal.SIGTERM, None)
                # inside: NOT requested yet — the record step must complete
                assert shutdown.requested() is False
            # outside: now requested
            assert shutdown.requested() is True
        assert any("deferred until it completes" in r.message for r in caplog.records)
        assert any("honouring deferred" in r.message for r in caplog.records)

    def test_reentrant_only_fires_at_outermost_exit(self):
        with shutdown.deferred("outer"):
            with shutdown.deferred("inner"):
                shutdown._handler(signal.SIGINT, None)
            assert shutdown.requested() is False   # still inside outer
        assert shutdown.requested() is True

    def test_deferred_without_signal_is_a_noop(self):
        with shutdown.deferred("x"):
            pass
        assert shutdown.requested() is False

    def test_exception_inside_deferred_still_releases(self):
        with pytest.raises(ValueError):
            with shutdown.deferred("x"):
                raise ValueError("boom")
        assert shutdown._deferred_depth == 0


class TestRealSignal:
    """End-to-end in a subprocess: a loop that finishes its step on SIGTERM."""

    def test_sigterm_finishes_step_and_exits_cleanly(self, tmp_path):
        script = textwrap.dedent(f"""
            import sys, time, logging
            sys.path.insert(0, {os.getcwd()!r})
            logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
            from pm.core import shutdown
            shutdown.install()
            out = open({str(tmp_path / 'out.txt')!r}, "w")
            print("ready", flush=True)
            while not shutdown.requested():
                with shutdown.deferred("step"):
                    out.write("start\\n"); out.flush()
                    time.sleep(0.6)            # SIGTERM lands in here
                    out.write("end\\n"); out.flush()
                if shutdown.sleep(0.05):
                    break
            out.write("clean\\n"); out.close()
        """)
        p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "ready"
        time.sleep(0.2)                       # inside the first step
        p.send_signal(signal.SIGTERM)
        rc = p.wait(timeout=10)
        assert rc == 0, p.stderr.read()
        lines = (tmp_path / "out.txt").read_text().split()
        # the step that was in flight completed, then a clean exit
        assert lines[-1] == "clean"
        assert lines.count("start") == lines.count("end")


# TestArmsHonourStop moved to attic/tests/test_nearclose_fragments.py on
# 2026-08-24: its parametrize covered exactly the sqlite arms, and the last
# two (fade_nearclose, back_nearclose) retired that day. The CONTRACT — a
# stop request breaks per-market loops before the next network call — still
# holds for any future arm; re-inline the class when one ships.
