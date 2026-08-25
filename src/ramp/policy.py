"""The tier-selection policy: a pure, unit-testable state machine.

Given the current tier and a resource sample (RAM + optional GPU/VRAM +
optional disk) it decides whether to stay, switch tiers, or unload.

Resources are held accountable as follows:

- **RAM** works as before: fast downgrades under pressure (consecutive
  breaches of ``safety_margin_mb``, or instantly below ``critical_free_mb``),
  slow damped upgrades.
- **VRAM** (when an NVIDIA GPU is detected): a tier that declares
  ``est_vram_mb > 0`` must also fit the VRAM budget; free VRAM below
  ``vram_safety_margin_mb`` counts as a pressure breach just like RAM.
- **Disk**: free space below ``disk_min_free_mb`` blocks upgrades (loading
  a bigger model cannot free disk, so disk gates rather than downgrades).

Tiers are ordered best-first, so "downgrade" means moving to a HIGHER index.
No I/O happens here; the controller executes the decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from .config import Config
from .monitor import ResourceSample

Action = Literal["stay", "switch", "unload"]


@dataclass
class Decision:
    action: Action
    target: Optional[int] = None
    reason: str = ""


class Policy:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._breaches = 0
        self._upgrade_since: Optional[float] = None

    def reset(self) -> None:
        """Clear hysteresis state (called after every executed switch)."""
        self._breaches = 0
        self._upgrade_since = None

    # -- helpers ---------------------------------------------------------

    def _tier_fits(
        self,
        idx: int,
        ram_budget_mb: float,
        vram_budget_mb: Optional[float],
        ram_extra_mb: float = 0.0,
        vram_extra_mb: float = 0.0,
    ) -> bool:
        tier = self.cfg.tiers[idx]
        if tier.est_ram_mb + self.cfg.safety_margin_mb + ram_extra_mb > ram_budget_mb:
            return False
        # VRAM is only a constraint when a GPU is present AND the tier
        # actually claims VRAM.
        if tier.est_vram_mb > 0 and vram_budget_mb is not None:
            needed = tier.est_vram_mb + self.cfg.vram_safety_margin_mb + vram_extra_mb
            if needed > vram_budget_mb:
                return False
        return True

    def _best_fit(
        self,
        ram_budget_mb: float,
        vram_budget_mb: Optional[float],
        ram_extra_mb: float = 0.0,
        vram_extra_mb: float = 0.0,
    ) -> Optional[int]:
        """Best (largest) tier that fits every budget."""
        for i in range(len(self.cfg.tiers)):
            if self._tier_fits(i, ram_budget_mb, vram_budget_mb, ram_extra_mb, vram_extra_mb):
                return i
        return None

    def _best_fit_smaller(
        self, current: int, ram_budget_mb: float, vram_budget_mb: Optional[float]
    ) -> Optional[int]:
        """Largest tier strictly smaller than ``current`` that fits."""
        for i in range(current + 1, len(self.cfg.tiers)):
            if self._tier_fits(i, ram_budget_mb, vram_budget_mb):
                return i
        return None

    # -- decisions -------------------------------------------------------

    def initial_tier(self, sample: ResourceSample) -> Optional[int]:
        vram = sample.gpu.free_raw_mb if sample.gpu is not None else None
        return self._best_fit(sample.ram.raw_mb, vram)

    def evaluate(self, current: int, sample: ResourceSample, now: float) -> Decision:
        cfg = self.cfg
        hys = cfg.hysteresis
        cur = cfg.tiers[current]
        smallest = len(cfg.tiers) - 1

        ram_raw = sample.ram.raw_mb
        ram_ema = sample.ram.ema_mb
        # Budgets are "what would be free after unloading the current tier".
        ram_budget_raw = ram_raw + cur.est_ram_mb
        ram_budget_ema = ram_ema + cur.est_ram_mb
        if sample.gpu is not None:
            vram_budget_raw: Optional[float] = sample.gpu.free_raw_mb + cur.est_vram_mb
            vram_budget_ema: Optional[float] = sample.gpu.free_ema_mb + cur.est_vram_mb
        else:
            vram_budget_raw = vram_budget_ema = None

        # Emergency: RAM below the critical floor. Act now, skip hysteresis.
        if ram_raw < hys.critical_free_mb:
            self.reset()
            target = self._best_fit_smaller(current, ram_budget_raw, vram_budget_raw)
            if target is not None:
                return Decision("switch", target, "critical-memory")
            if current < smallest:
                # Even the smallest tier doesn't nominally fit, but shedding
                # most of the footprint beats keeping the big model alive.
                return Decision("switch", smallest, "critical-memory")
            return Decision("unload", None, "critical-memory")

        # Pressure: RAM headroom below the safety margin, or - for tiers
        # that live (partly) in VRAM - free VRAM below its margin.
        ram_breach = ram_raw < cfg.safety_margin_mb
        vram_breach = (
            sample.gpu is not None
            and cur.est_vram_mb > 0
            and sample.gpu.free_raw_mb < cfg.vram_safety_margin_mb
        )
        if ram_breach or vram_breach:
            self._upgrade_since = None
            self._breaches += 1
            if self._breaches < hys.downgrade_after_samples:
                return Decision("stay", reason="pressure-pending")
            self._breaches = 0
            reason = "memory-pressure" if ram_breach else "vram-pressure"
            target = self._best_fit_smaller(current, ram_budget_raw, vram_budget_raw)
            if target is not None:
                return Decision("switch", target, reason)
            if current < smallest:
                return Decision("switch", smallest, reason)
            return Decision("stay", reason="already-smallest")

        # Healthy: consider upgrading, but only after sustained headroom -
        # and never while disk space is critically low.
        self._breaches = 0
        if sample.disk is not None and sample.disk.free_mb < cfg.disk_min_free_mb:
            self._upgrade_since = None
            return Decision("stay", reason="disk-low")

        best = self._best_fit(
            ram_budget_ema,
            vram_budget_ema,
            ram_extra_mb=hys.upgrade_extra_mb,
            vram_extra_mb=cfg.vram_upgrade_extra_mb,
        )
        if best is not None and best < current:
            if self._upgrade_since is None:
                self._upgrade_since = now
                return Decision("stay", reason="upgrade-pending")
            if now - self._upgrade_since >= hys.upgrade_after_s:
                self._upgrade_since = None
                return Decision("switch", best, "headroom-recovered")
            return Decision("stay", reason="upgrade-pending")

        self._upgrade_since = None
        return Decision("stay", reason="steady")
