"""Unit tests for the tier-selection state machine (pure logic, no I/O)."""
import time

import pytest

from ramp.config import Config
from ramp.monitor import DiskSample, GpuSample, MemorySample, ResourceSample
from ramp.policy import Policy


def make_cfg(**overrides):
    raw = {
        "backend": "mock",
        "safety_margin_mb": 1000,
        "vram_safety_margin_mb": 500,
        "vram_upgrade_extra_mb": 0,
        "disk_min_free_mb": 5000,
        "hysteresis": {
            "downgrade_after_samples": 2,
            "upgrade_after_s": 60,
            "upgrade_extra_mb": 500,
            "critical_free_mb": 500,
        },
        "tiers": [
            {"name": "big", "est_ram_mb": 6000, "est_vram_mb": 4000},
            {"name": "mid", "est_ram_mb": 3000, "est_vram_mb": 2000},
            {"name": "small", "est_ram_mb": 1000, "est_vram_mb": 0},
        ],
    }
    raw.update(overrides)
    return Config.from_dict(raw)


def S(ram, ram_ema=None, vram=None, vram_ema=None, disk_free=50000):
    """Build a ResourceSample. vram=None means no GPU detected."""
    gpu = None
    if vram is not None:
        gpu = GpuSample(
            name="test-gpu",
            total_mb=8000,
            free_raw_mb=vram,
            free_ema_mb=vram if vram_ema is None else vram_ema,
            used_mb=8000 - vram,
            util_percent=0.0,
        )
    disk = None
    if disk_free is not None:
        disk = DiskSample(path=".", free_mb=disk_free, total_mb=500000)
    return ResourceSample(
        ram=MemorySample(
            raw_mb=ram,
            ema_mb=ram if ram_ema is None else ram_ema,
            total_mb=16000,
            percent=50.0,
        ),
        gpu=gpu,
        disk=disk,
        ts=time.time(),
    )


@pytest.fixture
def policy():
    return Policy(make_cfg())


# -- initial tier selection ---------------------------------------------

def test_initial_tier_picks_best_ram_fit(policy):
    assert policy.initial_tier(S(8000)) == 0      # big: 6000+1000 <= 8000
    assert policy.initial_tier(S(5000)) == 1      # mid: 3000+1000 <= 5000
    assert policy.initial_tier(S(2500)) == 2      # small: 1000+1000 <= 2500
    assert policy.initial_tier(S(900)) is None    # nothing fits


def test_initial_tier_respects_vram(policy):
    # RAM would allow big, but VRAM only fits mid (2000+500 <= 3000 < 4500).
    assert policy.initial_tier(S(8000, vram=3000)) == 1
    # VRAM fits nothing GPU-resident -> small (est_vram 0, unconstrained).
    assert policy.initial_tier(S(8000, vram=400)) == 2
    # No GPU detected -> VRAM not enforced.
    assert policy.initial_tier(S(8000, vram=None)) == 0


# -- steady state --------------------------------------------------------

def test_stays_when_all_resources_fine(policy):
    d = policy.evaluate(0, S(3000, vram=1000), now=0)
    assert d.action == "stay"
    assert d.reason == "steady"


# -- RAM downgrades ------------------------------------------------------

def test_downgrade_needs_consecutive_breaches(policy):
    d1 = policy.evaluate(0, S(800), now=0)
    assert d1.action == "stay" and d1.reason == "pressure-pending"
    d2 = policy.evaluate(0, S(800), now=3)
    assert d2.action == "switch"
    assert d2.reason == "memory-pressure"
    # RAM budget after unload: 800 + 6000 = 6800 -> mid fits.
    assert d2.target == 1


def test_single_breach_recovers_without_downgrade(policy):
    policy.evaluate(0, S(800), now=0)
    d = policy.evaluate(0, S(3000, ram_ema=2500), now=3)
    assert d.action == "stay"
    d = policy.evaluate(0, S(800), now=6)
    assert d.action == "stay" and d.reason == "pressure-pending"


def test_pressure_on_smallest_tier_stays(policy):
    policy.evaluate(2, S(800), now=0)
    d = policy.evaluate(2, S(800), now=3)
    assert d.action == "stay"
    assert d.reason == "already-smallest"


# -- VRAM downgrades -----------------------------------------------------

