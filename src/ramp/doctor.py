"""``ramp doctor`` - check that this machine can actually run RAMP.

Every check answers one question a new user would otherwise have to work out
from a stack trace, and every failure carries the command that fixes it.
Checks are pure data (a list of ``Check``) so the CLI can render them and
tests can assert on them.
"""
from __future__ import annotations

import shutil
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Literal

import psutil

from . import runtimes
from .autoconfig import AutoConfigError, fetch_ollama_models
from .config import Config, ConfigError
from .monitor import ResourceMonitor

Status = Literal["ok", "warn", "fail"]
_MB = 1024 * 1024


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""


def check_python() -> Check:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v < (3, 10):
        return Check(
            "Python", "fail", f"{ver} (RAMP needs 3.10+)",
            "Install Python 3.10 or newer.",
        )
    return Check("Python", "ok", ver)


def check_memory() -> Check:
    vm = psutil.virtual_memory()
    total = vm.total / _MB
    avail = vm.available / _MB
    detail = f"{total / 1024:.1f} GB total, {avail / 1024:.1f} GB available"
    if total < 4096:
        return Check(
            "System RAM", "warn", detail,
            "Under 4 GB total - only very small models will fit.",
        )
    if avail < 1024:
        return Check(
            "System RAM", "warn", detail,
            "Very little free right now; RAMP will start on a small tier.",
        )
    return Check("System RAM", "ok", detail)


def check_gpu() -> Check:
    gpu = ResourceMonitor().sample().gpu
    if gpu is None:
        return Check(
            "GPU / VRAM", "warn", "no NVIDIA GPU detected",
            "Not required - RAMP runs CPU-only, and VRAM limits simply "
            "aren't enforced. (Only NVIDIA is supported today.)",
        )
    return Check(
        "GPU / VRAM", "ok",
        f"{gpu.name} - {gpu.total_mb / 1024:.1f} GB total, "
        f"{gpu.free_raw_mb / 1024:.1f} GB free",
    )


def check_disk(path: str = ".") -> Check:
    try:
        usage = shutil.disk_usage(path)
    except OSError as e:
        return Check("Disk", "warn", f"couldn't read {path!r}: {e}")
    free = usage.free / _MB
    detail = f"{free / 1024:.1f} GB free"
    if free < 5120:
        return Check(
            "Disk", "warn", detail,
            "Below RAMP's default disk floor (5 GB); upgrades will be gated.",
        )
    return Check("Disk", "ok", detail)


def check_ollama(url: str = "http://127.0.0.1:11434") -> list[Check]:
    try:
        with urllib.request.urlopen(f"{url}/api/version", timeout=3) as r:
            import json as _json
            version = _json.loads(r.read()).get("version", "?")
    except (urllib.error.URLError, OSError, ValueError):
        return [
            Check(
                "Ollama server", "fail", f"not reachable at {url}",
                "Install from https://ollama.com and run 'ollama serve' - or "
                "use the llama.cpp backend with an explicit config (-c).",
            )
        ]
    server = Check("Ollama server", "ok", f"v{version} at {url}")

    try:
        models = fetch_ollama_models(url)
    except AutoConfigError as e:
        return [server, Check("Ollama models", "fail", str(e),
                              "Pull one, e.g. 'ollama pull qwen2.5:3b'.")]

    names = ", ".join(m.name for m in sorted(models, key=lambda m: m.size_mb)[:4])
    detail = f"{len(models)} installed ({names}{'...' if len(models) > 4 else ''})"
    if len(models) < 2:
        return [server, Check(
            "Ollama models", "warn", detail,
            "RAMP needs at least 2 differently-sized models to have a ladder "
            "to move along. Pull a smaller one, e.g. 'ollama pull qwen2.5:0.5b'.",
        )]
    return [server, Check("Ollama models", "ok", detail)]



def check_runtimes() -> Check:
    """What model servers are running, and can transparent mode use them?

    Transparent mode only works against a server RAMP can identify *and*
    restart, so this reports both facts rather than just "something is
    listening".
    """
    found = []
    for rt in runtimes.ALL:
        detected = runtimes.identify(rt.default_port, timeout=1.5)
        if detected is not None:
            found.append((detected, rt.default_port))

    if not found:
        return Check(
            "Model servers", "warn",
            "none detected on the usual ports "
            f"({', '.join(str(r.default_port) for r in runtimes.ALL)})",
            "Transparent mode needs one running. Start Ollama, llama-server, "
            "or LM Studio - or skip it and point clients at RAMP directly.",
        )

    desc = ", ".join(f"{rt.label} on {port}" for rt, port in found)
    unusable = [rt for rt, _ in found if not rt.relocatable]
    if unusable:
        return Check(
            "Model servers", "warn", desc,
            "Transparent mode can't relocate an unidentified server; use "
            "RAMP's own port for that one.",
        )
    return Check("Model servers", "ok", f"{desc} (transparent mode supported)")


def check_llama_server(binary: str = "llama-server") -> Check:
    found = shutil.which(binary)
    if found is None:
        return Check(
            "llama.cpp", "warn", "llama-server not on PATH",
            "Only needed for the 'llama' backend; the Ollama backend "
            "doesn't use it.",
        )
    return Check("llama.cpp", "ok", found)


def check_port(host: str = "127.0.0.1", port: int = 8090) -> Check:
    with socket.socket() as s:
        s.settimeout(1.0)
        if s.connect_ex((host, port)) == 0:
            # Something is listening - is it us?
            try:
                with urllib.request.urlopen(
                    f"http://{host}:{port}/ramp/status", timeout=2
                ):
                    return Check(
                        "Listen port", "ok", f"{port} - a RAMP daemon is already running",
                        "Query it with 'ramp status', or stop it before starting another.",
                    )
            except (urllib.error.URLError, OSError):
                return Check(
                    "Listen port", "fail", f"{port} is in use by something else",
                    "Pick another port in your config's listen.port.",
                )
    return Check("Listen port", "ok", f"{port} is free")


def check_config(path: str | None) -> Check | None:
    if path is None:
        return None
    try:
        cfg = Config.load(path)
    except FileNotFoundError:
        return Check("Config", "fail", f"{path} not found",
                     "Generate one with 'ramp init'.")
    except (ConfigError, ValueError) as e:
        return Check("Config", "fail", f"{path}: {e}",
                     "Fix the file, or regenerate it with 'ramp init'.")
    names = " > ".join(t.name for t in cfg.tiers)
    return Check("Config", "ok", f"{path}: {len(cfg.tiers)} tier(s) [{names}]")


def run_checks(
    config_path: str | None = None,
    ollama_url: str = "http://127.0.0.1:11434",
    port: int = 8090,
) -> list[Check]:
    checks = [check_python(), check_memory(), check_gpu(), check_disk()]
    checks += check_ollama(ollama_url)
    checks.append(check_runtimes())
    checks.append(check_llama_server())
    checks.append(check_port(port=port))
    cfg_check = check_config(config_path)
    if cfg_check is not None:
        checks.append(cfg_check)
    return checks


def worst(checks: list[Check]) -> Status:
    if any(c.status == "fail" for c in checks):
        return "fail"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "ok"
