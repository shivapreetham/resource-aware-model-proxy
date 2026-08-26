"""Zero-config startup: build a tier ladder by looking at the machine.

The point of RAMP is that you shouldn't have to think about memory. Making
people hand-write a YAML ladder before they can try it undercuts that. So
``ramp`` with no arguments inspects what's actually installed - Ollama's
model library, system RAM, GPU VRAM - and assembles a sensible ladder.

The footprint estimates here are deliberately *conservative*: underestimating
a tier is far more dangerous than overestimating it, because it lets RAMP
load something that doesn't fit. Calibrate them properly by pinning a tier
and watching real usage (see docs/MONITORING.md).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

import psutil

from .monitor import ResourceMonitor

_MB = 1024 * 1024

# Runtime overhead beyond the weights: KV cache, activations, the server
# process itself. A flat allowance, rounded up on purpose.
_RUNTIME_OVERHEAD_MB = 300
# Weights inflate somewhat once loaded (KV cache scales with context).
_WEIGHT_MULTIPLIER = 1.20
_VRAM_MULTIPLIER = 1.15
_VRAM_OVERHEAD_MB = 200
# What a GPU-resident model still costs in *system* RAM: the server process
# and buffers, not the weights - those live in VRAM. A share of the weights
# is still counted, because runtimes spill layers to the CPU when a model
# only just fits, and because a flat cost would make every tier identical in
# RAM - collapsing the ladder, leaving nothing to step down to.
_GPU_RESIDENT_BASE_MB = 500
_GPU_RESIDENT_WEIGHT_SHARE = 0.15

#: Presets. The margin is what RAMP refuses to consume; a large one keeps
#: the machine responsive but can make the top tier unreachable, which is
#: worse than useless - it means never using the model you downloaded.
PROFILES = {
    "safe": {
        "margin_fraction": 0.15, "margin_min": 1536, "margin_max": 4096,
        "upgrade_after_s": 180, "min_swap_interval_s": 120,
    },
    "balanced": {
        "margin_fraction": 0.08, "margin_min": 768, "margin_max": 2048,
        "upgrade_after_s": 90, "min_swap_interval_s": 45,
    },
    # "use the machine". Leaves only a sliver free, and climbs back quickly.
    "aggressive": {
        "margin_fraction": 0.0, "margin_min": 500, "margin_max": 500,
        "upgrade_after_s": 20, "min_swap_interval_s": 0,
    },
}
DEFAULT_PROFILE = "balanced"


class AutoConfigError(RuntimeError):
    """Raised when the machine can't be inspected well enough to run."""


@dataclass
class DetectedModel:
    name: str
    size_mb: float


def fetch_ollama_models(url: str = "http://127.0.0.1:11434") -> list[DetectedModel]:
    """List models installed in a local Ollama server."""
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=5) as r:
            payload = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AutoConfigError(
            f"couldn't reach Ollama at {url} ({e}). Start it with 'ollama serve', "
            "or write a config and pass it with -c."
        ) from e

    models = []
    for m in payload.get("models", []):
        name, size = m.get("name"), m.get("size")
        if name and isinstance(size, (int, float)) and size > 0:
            models.append(DetectedModel(name=name, size_mb=size / _MB))
    if not models:
        raise AutoConfigError(
            "Ollama is running but has no models. Pull one first, e.g. "
            "'ollama pull qwen2.5:3b'."
        )
    return models


def estimate_footprint(
    size_mb: float, total_vram_mb: float | None
) -> tuple[float, float]:
    """(RAM, VRAM) estimate for a model of this weight size.

    The important case is a model that fits on the GPU: its weights live in
    VRAM, so charging them against system RAM as well is double-counting.
    Doing that made large models permanently unloadable - a 5 GB model was
    billed ~6.7 GB of RAM it never used, so the top tier of a ladder could
    never be reached on a 16 GB machine.
    """
    if total_vram_mb is not None:
        vram = size_mb * _VRAM_MULTIPLIER + _VRAM_OVERHEAD_MB
        if vram <= total_vram_mb:
            # GPU-resident: the weights are in VRAM, so charge RAM only for
            # the process plus a share for spill.
            ram = _GPU_RESIDENT_BASE_MB + size_mb * _GPU_RESIDENT_WEIGHT_SHARE
            return round(ram), round(vram)
    # CPU-resident (no GPU, or too big for it): the weights are in RAM.
    return round(size_mb * _WEIGHT_MULTIPLIER + _RUNTIME_OVERHEAD_MB), 0.0


