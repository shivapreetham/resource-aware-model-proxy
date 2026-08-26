"""Runtime detection and relaunch planning.

Transparent mode stops a server that someone is using. The single property
worth guarding is that it only ever does so when it can put that server
back - so most of these tests are about refusing.
"""
import pytest

from ramp import runtimes
from ramp.runtimes import (
    GENERIC,
    LLAMACPP,
    LMSTUDIO,
    OLLAMA,
    identify,
    plan_relaunch,
    rewrite_flag,
    rewrite_port_arg,
    stop_command,
)

# -- identification ------------------------------------------------------

def fake_endpoints(monkeypatch, responses: dict):
    """Serve canned JSON for specific paths; 'not found' for everything else."""
    def _get(url, timeout=2.0):
        for path, payload in responses.items():
            if url.endswith(path):
                return payload
        return None
    monkeypatch.setattr(runtimes, "_get", _get)


def test_identifies_ollama(monkeypatch):
    fake_endpoints(monkeypatch, {"/api/version": {"version": "0.33.0"}})
    assert identify(11434) is OLLAMA


def test_identifies_lm_studio(monkeypatch):
    fake_endpoints(monkeypatch, {
        "/api/v0/models": {"data": [{"id": "llama-3"}]},
        "/v1/models": {"data": [{"id": "llama-3"}]},
    })
    assert identify(1234) is LMSTUDIO


def test_identifies_llama_cpp(monkeypatch):
    fake_endpoints(monkeypatch, {
        "/props": {"default_generation_settings": {}, "model_path": "/m/x.gguf"},
        "/v1/models": {"data": []},
    })
    assert identify(8080) is LLAMACPP


def test_unknown_openai_server_is_generic_and_not_relocatable(monkeypatch):
    """Everything here speaks /v1, so that alone identifies nothing. An
    unrecognised server must not be stopped - we couldn't restart it."""
    fake_endpoints(monkeypatch, {"/v1/models": {"data": []}})
    rt = identify(9999)
    assert rt is GENERIC
    assert rt.relocatable is False


def test_nothing_listening(monkeypatch):
    fake_endpoints(monkeypatch, {})
    assert identify(9999) is None


def test_lm_studio_is_not_mistaken_for_llama_cpp(monkeypatch):
    """Order matters: LM Studio also answers /v1, so it must be checked
    before the generic fallback."""
    fake_endpoints(monkeypatch, {
        "/api/v0/models": {"data": []},
        "/props": {"model_path": "/x"},
        "/v1/models": {"data": []},
    })
    assert identify(1234) is LMSTUDIO


# -- argv rewriting (how llama.cpp gets relocated) -----------------------

def test_rewrites_an_existing_port_flag():
    cmd = ["llama-server", "-m", "/m/q.gguf", "--port", "8080", "--ctx-size", "4096"]
    out = rewrite_port_arg(cmd, 8081)
    assert out[out.index("--port") + 1] == "8081"
    assert "-m" in out and "/m/q.gguf" in out      # model preserved
    assert "--ctx-size" in out and "4096" in out   # tuning preserved


def test_appends_a_port_flag_when_absent():
    out = rewrite_port_arg(["llama-server", "-m", "/m/q.gguf"], 8081)
    assert out[out.index("--port") + 1] == "8081"


def test_rewrite_flag_handles_a_trailing_flag_with_no_value():
    assert rewrite_flag(["x", "--port"], "--port", "9") == ["x", "--port", "9"]


def test_rewriting_never_drops_arguments():
    """Losing a flag would silently change how the model runs."""
    cmd = ["llama-server", "-m", "/m/q.gguf", "-ngl", "999", "--flash-attn"]
    out = rewrite_port_arg(cmd, 9999)
    for token in cmd:
        assert token in out


# -- relaunch planning ---------------------------------------------------

def test_ollama_relaunch_uses_the_host_env_var():
    r = plan_relaunch(OLLAMA, 11435, binary="/usr/bin/ollama")
    assert r.argv == ["/usr/bin/ollama", "serve"]
    assert r.env["OLLAMA_HOST"] == "127.0.0.1:11435"


def test_ollama_relaunch_refused_without_a_binary():
    assert plan_relaunch(OLLAMA, 11435, binary=None) is None


def test_llama_cpp_relaunch_needs_the_original_command():
    """Its model path and flags cannot be reconstructed, so without the
    running process's argv we must refuse."""
    assert plan_relaunch(LLAMACPP, 8081, cmdline=None) is None

    r = plan_relaunch(LLAMACPP, 8081, cmdline=["llama-server", "-m", "/m/q.gguf"])
    assert r is not None
    assert "/m/q.gguf" in r.argv


def test_lm_studio_relaunch_uses_its_cli():
    r = plan_relaunch(LMSTUDIO, 1235, binary="/home/u/.lmstudio/bin/lms")
    assert r.argv == ["/home/u/.lmstudio/bin/lms", "server", "start", "--port", "1235"]


def test_lm_studio_relaunch_refused_without_the_cli():
    assert plan_relaunch(LMSTUDIO, 1235, binary=None) is None


def test_generic_server_is_never_relocatable():
    assert plan_relaunch(GENERIC, 1234, binary="/anything") is None


@pytest.mark.parametrize("rt", [OLLAMA, LLAMACPP, LMSTUDIO])
def test_every_supported_runtime_declares_how_it_relocates(rt):
    assert rt.relocatable
    assert rt.relocation, "the consent prompt shows this - it must not be blank"


# -- stopping ------------------------------------------------------------

def test_lm_studio_is_stopped_via_its_cli_not_by_killing_it():
    """Its server belongs to the desktop app; terminating the process is the
    wrong way to shut it down."""
    assert stop_command(LMSTUDIO, "/bin/lms") == ["/bin/lms", "server", "stop"]


def test_other_runtimes_have_no_special_stop_command():
    assert stop_command(OLLAMA, "/bin/ollama") is None
    assert stop_command(LLAMACPP, None) is None
