"""Transparent mode: put RAMP on the port your tools already use.

Pointing every client at a new base_url is friction, and friction is why
good infrastructure goes unused. The alternative is for RAMP to occupy the
port the incumbent server already owns and relocate that server behind it -
then every tool flows through RAMP with no configuration at all, and can't
tell the difference except that the model quietly resizes itself under
memory pressure.

Works with Ollama, llama.cpp's llama-server, and LM Studio. All that differs
between them is how the incumbent is identified and relaunched; see
``runtimes.py``.

This is an invasive thing to do to someone's machine, so the rules are:

* **Ask first.** Never done without explicit consent.
* **Never leave things broken.** The replacement is started and proven
  healthy *before* the original is stopped, and any failure rolls back.
* **Refuse rather than guess.** If RAMP cannot work out how to restart what
  it is about to stop, it declines and changes nothing.
* **Always reversible.** ``restore()`` puts the server back, automatically
  on exit and via ``ramp restore`` after an unclean shutdown.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from . import runtimes
from .runtimes import Runtime


class TransparentModeError(RuntimeError):
    """Raised when transparent mode can not be enabled safely. Nothing has changed."""


def _probe(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> str | None:
    """Label of whatever is serving on ``port``, or None if nothing is."""
    rt = runtimes.identify(port, host, timeout)
    return rt.label if rt is not None else None


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) != 0


def _which(names: list[str], extra_paths: list[str]) -> str | None:
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    for c in extra_paths:
        if c and os.path.isfile(c):
            return c
    return None


def find_ollama_binary(configured: str = "ollama") -> str | None:
    local = os.environ.get("LOCALAPPDATA") or ""
    return _which(
        [configured, "ollama"],
        [
            os.path.join(local, "Programs", "Ollama", "ollama.exe") if local else "",
            "/usr/local/bin/ollama",
            "/opt/homebrew/bin/ollama",
            "/usr/bin/ollama",
        ],
    )


def find_lms_binary(configured: str = "lms") -> str | None:
    home = os.path.expanduser("~")
    return _which(
        [configured, "lms"],
        [
            os.path.join(home, ".lmstudio", "bin", "lms.exe"),
            os.path.join(home, ".lmstudio", "bin", "lms"),
            os.path.join(home, ".cache", "lm-studio", "bin", "lms"),
        ],
    )


def find_binary_for(runtime: Runtime, configured: str | None = None) -> str | None:
    """The executable used to relaunch ``runtime``, if RAMP needs one.

    llama.cpp deliberately returns None: it is relaunched from the running
    process's own argv, because its model path and tuning flags cannot be
    reconstructed from scratch.
    """
    if runtime is runtimes.OLLAMA:
        return find_ollama_binary(configured or "ollama")
    if runtime is runtimes.LMSTUDIO:
        return find_lms_binary(configured or "lms")
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
            if name.startswith(("ollama", "llama-server", "lm studio", "lms")):
                procs.append(p)
    return procs


def _holder_cmdline(port: int) -> list[str] | None:
    for proc in find_serving_processes(port):
        try:
            cmd = proc.cmdline()
        except psutil.Error:
            continue
        if cmd:
            return cmd
    return None


@dataclass
class TransparentPlan:
    target_port: int          # the port RAMP will occupy
    relocate_port: int        # where the incumbent server gets moved to
    ollama_bin: str           # binary used to relaunch it, if one is needed
    ollama_version: str       # label of what was found there
    holders: list[int]        # PIDs currently serving target_port
    runtime: Runtime = runtimes.OLLAMA
    relaunch: runtimes.Relaunch | None = None

    def describe(self) -> str:
        label = self.runtime.label
        how = self.relaunch.note if self.relaunch else "its usual command"
        pids = ", ".join(map(str, self.holders)) or "unknown"
        return (
            f"RAMP would step in front of {label} on port {self.target_port}:\n"
            f"  1. start {label} on port {self.relocate_port} "
            f"({how}) and wait for it to be healthy\n"
            f"  2. stop the one currently on {self.target_port} (pid {pids})\n"
            f"  3. serve RAMP on {self.target_port}, forwarding to "
            f"{self.relocate_port}\n\n"
            f"Every tool pointed at localhost:{self.target_port} then flows "
            f"through RAMP with no client changes. {label} keeps all its models "
            f"and keeps working - it just moves one port over.\n"
            f"Undone automatically when RAMP exits, or with 'ramp restore'."
        )


def plan(
    target_port: int = 11434,
    relocate_port: int = 11435,
    ollama_bin: str = "ollama",
) -> TransparentPlan:
    """Check transparent mode can be enabled. Raises TransparentModeError if not."""
    runtime = runtimes.identify(target_port)
    if runtime is None:
        raise TransparentModeError(
            f"nothing is answering on port {target_port}. Transparent mode only "
            "makes sense when a model server already owns that port; otherwise "
            "just run RAMP on it directly with --port."
        )
    if not runtime.relocatable:
        raise TransparentModeError(runtimes.describe_unsupported(runtime))
    if not _port_free(relocate_port):
        raise TransparentModeError(
            f"port {relocate_port} is already in use, so {runtime.label} has "
            "nowhere to move. Pick another with --relocate-port."
        )

    binary = find_binary_for(runtime, ollama_bin)
    cmdline = _holder_cmdline(target_port) if runtime is runtimes.LLAMACPP else None
    relaunch = runtimes.plan_relaunch(
        runtime, relocate_port, cmdline=cmdline, binary=binary
    )
    if relaunch is None:
        raise TransparentModeError(
            f"found {runtime.label} on port {target_port}, but RAMP can't work "
            "out how to start it again afterwards "
            f"({runtime.relocation or 'no known method'}), so it won't stop it. "
            "Point your client at RAMP's own port instead."
        )

    holders = [p.pid for p in find_serving_processes(target_port)]
    return TransparentPlan(
        target_port=target_port,
        relocate_port=relocate_port,
        ollama_bin=binary or "",
        ollama_version=runtime.label,
        holders=holders,
        runtime=runtime,
        relaunch=relaunch,
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
# runs and the server is left moved, with nothing on its usual port. So the
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


def _restore_argv_for(p: TransparentPlan) -> list[str]:
    """The command that puts the incumbent back on its original port."""
    if p.relaunch is None:
        return []
    argv = list(p.relaunch.argv)
    if p.runtime is runtimes.LLAMACPP:
        return runtimes.rewrite_port_arg(argv, p.target_port)
    if p.runtime is runtimes.LMSTUDIO:
        return runtimes.rewrite_flag(argv, "--port", str(p.target_port))
    return argv


def _restore_env_for(p: TransparentPlan) -> dict[str, str]:
    env = dict(p.relaunch.env) if p.relaunch else {}
    if p.runtime is runtimes.OLLAMA:
        env["OLLAMA_HOST"] = f"127.0.0.1:{p.target_port}"
    return env


def save_state(state: TransparentState) -> None:
    p = state.plan
    # Record how to put things back, not merely that they moved: the original
    # argv is the only way to restart a llama-server with the right model.
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "target_port": p.target_port,
                    "relocate_port": p.relocate_port,
                    "runtime": p.runtime.key,
                    "ollama_bin": p.ollama_bin,
                    "restore_argv": _restore_argv_for(p),
                    "restore_env": _restore_env_for(p),
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


def _runtime_by_key(key: str | None) -> Runtime:
    for rt in runtimes.ALL:
        if rt.key == key:
            return rt
    return runtimes.OLLAMA


def repair() -> list[str]:
    """Undo an arrangement left behind by a previous run.

    Safe to call at any time: if there is nothing to repair, it says so and
    changes nothing.
    """
    saved = load_state()
    if saved is None:
        return ["nothing to restore - no previous transparent-mode session found"]

    notes = []
    target = saved.get("target_port", 11434)
    relocate = saved.get("relocate_port", 11435)
    runtime = _runtime_by_key(saved.get("runtime"))

    pid = saved.get("relocated_pid")
    if pid:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            proc.wait(timeout=15)
            notes.append(f"stopped the relocated {runtime.label} on port {relocate}")
        except (psutil.Error, psutil.TimeoutExpired):
            pass
    for proc in find_serving_processes(relocate):
        with contextlib.suppress(psutil.Error):
            proc.terminate()

    if _probe(target) is not None:
        notes.append(f"{runtime.label} is already answering on port {target}")
        clear_state()
        return notes

    argv = saved.get("restore_argv") or []
    env = saved.get("restore_env") or {}
    if not argv:
        binary = saved.get("ollama_bin") or find_binary_for(runtime)
        if binary and runtime is runtimes.OLLAMA:
            argv = [binary, "serve"]
    if argv:
        notes.extend(_restart_with(argv, env, target))
    else:
        notes.append(
            f"start {runtime.label} yourself - RAMP has no recorded command for it"
        )
    clear_state()
    return notes


def _start_relocated(p: TransparentPlan, timeout: float = 60.0) -> subprocess.Popen:
    if p.relaunch is None:
        raise TransparentModeError("no relaunch command was planned")
    env = dict(os.environ)
    env.update(p.relaunch.env)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            p.relaunch.argv,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except OSError as e:
        raise TransparentModeError(
            f"could not start {p.runtime.label} on port {p.relocate_port}: {e}"
        ) from e

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise TransparentModeError(
                f"the relocated {p.runtime.label} exited immediately "
                f"(code {proc.returncode})"
            )
        if _probe(p.relocate_port) is not None:
            return proc
        time.sleep(0.5)
    proc.terminate()
    raise TransparentModeError(
        f"the relocated {p.runtime.label} never became healthy on port "
        f"{p.relocate_port}"
    )


def _stop_holders(p: TransparentPlan, timeout: float = 20.0) -> list[int]:
    stopped = []
    # Prefer a graceful shutdown where the runtime provides one: LM Studio's
    # server belongs to the desktop app, so killing the process is wrong.
    stop_cmd = runtimes.stop_command(p.runtime, p.ollama_bin or None)
    if stop_cmd:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(stop_cmd, capture_output=True, timeout=30, check=False)

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
        f"port {p.target_port} is still held after asking {p.runtime.label} to "
        "stop. It may be managed by a service or tray app that restarts it."
    )


def engage(p: TransparentPlan) -> TransparentState:
    """Enable transparent mode, rolling back completely on any failure."""
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
    """Undo transparent mode. Returns human-readable notes."""
    notes = []
    p = state.plan
    if state.relocated_proc is not None and state.relocated_proc.poll() is None:
        state.relocated_proc.terminate()
        try:
            state.relocated_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            state.relocated_proc.kill()
        notes.append(
            f"stopped the relocated {p.runtime.label} on port {p.relocate_port}"
        )

    if state.stopped_pids:
        argv = _restore_argv_for(p)
        if argv:
            notes.extend(_restart_with(argv, _restore_env_for(p), p.target_port))
        else:
            notes.append(f"start {p.runtime.label} yourself to put it back")
    clear_state()
    return notes


def _restart_with(
    argv: list[str], env_overrides: dict[str, str], port: int, timeout: float = 30.0
) -> list[str]:
    env = dict(os.environ)
    env.update(env_overrides or {})
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except OSError as e:
        return [f"could not restart it ({e}) - start it yourself: {' '.join(argv)}"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _probe(port) is not None:
            return [f"restarted it on port {port}"]
        time.sleep(0.5)
    return [
        f"could not confirm it came back on port {port} - "
        f"start it yourself: {' '.join(argv)}"
    ]


def _restart_on(binary: str, port: int, timeout: float = 30.0) -> list[str]:
    """Backwards-compatible helper for the Ollama-only restart path."""
    return _restart_with(
        [binary, "serve"], {"OLLAMA_HOST": f"127.0.0.1:{port}"}, port, timeout
    )
