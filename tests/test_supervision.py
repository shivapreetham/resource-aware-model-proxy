"""Supervision: noticing that the upstream server died, and the watch view.

Regression context: in transparent mode RAMP does not own the relocated
server - the transparent module started it. The backend used to report
health from its own ``proc`` handle, which is None in that case, so a dead
upstream read as healthy forever and every request 502'd with no recovery.
"""
import socket

from ramp.backend import OllamaBackend
from ramp.config import Config, TierConfig
from ramp.watch import render


def make_backend(url: str) -> OllamaBackend:
    cfg = Config.from_dict({
        "backend": "ollama",
        "ollama_url": url,
        "tiers": [{"name": "t", "model": "m", "est_ram_mb": 100}],
    })
    b = OllamaBackend(cfg)
    b.tier = TierConfig(name="t", est_ram_mb=100, model="m")
    return b


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- health detection ----------------------------------------------------

def test_dead_upstream_is_reported_dead_even_though_we_never_owned_it():
    """The bug: proc is None in transparent mode, so this used to be True."""
    b = make_backend(f"http://127.0.0.1:{free_port()}")
    b.proc = None
    assert b.alive() is False


def test_live_upstream_is_reported_alive():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        b = make_backend(f"http://127.0.0.1:{port}")
        b.proc = None
        assert b.alive() is True
    finally:
        srv.close()


def test_no_tier_means_not_alive():
    b = make_backend("http://127.0.0.1:1")
    b.tier = None
    assert b.alive() is False


def test_endpoint_is_parsed_from_the_configured_url():
    """Recovery must respawn on the port we actually proxy to. Using the
    default would try to bind the port RAMP itself is holding."""
    b = make_backend("http://127.0.0.1:11435")
    assert b._endpoint() == ("127.0.0.1", 11435)


def test_endpoint_falls_back_to_the_default_port():
    b = make_backend("http://127.0.0.1")
    assert b._endpoint() == ("127.0.0.1", 11434)


# -- watch rendering -----------------------------------------------------

STATUS = {
    "tier": "qwen2.5-1.5b",
    "last_decision": "steady",
    "pinned": None,
    "memory": {"available_mb": 4000, "total_mb": 16000},
    "gpu": {"vram_free_mb": 6000, "vram_total_mb": 8000, "name": "RTX 5060"},
    "disk": {"free_mb": 500000, "low": False},
    "tiers": [
        {"name": "qwen2.5-1.5b", "est_ram_mb": 2200, "est_vram_mb": 1800, "active": True},
        {"name": "qwen2.5-0.5b", "est_ram_mb": 900, "active": False},
    ],
    "metrics": {"swaps_total": 3, "swaps_per_hour": 1.2, "mean_swap_s": 1.9,
                "requests_total": 42},
    "self": {"rss_mb": 64.5, "backend_rss_mb": 0.0},
    "events": [
        {"from": "qwen2.5-1.5b", "to": "qwen2.5-0.5b", "reason": "memory-pressure"},
        {"from": "qwen2.5-0.5b", "to": "qwen2.5-1.5b", "reason": "headroom-recovered"},
    ],
}


def test_watch_shows_the_active_tier_and_the_ladder():
    out = render(STATUS, color=False)
    assert "qwen2.5-1.5b" in out
    assert "qwen2.5-0.5b" in out
    assert ">" in out, "the active tier must be marked"


def test_watch_shows_resources_and_switches():
    out = render(STATUS, color=False)
    assert "RAM" in out and "VRAM" in out and "Disk" in out
    assert "memory-pressure" in out
    assert "headroom-recovered" in out


def test_watch_reports_overhead_and_swap_rate():
    out = render(STATUS, color=False)
    assert "64.5" in out
    assert "3 total" in out


def test_watch_handles_nothing_loaded():
    """An unloaded daemon still has to render rather than crash."""
    out = render({"tier": None, "last_decision": "critical-memory", "tiers": []},
                 color=False)
    assert "nothing loaded" in out


def test_watch_handles_a_bare_status_payload():
    """Older daemons, or one that has only just started, may omit sections."""
    out = render({"tier": "x", "tiers": [{"name": "x", "est_ram_mb": 1, "active": True}]},
                 color=False)
    assert "x" in out


def test_watch_marks_a_pinned_tier():
    out = render({**STATUS, "pinned": "qwen2.5-0.5b"}, color=False)
    assert "pinned" in out


def test_render_emits_no_escape_codes_when_colour_is_off():
    """So it stays readable when piped to a file or a CI log."""
    assert "\033[" not in render(STATUS, color=False)