def test_vram_pressure_downgrades(policy):
    # RAM is fine (3000), but free VRAM (300) is under its 500 margin and
    # the current tier lives on the GPU.
    policy.evaluate(0, S(3000, vram=300), now=0)
    d = policy.evaluate(0, S(3000, vram=300), now=3)
    assert d.action == "switch"
    assert d.reason == "vram-pressure"
    # VRAM budget after unload: 300 + 4000 = 4300 -> mid (2000+500) fits.
    assert d.target == 1


def test_vram_pressure_ignored_for_cpu_tier(policy):
    # small has est_vram_mb=0: low VRAM is not its problem.
    policy.evaluate(2, S(3000, vram=100), now=0)
    d = policy.evaluate(2, S(3000, vram=100), now=3)
    assert d.action == "stay" and d.reason == "steady"


def test_vram_pressure_can_skip_to_cpu_tier(policy):
    # VRAM budget after unloading big: 100 + 4000 = 4100; mid needs 2500 ->
    # fits. But if the GPU is nearly full even after unload, fall to small.
    policy.evaluate(0, S(6000, vram=100), now=0)
    p2 = Policy(make_cfg(vram_safety_margin_mb=2500))
    p2.evaluate(0, S(6000, vram=100), now=0)
    d = p2.evaluate(0, S(6000, vram=100), now=3)
    # mid needs 2000+2500=4500 > 4100 budget -> small (no VRAM claim).
    assert d.action == "switch" and d.target == 2


# -- critical ------------------------------------------------------------

def test_critical_downgrades_immediately(policy):
    d = policy.evaluate(0, S(400, ram_ema=2000), now=0)
    assert d.action == "switch"
    assert d.reason == "critical-memory"
    assert d.target == 1


def test_critical_on_smallest_tier_unloads(policy):
    d = policy.evaluate(2, S(100), now=0)
    assert d.action == "unload"


# -- upgrades ------------------------------------------------------------

def test_upgrade_requires_sustained_headroom(policy):
    d = policy.evaluate(1, S(5000), now=0)
    assert d.action == "stay" and d.reason == "upgrade-pending"
    d = policy.evaluate(1, S(5000), now=30)
    assert d.action == "stay" and d.reason == "upgrade-pending"
    d = policy.evaluate(1, S(5000), now=61)
    assert d.action == "switch"
    assert d.target == 0
    assert d.reason == "headroom-recovered"


def test_pressure_resets_upgrade_timer(policy):
    policy.evaluate(1, S(5000), now=0)
    policy.evaluate(1, S(800, ram_ema=4000), now=30)  # dip
    d = policy.evaluate(1, S(5000), now=61)
    assert d.action == "stay"  # timer restarted at t=61
    d = policy.evaluate(1, S(5000), now=122)
    assert d.action == "switch" and d.target == 0


def test_upgrade_blocked_by_vram(policy):
    # RAM headroom is plenty, but big needs 4000+500 VRAM and the budget
    # (free 1500 + current mid 2000 = 3500) is short -> no upgrade.
    d = policy.evaluate(1, S(8000, vram=1500), now=0)
    assert d.action == "stay" and d.reason == "steady"
    # With enough free VRAM the upgrade timer starts.
    d = policy.evaluate(1, S(8000, vram=3000), now=1)
    assert d.reason == "upgrade-pending"


def test_upgrade_blocked_by_low_disk(policy):
    d = policy.evaluate(1, S(8000, disk_free=2000), now=0)
    assert d.action == "stay" and d.reason == "disk-low"
    # Timer never accumulates while disk is low.
    d = policy.evaluate(1, S(8000, disk_free=2000), now=100)
    assert d.action == "stay" and d.reason == "disk-low"
    # Disk freed: timer starts fresh.
    d = policy.evaluate(1, S(8000), now=101)
    assert d.reason == "upgrade-pending"


def test_low_disk_does_not_force_downgrade(policy):
    # Disk gates upgrades only; a healthy current tier stays put.
    d = policy.evaluate(0, S(8000, disk_free=2000), now=0)
    assert d.action == "stay" and d.reason == "disk-low"


def test_no_disk_reading_means_no_gate(policy):
    d = policy.evaluate(1, S(8000, disk_free=None), now=0)
    assert d.reason == "upgrade-pending"
