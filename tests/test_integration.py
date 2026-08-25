"""End-to-end tests: real controller + real mock-LLM subprocesses + ASGI proxy.

Memory readings are faked (scripted), so we can drive the ladder up and
down deterministically while real child processes get spawned and swapped.
"""
import asyncio
import time

import httpx
import pytest

from ramp.backend import MockBackend
from ramp.config import Config
from ramp.controller import Controller
from ramp.monitor import DiskSample, MemorySample, ResourceSample
from ramp.server import create_app


class FakeMonitor:
    def __init__(self, avail_mb: float, disk_free_mb: float = 100000) -> None:
        self.avail_mb = avail_mb
        self.disk_free_mb = disk_free_mb

    def sample(self) -> ResourceSample:
        return ResourceSample(
            ram=MemorySample(
                raw_mb=self.avail_mb,
                ema_mb=self.avail_mb,
                total_mb=16000,
                percent=50.0,
            ),
            gpu=None,
            disk=DiskSample(path=".", free_mb=self.disk_free_mb, total_mb=500000),
            ts=time.time(),
        )


CFG = {
    "backend": "mock",
    "poll_interval_s": 0.05,
    "safety_margin_mb": 1000,
    "drain_timeout_s": 2,
    "queue_timeout_s": 30,
    "startup_timeout_s": 60,
    "disk_min_free_mb": 5000,
    "min_swap_interval_s": 0,   # these tests run on sub-second timings
    "hysteresis": {
        "downgrade_after_samples": 2,
        "upgrade_after_s": 0.3,
        "upgrade_extra_mb": 0,
        "critical_free_mb": 300,
    },
    "tiers": [
        {"name": "big", "est_ram_mb": 4000},
        {"name": "small", "est_ram_mb": 500},
    ],
}


@pytest.fixture
async def stack():
    cfg = Config.from_dict(CFG)
    monitor = FakeMonitor(8000)
    controller = Controller(cfg, MockBackend(cfg), monitor)
    app = create_app(controller, cfg)
    # ASGITransport doesn't run the lifespan; start/stop manually.
    await controller.start()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ramp.test",
        timeout=30.0,
    )
    try:
        yield controller, monitor, client
    finally:
        await client.aclose()
        await controller.close()
        await app.state.client.aclose()


async def wait_for_tier(client: httpx.AsyncClient, name, timeout=20.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = await client.get("/ramp/status")
        data = r.json()
        last = data
        if data["tier"] == name and (name is None or data["ready"]):
            return data
        await asyncio.sleep(0.05)
    raise AssertionError(f"tier never became {name!r}; last status: {last}")


async def chat(client: httpx.AsyncClient, text="hello"):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": text}]},
    )
    assert r.status_code == 200, r.text
    return r


async def test_downgrade_then_upgrade(stack):
    _controller, monitor, client = stack

    # Plenty of memory at startup: best tier loads.
    await wait_for_tier(client, "big")
    r = await chat(client)
    assert "[big]" in r.json()["choices"][0]["message"]["content"]
    assert r.headers["x-ramp-tier"] == "big"

    # Sustained pressure: below margin, above critical -> downgrade.
    monitor.avail_mb = 600
    await wait_for_tier(client, "small")
    r = await chat(client)
    assert "[small]" in r.json()["choices"][0]["message"]["content"]
    assert r.headers["x-ramp-tier"] == "small"

    # Memory recovers: upgrade after sustained headroom.
    monitor.avail_mb = 8000
    await wait_for_tier(client, "big")
    r = await chat(client)
    assert "[big]" in r.json()["choices"][0]["message"]["content"]

    # The event log recorded the journey.
    events = (await client.get("/ramp/status")).json()["events"]
    switches = [(e["from"], e["to"], e["reason"]) for e in events]
    assert (None, "big", "startup") in switches
    assert ("big", "small", "memory-pressure") in switches
    assert ("small", "big", "headroom-recovered") in switches


