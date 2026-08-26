"""``ramp watch`` - a live view of what the daemon is doing.

``ramp status`` answers "what is it doing right now?" once. Watching the
ladder actually move - pressure building, a tier dropping, headroom
returning - is the only way to see that the thing works, and it is what you
want on a second monitor while you stress the machine.

Deliberately plain: no curses, no dependencies, just a repainted frame. That
works over SSH, in a Windows terminal, and in a recording.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request

BAR_WIDTH = 28


def _bar(used_fraction: float, width: int = BAR_WIDTH) -> str:
    used_fraction = max(0.0, min(1.0, used_fraction))
    filled = round(used_fraction * width)
    return "#" * filled + "." * (width - filled)


def _fetch(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(f"{url}/ramp/status", timeout=timeout) as r:
        return json.loads(r.read())


def _clear() -> str:
    # Home the cursor and clear downwards, rather than wiping the whole
    # screen: repainting in place avoids the flicker of a full clear.
    return "\033[H\033[J"


def render(st: dict, color: bool = True, width: int | None = None) -> str:
    def c(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    width = width or shutil.get_terminal_size((80, 24)).columns
    lines = []
    tier = st.get("tier") or "nothing loaded"
    decision = st.get("last_decision", "")
    pinned = st.get("pinned")

    lines.append(c("RAMP", "1") + f"  {c(tier, '32' if st.get('tier') else '31')}"
                 + (f"  {c('[pinned]', '33')}" if pinned else "")
                 + f"   {c(decision, '2')}")
    lines.append("")

    mem = st.get("memory") or {}
    if mem:
        total, avail = mem.get("total_mb", 1), mem.get("available_mb", 0)
        lines.append(
            f"  RAM   [{_bar(1 - avail / max(total, 1))}] "
            f"{avail:>7,} MB free of {total:,}"
        )
    gpu = st.get("gpu") or {}
    if gpu:
        gt, gf = gpu.get("vram_total_mb", 1), gpu.get("vram_free_mb", 0)
        lines.append(
            f"  VRAM  [{_bar(1 - gf / max(gt, 1))}] "
            f"{gf:>7,} MB free of {gt:,}"
        )
    disk = st.get("disk") or {}
    if disk:
        flag = c("  LOW", "33") if disk.get("low") else ""
        lines.append(f"  Disk  {' ' * (BAR_WIDTH + 2)} {disk.get('free_mb', 0):>7,} MB free{flag}")
    lines.append("")

    lines.append(c("  ladder", "2"))
    for t in st.get("tiers", []):
        active = t.get("active")
        marker = c(" >", "32") if active else "  "
        name = c(t["name"], "1") if active else t["name"]
        vram = f", {t['est_vram_mb']:.0f} MB VRAM" if t.get("est_vram_mb") else ""
        lines.append(f"  {marker} {name}  ({t['est_ram_mb']:.0f} MB RAM{vram})")
    lines.append("")

    m = st.get("metrics") or {}
    if m:
        lines.append(
            c("  swaps", "2")
            + f"  {m.get('swaps_total', 0)} total"
            f"  ({m.get('swaps_per_hour', 0)}/hr, mean {m.get('mean_swap_s', 0)}s)"
            f"   {c('requests', '2')} {m.get('requests_total', 0)}"
        )
    sf = st.get("self") or {}
    if sf:
        lines.append(
            c("  overhead", "2")
            + f"  {sf.get('rss_mb', 0)} MB daemon + "
            f"{sf.get('backend_rss_mb', 0)} MB backends"
        )
    lines.append("")

    events = (st.get("events") or [])[-6:]
    if events:
        lines.append(c("  recent switches", "2"))
        for e in events:
            frm, to = e.get("from") or "-", e.get("to") or "-"
            reason = e.get("reason", "")
            hue = "33" if "pressure" in reason else "32" if "recovered" in reason else "2"
            lines.append(f"    {frm} -> {to}  {c('(' + reason + ')', hue)}")

    lines.append("")
    lines.append(c("  Ctrl+C to stop watching (the daemon keeps running)", "2"))
    return "\n".join(line[:width] if len(line) < 400 else line for line in lines)


def watch(url: str, interval: float = 1.0, color: bool = True) -> int:
    url = url.rstrip("/")
    first = True
    while True:
        try:
            st = _fetch(url)
        except (urllib.error.URLError, OSError, ValueError) as e:
            if first:
                print(f"No daemon at {url} ({e})")
                print("Start one with 'ramp' (or 'ramp demo' to try it without models).")
                return 1
            # A daemon restarting mid-watch is normal; keep waiting for it.
            frame = f"{_clear()}  waiting for {url} ..."
            print(frame, flush=True)
            time.sleep(interval)
            continue
        first = False
        print(_clear() + render(st, color=color), flush=True)
        time.sleep(interval)


def supports_color() -> bool:
    return os.environ.get("NO_COLOR") is None
