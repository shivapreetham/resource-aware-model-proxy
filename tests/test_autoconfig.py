"""Unit tests for zero-config detection and the doctor checks."""
import pytest

from ramp.autoconfig import (
    AutoConfigError,
    DetectedModel,
    build_config,
    describe,
    estimate_footprint,
    select_ladder,
)
from ramp.config import Config
from ramp.doctor import Check, check_python, worst

MODELS = [
    DetectedModel("tiny:0.5b", 400),
    DetectedModel("small:1.5b", 1000),
    DetectedModel("mid:3b", 2000),
    DetectedModel("big:7b", 4500),
    DetectedModel("huge:70b", 40000),
]


# -- footprint estimation ------------------------------------------------

def test_estimate_is_conservative():
    """An estimate must exceed the raw weight size - weights aren't the whole cost."""
    ram, _ = estimate_footprint(1000, None)
    assert ram > 1000


def test_no_gpu_means_no_vram_claim():
    _, vram = estimate_footprint(1000, None)
    assert vram == 0.0


def test_vram_claimed_when_it_fits():
    _, vram = estimate_footprint(1000, total_vram_mb=8000)
    assert vram > 0


def test_model_too_big_for_gpu_makes_no_vram_claim():
    """A model that can't sit on the GPU must not be VRAM-constrained, so it
    stays available as a fallback when the GPU is full."""
    _, vram = estimate_footprint(20000, total_vram_mb=8000)
    assert vram == 0.0


# -- ladder selection ----------------------------------------------------

def test_ladder_is_ordered_largest_first():
    ladder = select_ladder(MODELS, total_ram_mb=64000)
    sizes = [m.size_mb for m in ladder]
    assert sizes == sorted(sizes, reverse=True)


def test_ladder_drops_models_that_cannot_fit():
    ladder = select_ladder(MODELS, total_ram_mb=8000)
    assert all(m.name != "huge:70b" for m in ladder)


def test_ladder_caps_tier_count_and_spans_the_range():
    ladder = select_ladder(MODELS, total_ram_mb=64000, max_tiers=3)
    assert len(ladder) == 3
    # Must include the biggest viable and the smallest, not just the top 3.
    assert ladder[0].name == "huge:70b"
    assert ladder[-1].name == "tiny:0.5b"


def test_ladder_keeps_something_when_nothing_fits():
    ladder = select_ladder(MODELS, total_ram_mb=100)
    assert len(ladder) == 1
    assert ladder[0].name == "tiny:0.5b"  # the smallest, as a last resort


def test_empty_model_list():
    assert select_ladder([], total_ram_mb=16000) == []


# -- generated config ----------------------------------------------------

def test_generated_config_is_valid():
    raw = build_config(MODELS, total_ram_mb=16000, total_vram_mb=8000)
    cfg = Config.from_dict(raw)          # must survive real validation
    assert cfg.backend == "ollama"
    assert len(cfg.tiers) >= 2
    # Config sorts tiers best-first by footprint.
    assert cfg.tiers[0].est_ram_mb > cfg.tiers[-1].est_ram_mb


def test_generated_config_reserves_a_margin():
    raw = build_config(MODELS, total_ram_mb=16000, total_vram_mb=None)
    assert 500 <= raw["safety_margin_mb"] <= 4096


def test_margin_is_clamped_on_a_huge_machine():
    raw = build_config(MODELS, total_ram_mb=512000, total_vram_mb=None)
    assert raw["safety_margin_mb"] <= 4096


def test_build_config_rejects_no_models():
    with pytest.raises(AutoConfigError):
        build_config([], total_ram_mb=16000, total_vram_mb=None)


def test_describe_mentions_every_tier():
    raw = build_config(MODELS, total_ram_mb=16000, total_vram_mb=8000)
    text = describe(raw)
    for t in raw["tiers"]:
        assert t["name"] in text


# -- doctor --------------------------------------------------------------

def test_python_check_passes_on_a_supported_interpreter():
    assert check_python().status == "ok"


def test_worst_status_ranking():
    assert worst([Check("a", "ok", "")]) == "ok"
    assert worst([Check("a", "ok", ""), Check("b", "warn", "")]) == "warn"
    assert worst([Check("a", "warn", ""), Check("b", "fail", "")]) == "fail"