def select_ladder(
    models: list[DetectedModel],
    total_ram_mb: float,
    max_tiers: int = 4,
    total_vram_mb: float | None = None,
) -> list[DetectedModel]:
    """Pick a spread of models that could actually run on this machine.

    Sorted largest-first (RAMP's tier order). A model is viable if it fits
    *either* in VRAM or in system RAM - judging only by RAM meant a 20 GB
    model was rejected on a machine with a 24 GB GPU, so people with real
    graphics cards were never offered the models they bought them for.

    People keep large libraries, so the ladder is a spread across the size
    range rather than the top N: a ladder of four 30B variants has nothing
    to fall back to.
    """
    if not models:
        return []
    ranked = sorted(models, key=lambda m: m.size_mb, reverse=True)

    def viable_on_this_machine(m: DetectedModel) -> bool:
        if estimate_footprint(m.size_mb, None)[0] <= total_ram_mb:
            return True
        if total_vram_mb is not None:
            _, vram = estimate_footprint(m.size_mb, total_vram_mb)
            return vram > 0  # a VRAM claim means it fits the card
        return False

    viable = [m for m in ranked if viable_on_this_machine(m)]
    if not viable:
        viable = ranked[-1:]
    if len(viable) <= max_tiers:
        return viable
    # Spread across the size range rather than taking the top N, so the
    # ladder actually spans from "good" to "always fits".
    step = (len(viable) - 1) / (max_tiers - 1)
    picked_idx = sorted({round(i * step) for i in range(max_tiers)})
    return [viable[i] for i in picked_idx]


def build_config(
    models: list[DetectedModel],
    total_ram_mb: float,
    total_vram_mb: float | None,
    ollama_url: str = "http://127.0.0.1:11434",
    port: int = 8090,
    profile: str = DEFAULT_PROFILE,
) -> dict:
    """Assemble a full RAMP config dict from detected hardware and models."""
    ladder = select_ladder(models, total_ram_mb, total_vram_mb=total_vram_mb)
    if not ladder:
        raise AutoConfigError("no usable models found")
    prof = PROFILES.get(profile) or PROFILES[DEFAULT_PROFILE]

    # What RAMP refuses to consume, so the rest of the machine keeps working.
    margin = min(
        max(total_ram_mb * prof["margin_fraction"], prof["margin_min"]),
        prof["margin_max"],
    )

    tiers = []
    for m in ladder:
        ram, vram = estimate_footprint(m.size_mb, total_vram_mb)
        tier = {"name": m.name, "model": m.name, "est_ram_mb": ram}
        if vram:
            tier["est_vram_mb"] = vram
        tiers.append(tier)

    return {
        "backend": "ollama",
        "ollama_url": ollama_url,
        "listen": {"host": "127.0.0.1", "port": port},
        "poll_interval_s": 3,
        "safety_margin_mb": round(margin),
        "min_swap_interval_s": prof["min_swap_interval_s"],
        "hysteresis": {"upgrade_after_s": prof["upgrade_after_s"]},
        "tiers": tiers,
    }


def autodetect(
    ollama_url: str = "http://127.0.0.1:11434",
    port: int = 8090,
    profile: str = DEFAULT_PROFILE,
) -> dict:
    """Inspect this machine and return a ready-to-use config dict."""
    total_ram_mb = psutil.virtual_memory().total / _MB
    gpu = ResourceMonitor().sample().gpu
    total_vram_mb = gpu.total_mb if gpu is not None else None
    models = fetch_ollama_models(ollama_url)
    return build_config(
        models, total_ram_mb, total_vram_mb, ollama_url, port, profile
    )


def describe(cfg: dict, total_vram_mb: float | None = None) -> str:
    """One line. `ramp status` is there for anyone who wants the numbers."""
    names = " > ".join(t["name"] for t in cfg["tiers"])
    return f"Ladder: {names}"


def describe_verbose(cfg: dict) -> str:
    lines = [f"Ladder ({len(cfg['tiers'])} tiers, "
             f"margin {cfg['safety_margin_mb']} MB):"]
    for t in cfg["tiers"]:
        vram = f" + {t['est_vram_mb']} MB VRAM" if t.get("est_vram_mb") else ""
        lines.append(f"  {t['name']}  ~{t['est_ram_mb']} MB RAM{vram}")
    return "\n".join(lines)