async def test_streaming_passthrough(stack):
    _controller, _monitor, client = stack
    await wait_for_tier(client, "big")

    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "auto",
            "stream": True,
            "messages": [{"role": "user", "content": "stream me"}],
        },
    ) as r:
        assert r.status_code == 200
        assert r.headers["x-ramp-tier"] == "big"
        body = ""
        async for line in r.aiter_lines():
            body += line + "\n"
    assert "data:" in body
    assert "[DONE]" in body
    assert "[big]" in body


async def test_critical_pressure_unloads_and_recovers(stack):
    controller, monitor, client = stack
    await wait_for_tier(client, "big")

    # Below critical on the smallest tier -> unload entirely.
    monitor.avail_mb = 100
    await wait_for_tier(client, None)
    assert not controller.ready.is_set()

    # Memory returns -> a tier loads again and requests succeed.
    monitor.avail_mb = 8000
    await wait_for_tier(client, "big")
    r = await chat(client)
    assert r.status_code == 200


async def test_pin_overrides_policy(stack):
    _controller, _monitor, client = stack
    await wait_for_tier(client, "big")

    r = await client.post("/ramp/pin/small")
    assert r.status_code == 200
    await wait_for_tier(client, "small")

    # Policy would upgrade (plenty of memory), but the pin holds.
    await asyncio.sleep(0.6)
    assert (await client.get("/ramp/status")).json()["tier"] == "small"

    await client.delete("/ramp/pin")
    await wait_for_tier(client, "big")


async def test_metrics_track_swaps_and_requests(stack):
    _controller, monitor, client = stack
    await wait_for_tier(client, "big")
    await chat(client)

    monitor.avail_mb = 600
    await wait_for_tier(client, "small")
    await chat(client)

    m = (await client.get("/ramp/status")).json()["metrics"]
    assert m["swaps_total"] >= 2                      # startup + downgrade
    assert m["swaps_by_reason"]["memory-pressure"] >= 1
    assert m["requests_total"] >= 2
    assert m["mean_swap_s"] > 0
    assert m["tier_seconds"]["big"] > 0
    assert m["uptime_s"] > 0


async def test_prometheus_endpoint(stack):
    _controller, _monitor, client = stack
    await wait_for_tier(client, "big")
    await chat(client)

    r = await client.get("/ramp/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    # Well-formed exposition: HELP/TYPE lines and labelled series.
    assert "# HELP ramp_swaps_total" in body
    assert "# TYPE ramp_swaps_total counter" in body
    assert 'ramp_swaps_total{reason="startup"}' in body
    assert 'ramp_tier_active{tier="big"} 1' in body
    assert 'ramp_tier_active{tier="small"} 0' in body
    assert "ramp_requests_total" in body
    # Every non-comment line must be "name value".
    for line in body.splitlines():
        if line and not line.startswith("#"):
            assert len(line.rsplit(" ", 1)) == 2, line
            float(line.rsplit(" ", 1)[1])


async def test_self_footprint_is_reported(stack):
    """RAMP reports its own overhead, so users can verify rather than trust."""
    _controller, _monitor, client = stack
    await wait_for_tier(client, "big")

    s = (await client.get("/ramp/status")).json()["self"]
    assert s["rss_mb"] > 0
    # The mock backend is a real child process, so it must be visible.
    assert s["backend_processes"] >= 1
    assert s["backend_rss_mb"] > 0

    body = (await client.get("/ramp/metrics")).text
    assert "ramp_self_rss_bytes" in body
    assert "ramp_backend_rss_bytes" in body
    assert "ramp_backend_processes" in body


async def test_models_list_is_stable_across_swaps(stack):
    """Clients cache /v1/models to populate dropdowns. The list must not
    change under them when RAMP swaps tiers."""
    _controller, monitor, client = stack
    await wait_for_tier(client, "big")

    before = (await client.get("/v1/models")).json()
    ids = [m["id"] for m in before["data"]]
    assert before["object"] == "list"
    assert "auto" in ids, "the virtual model name must be advertised"
    assert {"big", "small"} <= set(ids), "configured tiers should be listed"

    monitor.avail_mb = 600
    await wait_for_tier(client, "small")
    after = (await client.get("/v1/models")).json()
    assert [m["id"] for m in after["data"]] == ids


async def test_health_endpoint(stack):
    _controller, _monitor, client = stack
    await wait_for_tier(client, "big")
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["tier"] == "big"


async def test_cors_headers_present_for_browser_clients(stack):
    """Browser front-ends can't call RAMP at all without these."""
    _controller, _monitor, client = stack
    await wait_for_tier(client, "big")

    r = await client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "*"

    r = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Origin": "http://localhost:3000"},
    )
    assert r.headers.get("access-control-allow-origin") == "*"
    # The tier header must survive CORS, or browser clients can't read it.
    assert "x-ramp-tier" in r.headers.get("access-control-expose-headers", "").lower()


