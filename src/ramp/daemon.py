"""Running in the background, like a daemon should.

RAMP is meant to sit there for hours. Holding a terminal hostage and
printing uvicorn logs into it is the wrong shape: you start it, you get your
prompt back, and you look at it later with ``ramp status``, ``ramp watch`` or
``ramp doctor``.

Stopping matters more than starting here. Transparent mode moves someone's
model server to another port, and that must be undone on the way out - so
``ramp stop`` asks the daemon to shut down *gracefully* over HTTP rather than
killing it, because a hard kill skips the restore. Killing is the fallback,
and it is followed by the same repair ``ramp restore`` performs.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil


def state_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "state"
        )
    return Path(base) / "ramp"


def pid_file() -> Path:
    return state_dir() / "daemon.json"


def log_file() -> Path:
    return state_dir() / "daemon.log"


def _record(pid: int, url: str) -> None:
    path = pid_file()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": pid, "url": url, "started": time.time()}),
            encoding="utf-8",
        )


def clear_record() -> None:
    with contextlib.suppress(OSError):
        pid_file().unlink()


def running() -> dict | None:
    """The daemon we started, if it is still alive.

    A recorded PID is not proof: PIDs get reused, and the process may have
    died. Verified against the process table, and cleaned up when stale.
    """
    try:
        saved = json.loads(pid_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = saved.get("pid")
    if not isinstance(pid, int):
        return None
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            raise psutil.NoSuchProcess(pid)
        # Guard against PID reuse landing on some unrelated program.
        blob = " ".join(proc.cmdline()).lower()
        if "ramp" not in blob and "python" not in blob:
            raise psutil.NoSuchProcess(pid)
    except (psutil.Error, OSError):
        clear_record()
        return None
    return saved


def responding(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as r:
            return r.status in (200, 503)
    except (urllib.error.URLError, OSError):
        return False


def spawn(argv: list[str], url: str, wait_s: float = 120.0) -> tuple[int, Path]:
    """Start the daemon detached, and wait until it is actually serving.

    Returning before it is up would mean reporting success for something that
    might still fail to bind a port or load a model.
    """
    log = log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, "ab")  # noqa: SIM115 - owned by the child, closed below
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            argv,
            stdout=handle,
            stderr=handle,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    finally:
        handle.close()

    _record(proc.pid, url)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            clear_record()
            raise RuntimeError(
                f"the daemon exited immediately (code {proc.returncode}). "
                f"See {log}"
            )
        if responding(url):
            return proc.pid, log
        time.sleep(0.4)
    raise RuntimeError(f"the daemon did not start serving within {wait_s:.0f}s. See {log}")


def request_shutdown(url: str, timeout: float = 5.0) -> bool:
    """Ask the daemon to exit cleanly so its shutdown handlers run."""
    req = urllib.request.Request(f"{url.rstrip('/')}/ramp/shutdown", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status in (200, 202)
    except (urllib.error.URLError, OSError):
        return False


def stop(grace_s: float = 30.0) -> list[str]:
    """Stop the daemon, preferring a clean exit so transparent mode unwinds."""
    saved = running()
    if saved is None:
        return ["no RAMP daemon is running"]

    pid, url = saved["pid"], saved.get("url", "http://127.0.0.1:8090")
    notes = []

    if request_shutdown(url):
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if running() is None or not psutil.pid_exists(pid):
                clear_record()
                notes.append(f"stopped cleanly (pid {pid})")
                return notes
            time.sleep(0.3)
        notes.append("asked it to stop, but it is still running - killing it")
    else:
        notes.append("could not reach it to ask nicely - killing it")

    # Fallback. A kill skips the in-process restore, so repair afterwards.
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=15)
        notes.append(f"terminated (pid {pid})")
    except (psutil.Error, psutil.TimeoutExpired):
        with contextlib.suppress(psutil.Error):
            psutil.Process(pid).kill()
        notes.append(f"killed (pid {pid})")
    clear_record()

    from . import transparent

    if transparent.load_state() is not None:
        notes.append("repairing transparent mode after the hard stop:")
        notes.extend(f"  {n}" for n in transparent.repair())
    return notes


def foreground_argv(args_argv: list[str]) -> list[str]:
    """The command that runs this same configuration in the foreground."""
    return [sys.executable, "-m", "ramp", "run", *args_argv]
