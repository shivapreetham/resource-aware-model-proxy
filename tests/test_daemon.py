"""Background running: tracking the daemon, and stopping it safely.

The risky half is stopping. Transparent mode has moved someone's model
server to another port, and only a *clean* exit puts it back - so `ramp stop`
must ask over HTTP first and treat killing as a fallback that needs repair
afterwards.
"""
import json

import pytest

from ramp import daemon


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Never touch the real daemon record while testing."""
    monkeypatch.setattr(daemon, "state_dir", lambda: tmp_path)


# -- tracking ------------------------------------------------------------

def test_no_record_means_not_running():
    assert daemon.running() is None


def test_a_stale_pid_is_not_reported_as_running(tmp_path):
    """PIDs are reused. A recorded number proves nothing on its own."""
    daemon.pid_file().write_text(
        json.dumps({"pid": 999_999_999, "url": "http://127.0.0.1:8090"}),
        encoding="utf-8",
    )
    assert daemon.running() is None
    assert not daemon.pid_file().exists(), "a stale record should be cleaned up"


def test_a_corrupt_record_is_ignored():
    daemon.pid_file().write_text("{not json", encoding="utf-8")
    assert daemon.running() is None


def test_our_own_process_is_recognised(monkeypatch):
    import os
    daemon.pid_file().write_text(
        json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:8090"}),
        encoding="utf-8",
    )
    rec = daemon.running()
    assert rec is not None and rec["pid"] == os.getpid()


# -- stopping ------------------------------------------------------------

def test_stopping_nothing_is_a_clean_no_op():
    notes = daemon.stop()
    assert "no RAMP daemon is running" in notes[0]


def test_stop_asks_politely_before_killing(monkeypatch):
    """A hard kill skips the transparent-mode restore, so the graceful path
    must be tried first."""
    import os
    daemon.pid_file().write_text(
        json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:8090"}),
        encoding="utf-8",
    )
    asked = []
    monkeypatch.setattr(daemon, "request_shutdown",
                        lambda url, **k: asked.append(url) or True)
    # Present on the first look, gone afterwards - as a real clean exit goes.
    real_running = daemon.running
    calls = {"n": 0}

    def fake_running():
        calls["n"] += 1
        return real_running() if calls["n"] == 1 else None

    monkeypatch.setattr(daemon, "running", fake_running)

    notes = daemon.stop()
    assert asked, "stop() must try the graceful shutdown endpoint first"
    assert any("cleanly" in n for n in notes)


def test_a_hard_stop_triggers_transparent_repair(monkeypatch):
    """If we had to kill it, the restore never ran - so repair explicitly,
    or the user is left with their model server on the wrong port."""
    import os

    from ramp import transparent

    daemon.pid_file().write_text(
        json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:8090"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(daemon, "request_shutdown", lambda url, **k: False)

    killed = []

    class FakeProc:
        """Enough of psutil.Process for both running() and stop()."""

        def is_running(self):
            return True

        def cmdline(self):
            return ["python", "-m", "ramp"]

        def terminate(self):
            killed.append(True)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(daemon.psutil, "Process", lambda _pid: FakeProc())
    monkeypatch.setattr(transparent, "load_state", lambda: {"target_port": 11434})
    repaired = []
    monkeypatch.setattr(transparent, "repair",
                        lambda: repaired.append(True) or ["put it back"])

    notes = daemon.stop()
    assert killed, "should fall back to terminating"
    assert repaired, "a killed daemon must have its transparent mode repaired"
    assert any("repairing" in n for n in notes)


def test_clean_stop_does_not_repair_unnecessarily(monkeypatch):
    """A graceful exit already restored things; repairing again could start
    a second server."""
    import os

    from ramp import transparent

    daemon.pid_file().write_text(
        json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:8090"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(daemon, "request_shutdown", lambda url, **k: True)
    monkeypatch.setattr(daemon, "running", lambda: None)
    repaired = []
    monkeypatch.setattr(transparent, "repair", lambda: repaired.append(True) or [])

    daemon.stop()
    assert not repaired


# -- paths ---------------------------------------------------------------

def test_log_and_pid_live_under_one_state_dir(tmp_path):
    assert daemon.pid_file().parent == tmp_path
    assert daemon.log_file().parent == tmp_path


def test_foreground_argv_runs_the_same_config():
    argv = daemon.foreground_argv(["--port", "9999"])
    assert argv[-3:] == ["run", "--port", "9999"]
