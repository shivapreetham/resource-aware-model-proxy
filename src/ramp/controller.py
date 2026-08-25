"""The controller ties monitor + policy + backend together.

It runs the poll loop, executes tier switches (drain -> stop -> start ->
reopen the gate), restarts a crashed backend, and tracks state for the
status endpoint. Requests are gated on ``ready``: while a swap is in
progress the proxy holds incoming requests until the new tier is healthy.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque

from .backend import BackendError, ProcessBackend
from .config import Config
from .metrics import Metrics
from .monitor import ResourceMonitor, ResourceSample, process_footprint
from .policy import Policy

log = logging.getLogger("ramp.controller")


class Controller:
    def __init__(self, cfg: Config, backend: ProcessBackend, monitor: ResourceMonitor) -> None:
        self.cfg = cfg
        self.backend = backend
        self.monitor = monitor
        self.policy = Policy(cfg)
        self.metrics = Metrics()

        self.ready = asyncio.Event()
        # Serializes tier transitions (the poll loop and the pin endpoint
        # can both request a switch).
        self._swap_lock = asyncio.Lock()
        self.inflight = 0
        self.current: int | None = None
        self.pinned: str | None = None
        self.last_sample: ResourceSample | None = None
        # Why the policy last chose to stay/switch - surfaced in /ramp/status
        # so it's visible *why* RAMP is holding a tier (e.g. "disk-low").
        self.last_decision: str = "init"
        self.events: deque[dict] = deque(maxlen=100)
        self._task: asyncio.Task | None = None

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        sample = self.monitor.sample()
        self.last_sample = sample
        idx = self.policy.initial_tier(sample)
        if idx is None:
            log.warning(
                "no tier fits at startup (%.0f MB RAM available); waiting for resources",
                sample.ram.raw_mb,
            )
        else:
            await self._activate(idx, reason="startup")
        self._task = asyncio.create_task(self._loop(), name="ramp-controller")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.ready.clear()
        await self.backend.close()

    # -- control loop ----------------------------------------------------

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.poll_interval_s)
            try:
                await self._tick(time.monotonic())
            except Exception:
                log.exception("controller tick failed")

    async def _tick(self, now: float) -> None:
        sample = self.monitor.sample()
        self.last_sample = sample

        # A transition is already in flight (pin, or a previous decision).
        # Mid-swap the backend is intentionally stopped and self.current
        # still names the outgoing tier, so evaluating here would both
        # misread that as a crash and race the swap that's underway.
        if self._swap_lock.locked():
            return

        # Crash recovery: the backend died underneath us.
        if self.current is not None and not self.backend.alive():
            log.warning("backend died; restarting current tier")
            self.ready.clear()
            await self._activate(self.current, reason="backend-crash")
            return

        if self.pinned is not None:
            return

        if self.current is None:
            idx = self.policy.initial_tier(sample)
            if idx is not None:
                await self._activate(idx, reason="resources-available")
            return

        decision = self.policy.evaluate(self.current, sample, now)
        if decision.reason == "cooldown" and self.last_decision != "cooldown":
            self.metrics.suppressed()
        self.last_decision = decision.reason
        if decision.action == "switch" and decision.target is not None:
            await self._activate(decision.target, reason=decision.reason)
        elif decision.action == "unload":
            await self._unload(reason=decision.reason)

    # -- transitions -----------------------------------------------------

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.drain_timeout_s
        while self.inflight > 0 and loop.time() < deadline:
            await asyncio.sleep(0.05)
        if self.inflight > 0:
            log.warning(
                "drain timeout with %d request(s) in flight; swapping anyway",
                self.inflight,
            )

    async def _activate(self, idx: int, reason: str = "") -> None:
        async with self._swap_lock:
            old = self.tier_name
            t0 = time.monotonic()
            self.ready.clear()
            await self._drain()
            await self.backend.stop()
            self.metrics.enter_tier(None)
            # Try the target tier; on startup failure fall through to smaller ones.
            for candidate in range(idx, len(self.cfg.tiers)):
                tier = self.cfg.tiers[candidate]
                try:
                    await self.backend.start(tier)
                except BackendError as e:
                    log.error("failed to start tier %r: %s", tier.name, e)
                    self.metrics.swap_failed()
                    continue
                self.current = candidate
                self.policy.reset(time.monotonic())
                self.ready.set()
                self.metrics.swap_done(reason or "unspecified", time.monotonic() - t0)
                self.metrics.enter_tier(tier.name)
                self._record("switch", old, tier.name, reason)
                return
            self.current = None
            self.metrics.unload_done()
            self._record("unload", old, None, f"{reason} (all tiers failed to start)")
            log.error("all tiers from index %d failed to start; nothing loaded", idx)

    async def _unload(self, reason: str = "") -> None:
        async with self._swap_lock:
            old = self.tier_name
            self.ready.clear()
            await self._drain()
            await self.backend.stop()
            self.current = None
            self.policy.reset(time.monotonic())
            self.metrics.unload_done()
            self.metrics.enter_tier(None)
            self._record("unload", old, None, reason)
            log.warning("model unloaded (%s)", reason)

    # -- manual override -------------------------------------------------

    async def pin(self, name: str) -> None:
        for i, tier in enumerate(self.cfg.tiers):
            if tier.name == name:
                self.pinned = name
                await self._activate(i, reason="pinned")
                return
        raise KeyError(name)

    def unpin(self) -> None:
        self.pinned = None

    # -- introspection ---------------------------------------------------

    @property
    def tier_name(self) -> str | None:
        if self.current is None:
            return None
        return self.cfg.tiers[self.current].name

    def _record(self, event: str, frm: str | None, to: str | None, reason: str) -> None:
        self.events.append(
            {"ts": time.time(), "event": event, "from": frm, "to": to, "reason": reason}
        )
        log.info("%s: %s -> %s (%s)", event, frm, to, reason)

    def status(self) -> dict:
        s = self.last_sample
        return {
            "tier": self.tier_name,
            "tier_index": self.current,
            "pinned": self.pinned,
            "last_decision": self.last_decision,
            "ready": self.ready.is_set(),
            "inflight": self.inflight,
            "memory": None
            if s is None
            else {
                "available_mb": round(s.ram.raw_mb),
                "available_ema_mb": round(s.ram.ema_mb),
                "total_mb": round(s.ram.total_mb),
                "used_percent": s.ram.percent,
            },
            "gpu": None
            if s is None or s.gpu is None
            else {
                "name": s.gpu.name,
                "vram_total_mb": round(s.gpu.total_mb),
                "vram_free_mb": round(s.gpu.free_raw_mb),
                "vram_free_ema_mb": round(s.gpu.free_ema_mb),
                "vram_used_mb": round(s.gpu.used_mb),
                "util_percent": s.gpu.util_percent,
            },
            "disk": None
            if s is None or s.disk is None
            else {
                "path": s.disk.path,
                "free_mb": round(s.disk.free_mb),
                "total_mb": round(s.disk.total_mb),
                "low": s.disk.free_mb < self.cfg.disk_min_free_mb,
            },
            "tiers": [
                {
                    "name": t.name,
                    "est_ram_mb": t.est_ram_mb,
                    "est_vram_mb": t.est_vram_mb,
                    "ctx": t.ctx,
                    "active": i == self.current,
                }
                for i, t in enumerate(self.cfg.tiers)
            ],
            "self": self.self_footprint(),
            "metrics": self.metrics.snapshot(),
            "events": list(self.events),
        }

    def self_footprint(self) -> dict:
        """What RAMP itself costs. Surfaced so users can verify the overhead
        rather than take the README's word for it."""
        try:
            f = process_footprint()
        except Exception:  # psutil can fail on locked-down systems
            return {}
        return {
            "rss_mb": round(f.rss_mb, 1),
            "backend_rss_mb": round(f.children_rss_mb, 1),
            "backend_processes": f.child_count,
        }

    def prometheus(self) -> str:
        return self.metrics.prometheus(
            self.tier_name, [t.name for t in self.cfg.tiers], self.self_footprint()
        )
