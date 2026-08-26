"""The ``ramp`` command line.

    ramp                 start with an auto-detected ladder (no config needed)
    ramp run -c FILE     start from a config file
    ramp doctor          check this machine can run RAMP, and say how to fix it
    ramp init            write the auto-detected config out so you can edit it
    ramp status          query a running daemon
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request

import uvicorn
import yaml

from . import __version__, transparent
from .autoconfig import AutoConfigError, autodetect, describe
from .backend import make_backend
from .config import Config, ConfigError
from .controller import Controller
from .doctor import run_checks, worst
from .monitor import ResourceMonitor
from .server import create_app

DEFAULT_CONFIG_NAMES = ("ramp.yaml", "ramp.yml")
DEFAULT_PORT = 8090

# ANSI colour, but only when we're attached to a terminal that wants it.
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


_MARK = {
    "ok": (_c("  OK  ", "32"), ""),
    "warn": (_c(" WARN ", "33"), ""),
    "fail": (_c(" FAIL ", "31"), ""),
}


def _find_default_config() -> str | None:
    for name in DEFAULT_CONFIG_NAMES:
        if os.path.isfile(name):
            return name
    return None


# -- commands ------------------------------------------------------------


def cmd_doctor(args) -> int:
    print(_c("RAMP doctor", "1"), f"(v{__version__})\n")
    checks = run_checks(
        config_path=args.config or _find_default_config(),
        ollama_url=args.ollama_url,
        port=args.port or DEFAULT_PORT,
    )
    width = max(len(c.name) for c in checks)
    for c in checks:
        mark, _ = _MARK[c.status]
        print(f"[{mark}] {c.name.ljust(width)}  {c.detail}")
        if c.fix:
            print(f"         {_c('->', '2')} {c.fix}")

    overall = worst(checks)
    print()
    if overall == "fail":
        print(_c("Not ready.", "31"), "Fix the FAIL items above, then re-run 'ramp doctor'.")
        return 1
    if overall == "warn":
        print(_c("Ready, with caveats.", "33"), "Start it with 'ramp'.")
        return 0
    print(_c("All good.", "32"), "Start it with 'ramp'.")
    return 0


def _resolve_config(args) -> Config:
    """Explicit config if given, else a config file in cwd, else autodetect."""
    path = args.config or _find_default_config()
    if path:
        logging.getLogger("ramp").info("using config %s", path)
        cfg = Config.load(path)
    else:
        raw = autodetect(
            ollama_url=args.ollama_url, port=args.port or DEFAULT_PORT
        )
        print(describe(raw))
        print("\nTip: 'ramp init' writes this out as ramp.yaml so you can tune it.\n")
        cfg = Config.from_dict(raw)

    # CLI flags win over whatever the config says. --host matters for
    # containers and LAN use: bound to 127.0.0.1 inside Docker, the port is
    # unreachable from the host even with -p.
    if args.host:
        cfg.listen_host = args.host
    if args.port:
        cfg.listen_port = args.port
    return cfg


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            f"{_c('Refusing:', '31')} transparent mode needs confirmation, but this isn't "
            "an interactive terminal. Pass --yes if you're sure.",
            file=sys.stderr,
        )
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _engage_transparent(args) -> transparent.TransparentState | None:
    """Ask, then step in front of Ollama. Returns None if declined/impossible."""
    try:
        p = transparent.plan(
            target_port=args.transparent_port,
            relocate_port=args.relocate_port,
            ollama_bin=args.ollama_bin,
        )
    except transparent.TransparentModeError as e:
        print(f"{_c('Transparent mode unavailable:', '31')} {e}", file=sys.stderr)
        print("Nothing was changed.", file=sys.stderr)
        return None

    print(f"\n{_c('Transparent mode', '1')}\n")
    print(p.describe())
    print()
    if not _confirm("Put RAMP in front of Ollama?", args.yes):
        print("Left everything as it was. Run without --transparent to use "
              "RAMP's own port instead.")
        return None

    try:
        state = transparent.engage(p)
    except transparent.TransparentModeError as e:
        print(f"\n{_c('Could not enable transparent mode:', '31')} {e}", file=sys.stderr)
        print("Rolled back - Ollama is as you left it.", file=sys.stderr)
        return None

    print(f"{_c('Done.', '32')} Ollama now on {p.relocate_port}; RAMP taking "
          f"{p.target_port}.\n")
    return state


def cmd_restore(args) -> int:
    """Undo a transparent-mode arrangement left behind by an earlier run."""
    for note in transparent.repair():
        print(f"  {note}")
    return 0


def _warn_if_stale() -> None:
    """A previous run may have been killed before it could restore Ollama."""
    saved = transparent.load_state()
    if saved is None:
        return
    port = saved.get("target_port", 11434)
    print(
        f"{_c('Note:', '33')} a previous transparent-mode session did not shut "
        f"down cleanly, so Ollama may still be moved off port {port}.\n"
        f"      Run 'ramp restore' to put it back.\n"
    )


def cmd_run(args) -> int:
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    _warn_if_stale()

    state = None
    if getattr(args, "transparent", False):
        state = _engage_transparent(args)
        if state is None:
            return 1
        # Serve on the port we just claimed, talking to the relocated Ollama.
        args.port = state.plan.target_port
        args.ollama_url = f"http://127.0.0.1:{state.plan.relocate_port}"

    try:
        return _serve(args)
    finally:
        if state is not None:
            print("\nRestoring Ollama...")
            for note in transparent.restore(state):
                print(f"  {note}")


def _serve(args) -> int:
    try:
        cfg = _resolve_config(args)
    except AutoConfigError as e:
        print(f"{_c('Cannot start:', '31')} {e}\n", file=sys.stderr)
        print("Run 'ramp doctor' to see what's missing.", file=sys.stderr)
        return 1
    except (ConfigError, FileNotFoundError, ValueError) as e:
        print(f"{_c('Bad config:', '31')} {e}", file=sys.stderr)
        return 1

    controller = Controller(
        cfg, make_backend(cfg), ResourceMonitor(cfg.ema_alpha, disk_path=cfg.disk_path)
    )
    app = create_app(controller, cfg)
    url = f"http://{cfg.listen_host}:{cfg.listen_port}"
    print(f"{_c('RAMP', '1')} serving on {_c(url + '/v1', '36')} "
          f"({len(cfg.tiers)} tiers, {cfg.backend} backend)")
    print(f"     status: {url}/ramp/status     metrics: {url}/ramp/metrics")
    print("     point any OpenAI-compatible client at the /v1 URL above.\n")
    uvicorn.run(
        app, host=cfg.listen_host, port=cfg.listen_port, log_level=args.log_level
    )
    return 0


def cmd_init(args) -> int:
    try:
        raw = autodetect(ollama_url=args.ollama_url, port=args.port or DEFAULT_PORT)
    except AutoConfigError as e:
        print(f"{_c('Could not auto-detect:', '31')} {e}", file=sys.stderr)
        return 1

    out = args.output
    if os.path.exists(out) and not args.force:
        print(f"{_c('Refusing to overwrite', '31')} {out} (pass --force).", file=sys.stderr)
        return 1

    header = (
        "# RAMP config, auto-generated by 'ramp init'.\n"
        "#\n"
        "# est_ram_mb / est_vram_mb are CONSERVATIVE ESTIMATES from model file\n"
        "# sizes. To calibrate properly: pin a tier (POST /ramp/pin/<name>),\n"
        "# watch its real usage, and round up. See docs/MONITORING.md.\n\n"
    )
    with open(out, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(raw, f, sort_keys=False, default_flow_style=False)

    print(describe(raw))
    print(f"\nWrote {_c(out, '36')}. Start with: ramp run -c {out}")
    return 0


def cmd_status(args) -> int:
    url = args.url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/ramp/status", timeout=5) as r:
            st = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"{_c('No daemon at', '31')} {url} ({e})", file=sys.stderr)
        print("Start one with 'ramp'.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(st, indent=2))
        return 0

    tier = st["tier"] or _c("(nothing loaded)", "31")
    print(f"{_c('tier', '1')}      {tier}    {_c(st['last_decision'], '2')}")
    mem, gpu, disk = st.get("memory"), st.get("gpu"), st.get("disk")
    if mem:
        print(f"{_c('ram', '1')}       {mem['available_mb']:,} MB free "
              f"of {mem['total_mb']:,} MB")
    if gpu:
        print(f"{_c('vram', '1')}      {gpu['vram_free_mb']:,} MB free "
              f"of {gpu['vram_total_mb']:,} MB  ({gpu['name']})")
    if disk:
        low = _c("  LOW - upgrades gated", "33") if disk["low"] else ""
        print(f"{_c('disk', '1')}      {disk['free_mb']:,} MB free{low}")

    m = st.get("metrics", {})
    if m:
        print(f"{_c('swaps', '1')}     {m['swaps_total']} total, "
              f"{m['swaps_per_hour']}/hour, {m['mean_swap_s']}s mean")
        print(f"{_c('requests', '1')}  {m['requests_total']} "
              f"({m['requests_waited']} waited, {m['requests_rejected']} rejected)")
    s = st.get("self", {})
    if s:
        print(f"{_c('overhead', '1')}  {s['rss_mb']} MB (daemon) + "
              f"{s['backend_rss_mb']} MB (backends)")

    print(f"\n{_c('ladder', '1')}")
    for t in st["tiers"]:
        mark = _c(" * ", "32") if t["active"] else "   "
        vram = f", {t['est_vram_mb']:.0f} MB VRAM" if t.get("est_vram_mb") else ""
        print(f"{mark}{t['name']}  ({t['est_ram_mb']:.0f} MB RAM{vram})")

    events = st.get("events", [])[-5:]
    if events:
        print(f"\n{_c('recent', '1')}")
        for e in events:
            print(f"   {e['from'] or '-'} -> {e['to'] or '-'}  ({e['reason']})")
    return 0


# -- entry point ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ramp",
        description="RAMP - Resource-Aware Model Proxy: an elastic local LLM daemon.",
        epilog="Run 'ramp doctor' first if anything misbehaves.",
    )
    p.add_argument("--version", action="version", version=f"ramp {__version__}")
    sub = p.add_subparsers(dest="command")

    def common(sp):
        sp.add_argument("--ollama-url", default="http://127.0.0.1:11434",
                        help="Ollama server to inspect (default: %(default)s)")
        sp.add_argument("--port", type=int, default=None,
                        help=f"port RAMP listens on (default: {DEFAULT_PORT})")
        return sp

    run = common(sub.add_parser("run", help="start the daemon"))
    run.add_argument("--config", "-c", help="config file (default: ./ramp.yaml, else auto-detect)")
    run.add_argument("--host", default=None,
                     help="address to bind (default: 127.0.0.1; use 0.0.0.0 in containers)")
    run.add_argument("--log-level", default="info")
    run.add_argument(
        "--transparent", action="store_true",
        help="serve on Ollama's port and move Ollama behind RAMP, so existing "
             "tools route through it with no client changes (asks first)",
    )
    run.add_argument("--transparent-port", type=int, default=11434,
                     help="port to serve on (default: %(default)s, Ollama's)")
    run.add_argument("--relocate-port", type=int, default=11435,
                     help="where Ollama moves to (default: %(default)s)")
    run.add_argument("--ollama-bin", default="ollama",
                     help="path to the ollama binary, if not on PATH")
    run.add_argument("--yes", "-y", action="store_true",
                     help="skip the confirmation prompt")
    run.set_defaults(func=cmd_run)

    doc = common(sub.add_parser("doctor", help="check this machine can run RAMP"))
    doc.add_argument("--config", "-c", help="also validate this config file")
    doc.set_defaults(func=cmd_doctor)

    ini = common(sub.add_parser("init", help="write an auto-detected config file"))
    ini.add_argument("--output", "-o", default="ramp.yaml")
    ini.add_argument("--force", action="store_true", help="overwrite an existing file")
    ini.set_defaults(func=cmd_init)

    res = sub.add_parser(
        "restore",
        help="put Ollama back on its own port after a transparent-mode session",
    )
    res.set_defaults(func=cmd_restore)

    st = sub.add_parser("status", help="query a running daemon")
    st.add_argument("--url", default="http://127.0.0.1:8090")
    st.add_argument("--json", action="store_true", help="raw JSON instead of a summary")
    st.set_defaults(func=cmd_status)

    return p


def main() -> None:
    parser = build_parser()
    argv = sys.argv[1:]
    # Bare `ramp` means `ramp run`, so the zero-config path is one word.
    if not argv or (argv[0].startswith("-") and argv[0] not in ("--version", "-h", "--help")):
        argv = ["run", *argv]
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        raise SystemExit(0)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
