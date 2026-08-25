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
_WEIGHT_MULTIPLIER = 1.30
_VRAM_MULTIPLIER = 1.15
_VRAM_OVERHEAD_MB = 200


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
    """Conservative (RAM, VRAM) estimate for a model of this weight size."""
    ram = size_mb * _WEIGHT_MULTIPLIER + _RUNTIME_OVERHEAD_MB
    if total_vram_mb is None:
        return round(ram), 0.0
    vram = size_mb * _VRAM_MULTIPLIER + _VRAM_OVERHEAD_MB
    # If it can't plausibly sit on the GPU, don't declare a VRAM claim: the
    # runtime will keep it (mostly) on the CPU, and a tier with no VRAM claim
    # stays available when the GPU is full.
    if vram > total_vram_mb:
        return round(ram), 0.0
    return round(ram), round(vram)


def select_ladder(
    models: list[DetectedModel], total_ram_mb: float, max_tiers: int = 4
) -> list[DetectedModel]:
    """Pick a spread of models that could actually run on this machine.

    Sorted largest-first (RAMP's tier order). Models too big to ever fit are
    dropped; if that leaves nothing, the smallest is kept so the daemon has
    something to try.
    """
    if not models:
        return []
    ranked = sorted(models, key=lambda m: m.size_mb, reverse=True)
    viable = [
        m for m in ranked
        if estimate_footprint(m.size_mb, None)[0] <= total_ram_mb
    ]
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
) -> dict:
    """Assemble a full RAMP config dict from detected hardware and models."""
    ladder = select_ladder(models, total_ram_mb)
    if not ladder:
        raise AutoConfigError("no usable models found")

    # Reserve a slice of total RAM for the rest of the system: 15%, clamped
    # to something sane on both tiny and huge machines.
    margin = min(max(total_ram_mb * 0.15, 1024), 4096)

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
        "min_swap_interval_s": 60,
        "tiers": tiers,
    }


def autodetect(ollama_url: str = "http://127.0.0.1:11434", port: int = 8090) -> dict:
    """Inspect this machine and return a ready-to-use config dict."""
    total_ram_mb = psutil.virtual_memory().total / _MB
    gpu = ResourceMonitor().sample().gpu
    total_vram_mb = gpu.total_mb if gpu is not None else None
    models = fetch_ollama_models(ollama_url)
    return build_config(models, total_ram_mb, total_vram_mb, ollama_url, port)


def describe(cfg: dict, total_vram_mb: float | None = None) -> str:
    """Human-readable summary of an auto-detected config."""
    lines = [
        f"Auto-detected {len(cfg['tiers'])} tier(s) from Ollama "
        f"(safety margin {cfg['safety_margin_mb']} MB"
        + (f", {round(total_vram_mb)} MB VRAM" if total_vram_mb else "")
        + "):"
    ]
    for i, t in enumerate(cfg["tiers"]):
        vram = f", ~{t['est_vram_mb']} MB VRAM" if t.get("est_vram_mb") else ""
        lines.append(f"  {i + 1}. {t['name']}  (~{t['est_ram_mb']} MB RAM{vram})")
    return "\n".join(lines)
