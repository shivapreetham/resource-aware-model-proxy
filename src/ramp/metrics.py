"""Operational telemetry.

The number that decides whether an elastic daemon is a good idea or a bad
one is **how often it actually swaps**. A swap costs ~2s warm (measured) but
tens of seconds cold, so a daemon that swaps twice a day is invisible and one
that swaps twice a minute is a bug. Nothing else in RAMP can tell you which
you have - so it is measured here rather than assumed.

Exposed two ways:
  * ``/ramp/status``  -> a ``metrics`` block, for eyeballing
  * ``/ramp/metrics`` -> Prometheus text format, for scraping into Grafana
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field


def _esc(v: str) -> str:
    """Escape a Prometheus label value."""
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.time)

    # Transitions
    swaps_total: int = 0
    swaps_by_reason: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    swaps_failed: int = 0
    unloads_total: int = 0
    swap_seconds_total: float = 0.0
    last_swap_at: float | None = None

    # Occupancy: seconds spent serving on each tier.
    tier_seconds: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    _tier_since: float | None = None
    _tier_name: str | None = None

    # Cooldown suppressions - how often the rate limiter actually bit.
    swaps_suppressed: int = 0

    # Requests
    requests_total: int = 0
    requests_waited: int = 0            # had to wait on the swap gate
    request_wait_seconds_total: float = 0.0
    requests_rejected: int = 0          # 503: no model available in time
    requests_failed: int = 0            # 502: backend unreachable

    # -- recording ------------------------------------------------------

    def swap_done(self, reason: str, duration_s: float) -> None:
        self.swaps_total += 1
        self.swaps_by_reason[reason] += 1
        self.swap_seconds_total += duration_s
        self.last_swap_at = time.time()

    def swap_failed(self) -> None:
        self.swaps_failed += 1

    def unload_done(self) -> None:
        self.unloads_total += 1
        self.last_swap_at = time.time()

    def suppressed(self) -> None:
        self.swaps_suppressed += 1

    def enter_tier(self, name: str | None) -> None:
        """Close out the previous tier's occupancy and start the next."""
        now = time.time()
        if self._tier_name is not None and self._tier_since is not None:
            self.tier_seconds[self._tier_name] += now - self._tier_since
        self._tier_name = name
        self._tier_since = now if name is not None else None

    def request_started(self, waited_s: float) -> None:
        self.requests_total += 1
        if waited_s > 0.01:
            self.requests_waited += 1
            self.request_wait_seconds_total += waited_s

    # -- reporting ------------------------------------------------------

    @property
    def uptime_s(self) -> float:
        return time.time() - self.started_at

    def _live_tier_seconds(self) -> dict[str, float]:
        """Occupancy including the currently-active tier's partial time."""
        out = dict(self.tier_seconds)
        if self._tier_name is not None and self._tier_since is not None:
            out[self._tier_name] = out.get(self._tier_name, 0.0) + (
                time.time() - self._tier_since
            )
        return out

    def snapshot(self) -> dict:
        up = self.uptime_s
        hours = up / 3600 if up > 0 else 0
        return {
            "uptime_s": round(up, 1),
            "swaps_total": self.swaps_total,
            "swaps_per_hour": round(self.swaps_total / hours, 2) if hours > 0 else 0.0,
            "swaps_by_reason": dict(self.swaps_by_reason),
            "swaps_failed": self.swaps_failed,
            "swaps_suppressed": self.swaps_suppressed,
            "unloads_total": self.unloads_total,
            "swap_seconds_total": round(self.swap_seconds_total, 2),
            "swap_time_percent": round(100 * self.swap_seconds_total / up, 2) if up > 0 else 0.0,
            "mean_swap_s": round(self.swap_seconds_total / self.swaps_total, 2)
            if self.swaps_total
            else 0.0,
            "tier_seconds": {k: round(v, 1) for k, v in self._live_tier_seconds().items()},
            "requests_total": self.requests_total,
            "requests_waited": self.requests_waited,
            "request_wait_seconds_total": round(self.request_wait_seconds_total, 2),
            "requests_rejected": self.requests_rejected,
            "requests_failed": self.requests_failed,
        }

    def prometheus(
        self, tier: str | None, tiers: list[str], selfstat: dict | None = None
    ) -> str:
        """Render Prometheus text exposition format."""
        L: list[str] = []
        occupancy = self._live_tier_seconds()

        def metric(name: str, kind: str, help_: str, samples: list[tuple[str, float]]):
            L.append(f"# HELP {name} {help_}")
            L.append(f"# TYPE {name} {kind}")
            for labels, value in samples:
                suffix = f"{{{labels}}}" if labels else ""
                v = int(value) if float(value).is_integer() else value
                L.append(f"{name}{suffix} {v}")

        metric("ramp_uptime_seconds", "gauge", "Daemon uptime.", [("", round(self.uptime_s, 1))])
        metric("ramp_swaps_total", "counter", "Tier switches by reason.",
               [(f'reason="{_esc(r)}"', n) for r, n in sorted(self.swaps_by_reason.items())]
               or [('reason="none"', 0)])
        metric("ramp_swaps_failed_total", "counter", "Tier activations that failed to start.",
               [("", self.swaps_failed)])
        metric("ramp_swaps_suppressed_total", "counter",
               "Switches withheld by the cooldown rate limiter.", [("", self.swaps_suppressed)])
        metric("ramp_unloads_total", "counter", "Times all tiers were unloaded.",
               [("", self.unloads_total)])
        metric("ramp_swap_seconds_total", "counter", "Cumulative time spent swapping.",
               [("", round(self.swap_seconds_total, 3))])
        metric("ramp_tier_seconds_total", "counter", "Time served on each tier.",
               [(f'tier="{_esc(t)}"', round(occupancy.get(t, 0.0), 1)) for t in tiers])
        metric("ramp_tier_active", "gauge", "1 for the currently loaded tier, else 0.",
               [(f'tier="{_esc(t)}"', 1 if t == tier else 0) for t in tiers])
        metric("ramp_requests_total", "counter", "Proxied requests.", [("", self.requests_total)])
        metric("ramp_requests_waited_total", "counter",
               "Requests that waited on a swap gate.", [("", self.requests_waited)])
        metric("ramp_request_wait_seconds_total", "counter",
               "Cumulative gate wait time.", [("", round(self.request_wait_seconds_total, 3))])
        metric("ramp_requests_rejected_total", "counter",
               "Requests rejected with 503 (no model available).", [("", self.requests_rejected)])
        metric("ramp_requests_failed_total", "counter",
               "Requests failed with 502 (backend unreachable).", [("", self.requests_failed)])
        if selfstat:
            mb = 1024 * 1024
            metric("ramp_self_rss_bytes", "gauge",
                   "Resident memory of the RAMP daemon itself (its overhead).",
                   [("", int(selfstat.get("rss_mb", 0) * mb))])
            metric("ramp_backend_rss_bytes", "gauge",
                   "Resident memory of backend processes RAMP spawned.",
                   [("", int(selfstat.get("backend_rss_mb", 0) * mb))])
            metric("ramp_backend_processes", "gauge",
                   "Number of backend processes RAMP spawned.",
                   [("", selfstat.get("backend_processes", 0))])
        return "\n".join(L) + "\n"
