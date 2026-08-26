"""The demo has to work on someone else's machine, not just the author's.

These tests pin the property that makes that true: the ladder is derived
from available memory, so the same commands behave the same way on a small
laptop and a large workstation.
"""
import pytest

from ramp.config import Config
from ramp.demo import demo_config

# 2 GB netbook through 64 GB workstation.
SIZES = [1500, 2000, 4000, 8000, 16000, 32000, 64000]


@pytest.mark.parametrize("free_mb", SIZES)
def test_demo_ladder_is_valid_on_any_machine(free_mb):
    cfg = Config.from_dict(demo_config(free_mb))
    assert len(cfg.tiers) == 3
    assert cfg.tiers[0].est_ram_mb > cfg.tiers[-1].est_ram_mb


@pytest.mark.parametrize("free_mb", SIZES)
def test_demo_starts_on_the_big_tier(free_mb):
    """If it started small there'd be nothing to demonstrate."""
    cfg = Config.from_dict(demo_config(free_mb))
    top = cfg.tiers[0]
    assert top.est_ram_mb + cfg.safety_margin_mb <= free_mb


@pytest.mark.parametrize("free_mb", SIZES)
def test_modest_pressure_triggers_a_downgrade(free_mb):
    """`ramp stress` leaves ~35% of free memory. That must breach the margin,
    or the demo silently does nothing - which is how it failed the first
    two times it was tried."""
    cfg = Config.from_dict(demo_config(free_mb))
    under_stress = free_mb * 0.35
    assert under_stress < cfg.safety_margin_mb


@pytest.mark.parametrize("free_mb", SIZES)
def test_a_smaller_tier_fits_once_pressure_hits(free_mb):
    """Breaching isn't enough - something smaller has to actually fit,
    otherwise RAMP unloads everything and the demo shows an error."""
    cfg = Config.from_dict(demo_config(free_mb))
    top = cfg.tiers[0]
    budget = free_mb * 0.35 + top.est_ram_mb      # freed by unloading the top
    fits = [t for t in cfg.tiers[1:]
            if t.est_ram_mb + cfg.safety_margin_mb <= budget]
    assert fits, "nothing smaller fits - RAMP would unload entirely"


@pytest.mark.parametrize("free_mb", SIZES)
def test_it_climbs_back_when_memory_returns(free_mb):
    cfg = Config.from_dict(demo_config(free_mb))
    top, mid = cfg.tiers[0], cfg.tiers[1]
    budget = free_mb + mid.est_ram_mb             # recovered, sitting on mid
    needed = top.est_ram_mb + cfg.safety_margin_mb + cfg.hysteresis.upgrade_extra_mb
    assert needed <= budget


def test_tiny_machine_does_not_produce_nonsense():
    """A nearly-full machine shouldn't yield zero-sized or negative tiers."""
    cfg = Config.from_dict(demo_config(50))
    assert all(t.est_ram_mb > 0 for t in cfg.tiers)
    assert cfg.safety_margin_mb > 0
