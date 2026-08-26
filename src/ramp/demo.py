"""Self-contained demo: watch the ladder move, with nothing to download.

A demo that only works on the machine it was written on is worthless, so
everything here scales to whatever RAM the host actually has. The tiers are
derived from *currently free* memory rather than hardcoded, which means the
same commands behave the same way on an 8 GB laptop and a 64 GB workstation.
"""
from __future__ import annotations

import time

import psutil

_MB = 1024 * 1024


def demo_config(free_mb: float, port: int = 8090) -> dict:
    """A three-tier mock ladder proportioned to this machine.

    Fractions of currently-free memory, chosen so the whole cycle is
    reachable:

    * top tier + margin < free            -> starts on the big tier
    * margin is just under half of free   -> modest pressure trips a downgrade
    * a smaller tier fits the post-swap budget once tripped
    """
    f = max(free_mb, 1200)  # keep the arithmetic sane on a very full machine
    margin = round(f * 0.45)
    return {
        "backend": "mock",
        "listen": {"host": "127.0.0.1", "port": port},
        "poll_interval_s": 2,
        "safety_margin_mb": margin,
        "min_swap_interval_s": 0,          # a demo shouldn't wait a minute
        "hysteresis": {
            "downgrade_after_samples": 2,
            "upgrade_after_s": 15,
            "upgrade_extra_mb": round(f * 0.03),
            "critical_free_mb": round(f * 0.10),
        },
        "tiers": [
            {"name": "big-model", "est_ram_mb": round(f * 0.25), "mock_ballast_mb": 60},
            {"name": "medium-model", "est_ram_mb": round(f * 0.12), "mock_ballast_mb": 30},
            {"name": "small-model", "est_ram_mb": round(f * 0.05), "mock_ballast_mb": 10},
        ],
    }


def stress(leave_free_percent: float = 35.0, hold_s: float = 45.0, quiet: bool = False):
    """Occupy memory so the ladder has something to react to.

    Two details matter, both learned the hard way:

    * The target is a *fraction of currently free* memory, not a fixed size,
      so it creates real pressure on any machine.
    * The pages are re-touched while holding. Without that, Windows moves
      them to the standby list, "available" climbs back, and no pressure is
      ever observed.
    """
    start_free = psutil.virtual_memory().available / _MB
    target = max(start_free * (leave_free_percent / 100.0), 400)
    chunk_mb = max(64, min(256, round(start_free / 20)))
    chunks: list[bytearray] = []

    def say(msg):
        if not quiet:
            print(msg, flush=True)

    say(f"Free memory now: {start_free:,.0f} MB. Filling until ~{target:,.0f} MB is left.")
    while psutil.virtual_memory().available / _MB > target:
        try:
            b = bytearray(chunk_mb * _MB)
        except MemoryError:
            break
        for i in range(0, len(b), 4096):   # touch every page so it's committed
            b[i] = 1
        chunks.append(b)
        if len(chunks) * chunk_mb > start_free * 0.90:
            break                          # never take everything

    held = len(chunks) * chunk_mb
    say(f"Holding {held:,} MB for {hold_s:.0f}s. Ask RAMP again now - "
        f"it should answer from a smaller model.")

    end = time.monotonic() + hold_s
    while time.monotonic() < end:
        for b in chunks:                   # keep the pages resident
            for i in range(0, len(b), _MB):
                b[i] = 1
        say(f"  free: {psutil.virtual_memory().available / _MB:,.0f} MB")
        time.sleep(2)

    chunks.clear()
    say("Released. Within ~20s RAMP should climb back to the bigger model.")