async def test_unknown_model_name_is_served_anyway(stack):
    """Clients often send a hard-coded model name. RAMP serves whatever is
    loaded rather than 404-ing, which is what makes it a drop-in."""
    _controller, _monitor, client = stack
    await wait_for_tier(client, "big")
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert r.headers["x-ramp-tier"] == "big"


async def test_query_params_are_forwarded(stack):
    _controller, _monitor, client = stack
    await wait_for_tier(client, "big")
    r = await client.get("/v1/models?limit=1")
    assert r.status_code == 200


async def test_pin_unknown_tier_404(stack):
    _, _, client = stack
    r = await client.post("/ramp/pin/nope")
    assert r.status_code == 404


async def test_slow_swap_is_not_mistaken_for_a_crash():
    """A swap in flight must not trip crash recovery.

    Regression: mid-swap the backend is stopped while ``current`` still
    names the outgoing tier, so the poll loop used to read that transient
    state as a crash and "recover" to the old tier - clobbering a pin.
    """
    class SlowMockBackend(MockBackend):
        async def start(self, tier):
            await asyncio.sleep(0.4)  # long enough to span several polls
            await super().start(tier)

    cfg = Config.from_dict(CFG)
    monitor = FakeMonitor(8000)
    controller = Controller(cfg, SlowMockBackend(cfg), monitor)
    app = create_app(controller, cfg)
    await controller.start()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ramp.test",
        timeout=30.0,
    )
    try:
        await wait_for_tier(client, "big")
        r = await client.post("/ramp/pin/small")
        assert r.status_code == 200
        await wait_for_tier(client, "small")
        # Long enough for a spurious recovery (queued behind the swap lock
        # during the 0.4s start) to complete and record its event.
        await asyncio.sleep(1.5)

        status = (await client.get("/ramp/status")).json()
        assert status["tier"] == "small", "pin was clobbered"
        reasons = [e["reason"] for e in status["events"]]
        assert "backend-crash" not in reasons, f"spurious crash recovery: {reasons}"
    finally:
        await client.aclose()
        await controller.close()
        await app.state.client.aclose()


async def test_low_disk_blocks_upgrade(stack):
    _controller, monitor, client = stack
    await wait_for_tier(client, "big")

    # Drop to the small tier via memory pressure.
    monitor.avail_mb = 600
    await wait_for_tier(client, "small")

    # RAM recovers, but the disk is nearly full: no upgrade, and the
    # status explains why.
    monitor.avail_mb = 8000
    monitor.disk_free_mb = 1000
    await asyncio.sleep(1.0)
    status = (await client.get("/ramp/status")).json()
    assert status["tier"] == "small"
    assert status["last_decision"] == "disk-low"
    assert status["disk"]["low"] is True

    # Disk freed -> the upgrade proceeds.
    monitor.disk_free_mb = 100000
    await wait_for_tier(client, "big")
