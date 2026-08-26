"""Transparent-mode safety.

Transparent mode rearranges someone's machine, so what matters most is that it
*refuses cleanly* rather than that it succeeds. Every test here is about a
refusal or a rollback leaving the world untouched.
"""
import json
import socket

import pytest

from ramp import transparent
from ramp.transparent import TransparentModeError, TransparentPlan


@pytest.fixture
def busy_port():
    """A real port held open, so 'is this free' checks see it occupied."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    yield s.getsockname()[1]
    s.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- refusals ------------------------------------------------------------

def test_refuses_when_nothing_is_on_the_target_port():
    """Taking a port nobody uses is pointless - just bind it directly."""
    with pytest.raises(TransparentModeError, match="nothing is answering"):
        transparent.plan(target_port=free_port())


def test_refuses_when_relocation_port_is_occupied(monkeypatch, busy_port):
    """The incumbent must have somewhere to go, or we'd strand it.

    Note the seam: plan() identifies the runtime via runtimes.identify, not
    _probe. Patching the wrong one made this test quietly depend on a real
    Ollama running on 11434 - it passed on the author's machine and failed
    on every CI runner.
    """
    monkeypatch.setattr(transparent.runtimes, "identify",
                        lambda *a, **k: transparent.runtimes.OLLAMA)
    monkeypatch.setattr(transparent, "find_binary_for", lambda *a, **k: "/usr/bin/ollama")
    with pytest.raises(TransparentModeError, match="already in use"):
        transparent.plan(target_port=11434, relocate_port=busy_port)


def test_refuses_when_the_binary_to_restart_with_is_missing(monkeypatch):
    """Without a way to relaunch it we could stop the server for good."""
    monkeypatch.setattr(transparent.runtimes, "identify",
                        lambda *a, **k: transparent.runtimes.OLLAMA)
    monkeypatch.setattr(transparent, "_port_free", lambda *a, **k: True)
    monkeypatch.setattr(transparent, "find_binary_for", lambda *a, **k: None)
    with pytest.raises(TransparentModeError, match="can't work out how to start it again"):
        transparent.plan()


def test_a_refused_plan_changes_nothing(monkeypatch):
    """plan() must be pure inspection - no side effects before consent."""
    calls = []
    monkeypatch.setattr(transparent, "_start_relocated",
                        lambda *a, **k: calls.append("started"))
    monkeypatch.setattr(transparent, "_stop_holders",
                        lambda *a, **k: calls.append("stopped"))
    with pytest.raises(TransparentModeError):
        transparent.plan(target_port=free_port())
    assert calls == [], "planning must not touch any process"


# -- rollback ------------------------------------------------------------

def test_rolls_back_when_the_old_server_wont_stop(monkeypatch):
    """If we can't free the port, the replacement we started must be undone -
    otherwise the user is left with two Ollamas and a broken setup."""
    terminated = []

    class FakeProc:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            terminated.append(True)

    p = TransparentPlan(
        target_port=11434, relocate_port=11435,
        ollama_bin="ollama", ollama_version="0.0.0", holders=[123],
    )
    monkeypatch.setattr(transparent, "_start_relocated", lambda _p, **k: FakeProc())
    monkeypatch.setattr(
        transparent, "_stop_holders",
        lambda _p, **k: (_ for _ in ()).throw(TransparentModeError("still held")),
    )

    with pytest.raises(TransparentModeError, match="still held"):
        transparent.engage(p)
    assert terminated, "the relocated Ollama must be stopped on rollback"


def test_engage_starts_replacement_before_stopping_original(monkeypatch, tmp_path):
    """Ordering is the whole safety property: never free the port until the
    replacement is proven healthy."""
    monkeypatch.setattr(transparent, "state_path", lambda: tmp_path / "s.json")
    order = []

    class FakeProc:
        pid = 111

        def poll(self):
            return None

        def terminate(self):
            pass

    monkeypatch.setattr(
        transparent, "_start_relocated",
        lambda _p, **k: (order.append("start"), FakeProc())[1],
    )
    monkeypatch.setattr(
        transparent, "_stop_holders", lambda _p, **k: (order.append("stop"), [1])[1]
    )
    p = TransparentPlan(11434, 11435, "ollama", "0.0.0", [1])
    transparent.engage(p)
    assert order == ["start", "stop"]


# -- plan description ----------------------------------------------------

def test_the_consent_prompt_is_one_short_line():
    """A yes/no question should not be three paragraphs. The prompt names
    both ports and what happens; the mechanics live in details()."""
    p = TransparentPlan(11434, 11435, "/usr/bin/ollama", "0.5.0", [42])
    text = p.describe()
    assert "11434" in text and "11435" in text
    assert len(text) < 160, f"too long for a prompt: {len(text)} chars"
    assert text.count(chr(10)) == 0, "should be a single line"


def test_details_still_explain_every_step_and_the_undo():
    """--verbose and bug reports need the full mechanics."""
    p = TransparentPlan(11434, 11435, "/usr/bin/ollama", "0.5.0", [42])
    text = p.details()
    assert "42" in text                      # names the process it will stop
    assert "ramp restore" in text or "undo" in text


def test_find_ollama_binary_prefers_path(monkeypatch):
    monkeypatch.setattr(transparent.shutil, "which", lambda _n: "/usr/bin/ollama")
    assert transparent.find_ollama_binary() == "/usr/bin/ollama"


# -- crash safety --------------------------------------------------------
#
# Regression: restoring only on clean exit is not enough. A hard kill (or a
# crash, or a reboot) skips the in-process handler entirely and leaves Ollama
# moved with nothing on its usual port. Found by killing a live session.


def test_state_is_persisted_so_a_hard_kill_can_be_repaired(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    monkeypatch.setattr(transparent, "state_path", lambda: path)

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            pass

    monkeypatch.setattr(transparent, "_start_relocated", lambda _p, **k: FakeProc())
    monkeypatch.setattr(transparent, "_stop_holders", lambda _p, **k: [7])

    p = TransparentPlan(11434, 11435, "/usr/bin/ollama", "0.0.0", [7])
    transparent.engage(p)

    assert path.exists(), "engaging must record enough to undo it after a crash"
    saved = json.loads(path.read_text())
    assert saved["target_port"] == 11434
    assert saved["relocate_port"] == 11435
    assert saved["ollama_bin"] == "/usr/bin/ollama"
    assert saved["relocated_pid"] == 4242


def test_repair_restarts_ollama_and_clears_state(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "target_port": 11434, "relocate_port": 11435,
        "ollama_bin": "/usr/bin/ollama", "relocated_pid": None,
    }))
    monkeypatch.setattr(transparent, "state_path", lambda: path)
    monkeypatch.setattr(transparent, "find_serving_processes", lambda _p: [])
    monkeypatch.setattr(transparent, "_probe", lambda *a, **k: None)
    restarted = []
    monkeypatch.setattr(
        transparent, "_restart_with",
        lambda argv, env, port, **k: (restarted.append((argv[0], port)), ["restarted"])[1],
    )

    notes = transparent.repair()
    assert restarted == [("/usr/bin/ollama", 11434)]
    assert not path.exists(), "a completed repair must not repeat on next run"
    assert notes


def test_repair_is_a_noop_when_there_is_nothing_to_fix(monkeypatch, tmp_path):
    monkeypatch.setattr(transparent, "state_path", lambda: tmp_path / "absent.json")
    notes = transparent.repair()
    assert "nothing to restore" in notes[0]


def test_repair_does_not_restart_when_ollama_is_already_back(monkeypatch, tmp_path):
    """The user may have restarted Ollama themselves - don't start a second."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "target_port": 11434, "relocate_port": 11435,
        "ollama_bin": "ollama", "relocated_pid": None,
    }))
    monkeypatch.setattr(transparent, "state_path", lambda: path)
    monkeypatch.setattr(transparent, "find_serving_processes", lambda _p: [])
    monkeypatch.setattr(transparent, "_probe", lambda *a, **k: "0.32.0")
    called = []
    monkeypatch.setattr(transparent, "_restart_with",
                        lambda *a, **k: called.append(1) or [])

    notes = transparent.repair()
    assert not called, "must not start a second Ollama"
    assert "already answering" in " ".join(notes)
    assert not path.exists()
