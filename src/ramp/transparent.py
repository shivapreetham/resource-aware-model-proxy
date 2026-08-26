"""Transparent mode: put RAMP on the port your tools already use.

Pointing every client at a new base_url is friction, and friction is why
good infrastructure goes unused. The alternative is for RAMP to occupy
Ollama's port and relocate Ollama behind it - then every tool that already
speaks to ``localhost:11434`` flows through RAMP with no configuration at
all, and can't tell the difference except that the model quietly resizes
itself under memory pressure.

This is an invasive thing to do to someone's machine, so the rules are:

* **Ask first.** Never taken without explicit consent.
* **Never leave things broken.** The replacement Ollama is started and
  proven healthy *before* the original is stopped, and any failure rolls
  back to the original arrangement.
* **Always reversible.** ``restore()`` puts Ollama back on its own port,
  and runs automatically when RAMP exits.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import psutil


class TransparentModeError(RuntimeError):
    """Raised when transparent mode can not be enabled safely. Nothing has changed."""


def _probe(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> str | None:
    """Return the Ollama version listening on ``port``, or None."""
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/version", timeout=timeout
        ) as r:
            import json

            return json.loads(r.read()).get("version")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) != 0


def find_ollama_binary(configured: str = "ollama") -> str | None:
    found = shutil.which(configured)
    if found:
        return found
    # Common per-user install locations that aren't always on PATH.
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "Programs", "Ollama", "ollama.exe"))
    candidates += [
        "/usr/local/bin/ollama",
        "/opt/homebrew/bin/ollama",
        "/usr/bin/ollama",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def find_serving_processes(port: int) -> list[psutil.Process]:
    """Processes listening on ``port``. Best-effort: some platforms restrict
    connection enumeration, in which case we fall back to matching by name."""
    procs: list[psutil.Process] = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            if (
                conn.laddr
                and conn.laddr.port == port
                and conn.status == psutil.CONN_LISTEN
                and conn.pid
            ):
                try:
                    procs.append(psutil.Process(conn.pid))
                except psutil.Error:
                    continue
    except (psutil.AccessDenied, PermissionError):
        for p in psutil.process_iter(["name"]):
            name = (p.info.get("name") or "").lower()
            if name.startswith("ollama"):
                procs.append(p)
    return procs


@dataclass
class TransparentPlan:
    target_port: int          # the port RAMP will occupy (Ollama's usual one)
    relocate_port: int        # where Ollama gets moved to
    ollama_bin: str
    ollama_version: str
    holders: list[int]        # PIDs currently serving target_port

    def describe(self) -> str:
        return (
            f"RAMP would step in front of Ollama on port {self.target_port}:\n"
            f"  1. start a second Ollama on port {self.relocate_port} and wait "
            f"for it to be healthy\n"
            f"  2. stop the Ollama currently on {self.target_port} "
            f"(pid {', '.join(map(str, self.holders)) or 'unknown'})\n"
            f"  3. serve RAMP on {self.target_port}, forwarding to "
            f"{self.relocate_port}\n\n"
            f"Every tool pointed at localhost:{self.target_port} then flows "
            f"through RAMP with no client changes. Ollama keeps all its models "
            f"and keeps working - it just moves one port over.\n"
            f"Undone automatically when RAMP exits."
        )


def plan(
    target_port: int = 11434,
    relocate_port: int = 11435,
    ollama_bin: str = "ollama",
) -> TransparentPlan:
    """Check that transparent mode can be enabled. Raises TransparentModeError if not."""
    version = _probe(target_port)
    if version is None:
        raise TransparentModeError(
            f"nothing that looks like Ollama is answering on port {target_port}. "
            "Transparent mode only makes sense when Ollama already owns that port; "
            "otherwise just run RAMP on it directly with --port."
        )
    if not _port_free(relocate_port):
        raise TransparentModeError(
            f"port {relocate_port} is already in use, so Ollama has nowhere to "
            f"move. Pick another with --relocate-port."
        )
    binary = find_ollama_binary(ollama_bin)
    if binary is None:
        raise TransparentModeError(
            "couldn't find the ollama binary, so the relocated server can't be "
            "started. Pass --ollama-bin with its full path."
        )
    holders = [p.pid for p in find_serving_processes(target_port)]
    return TransparentPlan(
        target_port=target_port,
        relocate_port=relocate_port,
        ollama_bin=binary,
        ollama_version=version,
        holders=holders,
    )


@dataclass
class TransparentState:
    """What was changed, so it can be undone."""

    plan: TransparentPlan
    relocated_proc: subprocess.Popen | None = None
    stopped_pids: list[int] | None = None


# -- crash safety --------------------------------------------------------
#
# Restoring on clean exit is not enough. If RAMP is killed rather than asked
# to stop - a hard kill, a crash, a reboot - the in-process handler never
# runs and Ollama is left moved, with nothing on its usual port. So the
# arrangement is also written to disk, and can be repaired from a later run
# or by `ramp restore`.


def state_path() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "state"
        )
    return Path(base) / "ramp" / "transparent-state.json"


def save_state(state: TransparentState) -> None:
    p = state.plan
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "target_port": p.target_port,
                    "relocate_port": p.relocate_port,
                    "ollama_bin": p.ollama_bin,
                    "relocated_pid": (
                        state.relocated_proc.pid if state.relocated_proc else None
                    ),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        # Persisting is best-effort; a failure here must not abort the switch.
        pass


def load_state() -> dict | None:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_state() -> None:
    with contextlib.suppress(OSError):
        state_path().unlink()


def repair() -> list[str]:
    """Undo a transparent-mode arrangement left behind by a previous run.

    Safe to call at any time: if there is nothing to repair, it says so and
    changes nothing.
    """
    saved = load_state()
    if saved is None:
        return ["nothing to restore - no previous transparent-mode session found"]

    notes = []
    target = saved.get("target_port", 11434)
    relocate = saved.get("relocate_port", 11435)
    binary = saved.get("ollama_bin") or find_ollama_binary() or "ollama"

    # Stop the relocated Ollama if it's still running.
    pid = saved.get("relocated_pid")
    if pid:
        try:
            proc = psutil.Process(pid)
            if "ollama" in (proc.name() or "").lower():
                proc.terminate()
                proc.wait(timeout=15)
                notes.append(f"stopped the relocated Ollama on port {relocate}")
        except (psutil.Error, psutil.TimeoutExpired):
            pass
    for proc in find_serving_processes(relocate):
        try:
            proc.terminate()
        except psutil.Error:
            continue

    if _probe(target) is not None:
        notes.append(f"Ollama is already answering on port {target}")
        clear_state()
        return notes

    notes.extend(_restart_on(binary, target))
    clear_state()
    return notes


def _restart_on(binary: str, port: int, timeout: float = 30.0) -> list[str]:
    env = dict(os.environ)
    env["OLLAMA_HOST"] = f"127.0.0.1:{port}"
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(
            [binary, "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except OSError as e:
        return [f"could not restart Ollama ({e}) - start it yourself: ollama serve"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _probe(port) is not None:
            return [f"restarted Ollama on port {port}"]
        time.sleep(0.5)
    return [
        f"could not confirm Ollama came back on port {port} - "
        "start it yourself: ollama serve"
    ]


def _start_relocated(p: TransparentPlan, timeout: float = 60.0) -> subprocess.Popen:
    env = dict(os.environ)
    env["OLLAMA_HOST"] = f"127.0.0.1:{p.relocate_port}"
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [p.ollama_bin, "serve"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise TransparentModeError(
                f"the relocated Ollama exited immediately (code {proc.returncode})"
            )
        if _probe(p.relocate_port) is not None:
            return proc
        time.sleep(0.5)
    proc.terminate()
    raise TransparentModeError(
        f"the relocated Ollama never became healthy on port {p.relocate_port}"
    )


def _stop_holders(p: TransparentPlan, timeout: float = 20.0) -> list[int]:
    stopped = []
    for proc in find_serving_processes(p.target_port):
        try:
            proc.terminate()
            stopped.append(proc.pid)
        except psutil.Error:
            continue
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_free(p.target_port):
            return stopped
        time.sleep(0.5)
    raise TransparentModeError(
        f"port {p.target_port} is still held after asking Ollama to stop. "
        "It may be managed by a service or tray app that restarts it."
    )


def engage(p: TransparentPlan) -> TransparentState:
    """Step in front of Ollama, rolling back completely on any failure."""
    state = TransparentState(plan=p)
    # 1. Stand up the replacement first - if this fails nothing has changed.
    state.relocated_proc = _start_relocated(p)
    try:
        # 2. Only now free the target port.
        state.stopped_pids = _stop_holders(p)
    except TransparentModeError:
        state.relocated_proc.terminate()
        raise
    # 3. Record it, so a hard kill can still be repaired later.
    save_state(state)
    return state


def restore(state: TransparentState) -> list[str]:
    """Undo a transparent. Returns human-readable notes about what happened."""
    notes = []
    p = state.plan
    if state.relocated_proc is not None and state.relocated_proc.poll() is None:
        state.relocated_proc.terminate()
        try:
            state.relocated_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            state.relocated_proc.kill()
        notes.append(f"stopped the relocated Ollama on port {p.relocate_port}")

    if state.stopped_pids:
        # Put Ollama back where it was.
        env = dict(os.environ)
        env["OLLAMA_HOST"] = f"127.0.0.1:{p.target_port}"
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            subprocess.Popen(
                [p.ollama_bin, "serve"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if _probe(p.target_port) is not None:
                    notes.append(f"restarted Ollama on port {p.target_port}")
                    break
                time.sleep(0.5)
            else:
                notes.append(
                    f"could not confirm Ollama came back on port {p.target_port} - "
                    f"start it yourself with 'ollama serve'"
                )
        except OSError as e:
            notes.append(f"failed to restart Ollama: {e}")
    clear_state()
    return notes
