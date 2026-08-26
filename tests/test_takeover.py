"""Takeover safety.

Takeover rearranges someone's machine, so what matters most is that it
*refuses cleanly* rather than that it succeeds. Every test here is about a
refusal or a rollback leaving the world untouched.
"""
import socket

import pytest

from ramp import takeover
from ramp.takeover import TakeoverError, TakeoverPlan


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
    with pytest.raises(TakeoverError, match="nothing that looks like Ollama"):
        takeover.plan(target_port=free_port())


def test_refuses_when_relocation_port_is_occupied(monkeypatch, busy_port):
    """Ollama must have somewhere to go, or we'd strand it."""
    monkeypatch.setattr(takeover, "_probe", lambda *a, **k: "0.0.0-test")
    with pytest.raises(TakeoverError, match="already in use"):
        takeover.plan(target_port=11434, relocate_port=busy_port)


def test_refuses_when_ollama_binary_is_missing(monkeypatch):
    """Without the binary we could stop Ollama and never restart it."""
    monkeypatch.setattr(takeover, "_probe", lambda *a, **k: "0.0.0-test")
    monkeypatch.setattr(takeover, "_port_free", lambda *a, **k: True)
    monkeypatch.setattr(takeover, "find_ollama_binary", lambda *a, **k: None)
    with pytest.raises(TakeoverError, match="couldn't find the ollama binary"):
        takeover.plan()


def test_a_refused_plan_changes_nothing(monkeypatch):
    """plan() must be pure inspection - no side effects before consent."""
    calls = []
    monkeypatch.setattr(takeover, "_start_relocated",
                        lambda *a, **k: calls.append("started"))
    monkeypatch.setattr(takeover, "_stop_holders",
                        lambda *a, **k: calls.append("stopped"))
    with pytest.raises(TakeoverError):
        takeover.plan(target_port=free_port())
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

    p = TakeoverPlan(
        target_port=11434, relocate_port=11435,
        ollama_bin="ollama", ollama_version="0.0.0", holders=[123],
    )
    monkeypatch.setattr(takeover, "_start_relocated", lambda _p, **k: FakeProc())
    monkeypatch.setattr(
        takeover, "_stop_holders",
        lambda _p, **k: (_ for _ in ()).throw(TakeoverError("still held")),
    )

    with pytest.raises(TakeoverError, match="still held"):
        takeover.execute(p)
    assert terminated, "the relocated Ollama must be stopped on rollback"


def test_execute_starts_replacement_before_stopping_original(monkeypatch):
    """Ordering is the whole safety property: never free the port until the
    replacement is proven healthy."""
    order = []

    class FakeProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    monkeypatch.setattr(
        takeover, "_start_relocated",
        lambda _p, **k: (order.append("start"), FakeProc())[1],
    )
    monkeypatch.setattr(
        takeover, "_stop_holders", lambda _p, **k: (order.append("stop"), [1])[1]
    )
    p = TakeoverPlan(11434, 11435, "ollama", "0.0.0", [1])
    takeover.execute(p)
    assert order == ["start", "stop"]


# -- plan description ----------------------------------------------------

def test_plan_describes_every_step_and_how_to_undo():
    """The consent prompt has to be honest about what it will do."""
    p = TakeoverPlan(11434, 11435, "/usr/bin/ollama", "0.5.0", [42])
    text = p.describe()
    assert "11434" in text and "11435" in text
    assert "42" in text                      # names the process it will stop
    assert "ramp restore" in text or "exits" in text   # says how to undo


def test_find_ollama_binary_prefers_path(monkeypatch):
    monkeypatch.setattr(takeover.shutil, "which", lambda _n: "/usr/bin/ollama")
    assert takeover.find_ollama_binary() == "/usr/bin/ollama"