def test_failing_checks_carry_a_fix():
    """A failure the user can't act on is a bad error message."""
    from ramp.doctor import check_ollama
    checks = check_ollama("http://127.0.0.1:59999")  # nothing listening
    assert checks[0].status == "fail"
    assert checks[0].fix, "a FAIL check must tell the user what to do"


# -- GPU double-counting -------------------------------------------------
#
# Regression: a GPU-resident model was charged its weights against system RAM
# as well as VRAM. On a 16 GB machine a 5 GB model was billed ~6.7 GB of RAM
# it never used, so with a 2.4 GB margin the top tier needed 8.8 GB free and
# could never load. RAMP silently refused to use the best model available.

def test_a_gpu_resident_model_is_not_billed_its_weights_twice():
    big = 5000  # MB of weights, comfortably inside an 8 GB card
    ram_gpu, vram = estimate_footprint(big, total_vram_mb=8000)
    ram_cpu, _ = estimate_footprint(big, total_vram_mb=None)
    assert vram > big, "weights belong in VRAM"
    assert ram_gpu < big, "weights must not also be charged to RAM"
    assert ram_gpu < ram_cpu / 2, "GPU-resident should cost far less RAM"


def test_a_model_too_big_for_the_gpu_is_charged_to_ram():
    ram, vram = estimate_footprint(20000, total_vram_mb=8000)
    assert vram == 0.0
    assert ram > 20000, "it lives in RAM, so charge it there"


def test_gpu_resident_tiers_stay_distinguishable():
    """A flat RAM cost would make every tier identical, collapsing the
    ladder - under RAM pressure there would be nothing smaller to move to."""
    small, _ = estimate_footprint(500, total_vram_mb=8000)
    large, _ = estimate_footprint(5000, total_vram_mb=8000)
    assert large > small


# -- profiles ------------------------------------------------------------

@pytest.mark.parametrize("profile", ["safe", "balanced", "aggressive"])
def test_every_profile_builds_a_valid_config(profile):
    raw = build_config(MODELS, 16000, 8000, profile=profile)
    cfg = Config.from_dict(raw)
    assert cfg.safety_margin_mb > 0
    assert len(cfg.tiers) >= 2


def test_aggressive_leaves_about_500mb_and_climbs_back_fast():
    raw = build_config(MODELS, 16000, 8000, profile="aggressive")
    assert raw["safety_margin_mb"] == 500
    assert raw["hysteresis"]["upgrade_after_s"] <= 30
    assert raw["min_swap_interval_s"] == 0


def test_profiles_are_ordered_from_cautious_to_greedy():
    margins = [
        build_config(MODELS, 16000, 8000, profile=p)["safety_margin_mb"]
        for p in ("safe", "balanced", "aggressive")
    ]
    assert margins == sorted(margins, reverse=True)


def test_an_unknown_profile_falls_back_rather_than_crashing():
    raw = build_config(MODELS, 16000, 8000, profile="nonsense")
    assert raw["safety_margin_mb"] > 0


def test_a_big_gpu_makes_big_models_viable():
    """Judging viability by system RAM alone rejected a 20 GB model on a
    machine with a 24 GB card - so anyone with a real GPU was never offered
    the models they bought it for."""
    lib = [DetectedModel("big", 20000), DetectedModel("small", 400)]
    without_gpu = select_ladder(lib, total_ram_mb=16000)
    with_gpu = select_ladder(lib, total_ram_mb=16000, total_vram_mb=24000)
    assert "big" not in [m.name for m in without_gpu]
    assert "big" in [m.name for m in with_gpu]


def test_a_large_library_still_spans_the_range():
    """People keep a dozen models. Taking the top N would give a ladder of
    near-identical giants with nothing to fall back to."""
    lib = [DetectedModel(f"m{i}", s) for i, s in
           enumerate([9000, 8500, 8000, 7500, 4400, 1900, 1000, 400])]
    ladder = select_ladder(lib, total_ram_mb=64000, max_tiers=4)
    assert len(ladder) == 4
    assert ladder[-1].size_mb == 400, "the smallest must be kept as a floor"
    assert ladder[0].size_mb == 9000, "and the largest as the goal"
