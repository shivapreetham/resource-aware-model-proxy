"""Recognising what is already listening on a port, and how to move it.

Transparent mode works by taking the port clients already use and putting the
incumbent server somewhere else. That is only safe if RAMP can (a) tell what
the incumbent *is* and (b) start it again afterwards. Guessing either would
mean stopping someone's server with no way to bring it back, so anything not
positively identified is refused rather than assumed.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Runtime:
    key: str
    label: str
    default_port: int
    #: False when RAMP has no reliable way to restart it, which makes
    #: transparent mode unsafe and therefore refused.
    relocatable: bool = True
    #: How the port is chosen when relaunching.
    relocation: str = ""


OLLAMA = Runtime("ollama", "Ollama", 11434, True, "OLLAMA_HOST environment variable")
LLAMACPP = Runtime("llamacpp", "llama.cpp (llama-server)", 8080, True, "--port argument")
LMSTUDIO = Runtime("lmstudio", "LM Studio", 1234, True, "lms server start --port")
GENERIC = Runtime("openai-generic", "an OpenAI-compatible server", 0, False)

ALL = (OLLAMA, LLAMACPP, LMSTUDIO)


def _get(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def identify(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> Runtime | None:
    """Work out which server owns ``port``, or None if nothing answers.

    Ordered most-specific first: every one of these speaks the OpenAI API on
    /v1, so /v1/models cannot distinguish them and is only a fallback.
    """
    base = f"http://{host}:{port}"

    v = _get(f"{base}/api/version", timeout)
    if isinstance(v, dict) and "version" in v:
        return OLLAMA

    # LM Studio exposes a richer native API alongside the OpenAI one.
    v = _get(f"{base}/api/v0/models", timeout)
    if isinstance(v, dict) and "data" in v:
        return LMSTUDIO

    # llama-server reports its loaded model and sampling defaults here.
    v = _get(f"{base}/props", timeout)
    if isinstance(v, dict) and (
        "default_generation_settings" in v or "model_path" in v
    ):
        return LLAMACPP

    v = _get(f"{base}/v1/models", timeout)
    if isinstance(v, dict) and "data" in v:
        return GENERIC

    return None


def rewrite_flag(argv: list[str], flag: str, value: str) -> list[str]:
    """Set ``flag`` to ``value`` in ``argv``, appending it if absent."""
    out = list(argv)
    try:
        i = out.index(flag)
    except ValueError:
        return [*out, flag, value]
    if i + 1 < len(out):
        out[i + 1] = value
    else:
        out.append(value)
    return out


def rewrite_port_arg(cmdline: list[str], new_port: int, host: str = "127.0.0.1") -> list[str]:
    """Return ``cmdline`` with its listening port changed.

    Used to relaunch a llama-server exactly as the user originally ran it -
    same model, same flags - on a different port. Reconstructing the command
    from scratch is impossible (we don't know their model path or tuning), so
    the running process's own argv is the only reliable source.
    """
    out = list(cmdline)
    for flag, value in (("--port", str(new_port)), ("--host", host)):
        out = rewrite_flag(out, flag, value)
    return out


@dataclass
class Relaunch:
    """Everything needed to start the incumbent again on another port."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    note: str = ""


def plan_relaunch(
    runtime: Runtime,
    new_port: int,
    *,
    cmdline: list[str] | None = None,
    binary: str | None = None,
    host: str = "127.0.0.1",
) -> Relaunch | None:
    """How to bring ``runtime`` back up on ``new_port``, or None if we can't.

    Returning None is a feature: it makes transparent mode refuse instead of
    stopping a server it cannot restart.
    """
    if runtime is OLLAMA:
        if not binary:
            return None
        return Relaunch(
            argv=[binary, "serve"],
            env={"OLLAMA_HOST": f"{host}:{new_port}"},
            note=f"ollama serve with OLLAMA_HOST={host}:{new_port}",
        )

    if runtime is LLAMACPP:
        # Needs the original argv: we cannot guess the model path or flags.
        if not cmdline:
            return None
        argv = rewrite_port_arg(cmdline, new_port, host)
        return Relaunch(argv=argv, note=f"llama-server on port {new_port}")

    if runtime is LMSTUDIO:
        if not binary:
            return None
        return Relaunch(
            argv=[binary, "server", "start", "--port", str(new_port)],
            note=f"lms server start --port {new_port}",
        )

    return None


def describe_unsupported(runtime: Runtime) -> str:
    if runtime is GENERIC:
        return (
            "something OpenAI-compatible is on that port, but RAMP can't tell "
            "what it is and so has no way to start it again. Point your client "
            "at RAMP's own port instead of using transparent mode."
        )
    return f"RAMP doesn't know how to relaunch {runtime.label}."


def stop_command(runtime: Runtime, binary: str | None) -> list[str] | None:
    """A graceful shutdown command, where the runtime provides one.

    LM Studio's server is owned by the desktop app, so terminating the process
    is the wrong way to stop it; its CLI has a proper command.
    """
    if runtime is LMSTUDIO and binary:
        return [binary, "server", "stop"]
    return None
