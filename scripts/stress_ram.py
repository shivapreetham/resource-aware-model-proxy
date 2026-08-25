"""Allocate memory to simulate pressure and watch RAMP downgrade.

    python scripts/stress_ram.py --mb 4000 --hold-s 60

Allocates --mb megabytes (in 256 MB chunks, pages touched so they're really
committed), holds for --hold-s seconds, then releases and exits. Watch
``GET /ramp/status`` (or the daemon log) while it runs.
"""
from __future__ import annotations

import argparse
import time

CHUNK_MB = 256


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mb", type=int, default=2048)
    p.add_argument("--hold-s", type=int, default=60)
    args = p.parse_args()

    chunks: list[bytearray] = []
    allocated = 0
    print(f"allocating {args.mb} MB ...")
    while allocated < args.mb:
        size = min(CHUNK_MB, args.mb - allocated)
        buf = bytearray(size * 1024 * 1024)
        for i in range(0, len(buf), 4096):
            buf[i] = 1
        chunks.append(buf)
        allocated += size
        print(f"  {allocated} / {args.mb} MB", end="\r")
    print(f"\nholding {allocated} MB for {args.hold_s}s ... (Ctrl+C to release early)")
    try:
        time.sleep(args.hold_s)
    except KeyboardInterrupt:
        pass
    chunks.clear()
    print("released.")


if __name__ == "__main__":
    main()
