"""Configuration loading and validation for RAMP.

The config is a single YAML file. The heart of it is the *tier ladder*: an
ordered list of model configurations from best (largest) to smallest. RAMP
keeps tiers sorted by estimated RAM footprint, descending, regardless of the
order they appear in the file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the config file is invalid."""


@dataclass
class TierConfig:
    name: str
    est_ram_mb: float
    # Approximate VRAM footprint when (part of) the model runs on GPU.
    # 0 means the tier claims no VRAM and is never constrained by it.
    est_vram_mb: float = 0.0
    model: str = ""
    ctx: int = 4096
    args: list[str] = field(default_factory=list)
    # Only used by the mock backend: how much memory the fake model
    # process allocates, to make demos realistic.
    mock_ballast_mb: int = 0


@dataclass
class HysteresisConfig:
    # Consecutive low-memory samples required before a downgrade.
    downgrade_after_samples: int = 2
    # Seconds of sustained headroom required before an upgrade.
    upgrade_after_s: float = 120.0
    # Extra headroom (beyond safety_margin_mb) required to upgrade, so a
    # fresh upgrade doesn't immediately breach the margin and bounce back.
    upgrade_extra_mb: float = 1024.0
    # Below this free-RAM floor RAMP acts immediately, bypassing hysteresis.
    critical_free_mb: float = 1024.0


@dataclass
class Config:
    tiers: list[TierConfig]
    hysteresis: HysteresisConfig = field(default_factory=HysteresisConfig)
    backend: str = "llama"  # "llama" | "mock"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8090
    poll_interval_s: float = 3.0
    # Free RAM RAMP tries to keep available for the rest of the system.
    safety_margin_mb: float = 1536.0
    # Free VRAM RAMP tries to keep available (enforced only when an NVIDIA
    # GPU is detected and the tier declares est_vram_mb > 0).
    vram_safety_margin_mb: float = 512.0
    # Extra VRAM headroom required before an upgrade (see upgrade_extra_mb).
    vram_upgrade_extra_mb: float = 256.0
    # Below this free-disk floor, upgrades are blocked (a bigger model can't
    # free disk, so this gates rather than downgrades). Also surfaced in
    # status so clients can warn.
    disk_min_free_mb: float = 5120.0
    # Hard floor on how often an *upgrade* may occur, independent of
    # hysteresis - a rate limiter against churn. Downgrades are never
    # delayed: a late downgrade risks OOM, a late upgrade costs nothing.
    min_swap_interval_s: float = 60.0
    # Path whose drive is watched for free space (default: working dir).
    disk_path: str = "."
    # How long to wait for in-flight requests before swapping anyway.
    drain_timeout_s: float = 30.0
    # How long a queued request waits for a swap to finish before 503.
    queue_timeout_s: float = 120.0
    # How long a backend process gets to become healthy after spawn.
    startup_timeout_s: float = 180.0
    llama_server_bin: str = "llama-server"
    ollama_bin: str = "ollama"
    ollama_url: str = "http://127.0.0.1:11434"
    # Optional file to append backend process output to (default: discard).
    backend_log: str = ""
    # Smoothing factor for the available-memory EMA (higher = more reactive).
    ema_alpha: float = 0.4

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Config:
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a mapping")

        tiers_raw = raw.get("tiers") or []
        if not tiers_raw:
            raise ConfigError("config must define at least one tier")

        backend = str(raw.get("backend", "llama"))
        if backend not in ("llama", "mock", "ollama"):
            raise ConfigError(f"unknown backend {backend!r} (expected 'llama', 'ollama' or 'mock')")

        tiers: list[TierConfig] = []
        for i, t in enumerate(tiers_raw):
            if not isinstance(t, dict) or not t.get("name"):
                raise ConfigError(f"tier #{i} must be a mapping with a 'name'")
            est = t.get("est_ram_mb")
            if not isinstance(est, (int, float)) or est <= 0:
                raise ConfigError(f"tier {t.get('name')!r}: est_ram_mb must be a positive number")
            if backend == "llama" and not t.get("model"):
                raise ConfigError(
                    f"tier {t['name']!r}: 'model' (gguf path) is required "
                    "for the llama backend"
                )
            if backend == "ollama" and not t.get("model"):
                raise ConfigError(
                    f"tier {t['name']!r}: 'model' (ollama model tag) is required "
                    "for the ollama backend"
                )
            est_vram = t.get("est_vram_mb", 0.0)
            if not isinstance(est_vram, (int, float)) or est_vram < 0:
                raise ConfigError(f"tier {t['name']!r}: est_vram_mb must be a non-negative number")
            tiers.append(
                TierConfig(
                    name=str(t["name"]),
                    est_ram_mb=float(est),
                    est_vram_mb=float(est_vram),
                    model=str(t.get("model", "")),
                    ctx=int(t.get("ctx", 4096)),
                    args=[str(a) for a in t.get("args", [])],
                    mock_ballast_mb=int(t.get("mock_ballast_mb", 0)),
                )
            )

        names = [t.name for t in tiers]
        if len(set(names)) != len(names):
            raise ConfigError("tier names must be unique")

        # Best tier first, always.
        tiers.sort(key=lambda t: t.est_ram_mb, reverse=True)

        hys_raw = raw.get("hysteresis") or {}
        hysteresis = HysteresisConfig(
            downgrade_after_samples=int(hys_raw.get("downgrade_after_samples", 2)),
            upgrade_after_s=float(hys_raw.get("upgrade_after_s", 120.0)),
            upgrade_extra_mb=float(hys_raw.get("upgrade_extra_mb", 1024.0)),
            critical_free_mb=float(hys_raw.get("critical_free_mb", 1024.0)),
        )

        listen = raw.get("listen") or {}
        return Config(
            tiers=tiers,
            hysteresis=hysteresis,
            backend=backend,
            listen_host=str(listen.get("host", "127.0.0.1")),
            listen_port=int(listen.get("port", 8090)),
            poll_interval_s=float(raw.get("poll_interval_s", 3.0)),
            safety_margin_mb=float(raw.get("safety_margin_mb", 1536.0)),
            vram_safety_margin_mb=float(raw.get("vram_safety_margin_mb", 512.0)),
            vram_upgrade_extra_mb=float(raw.get("vram_upgrade_extra_mb", 256.0)),
            disk_min_free_mb=float(raw.get("disk_min_free_mb", 5120.0)),
            min_swap_interval_s=float(raw.get("min_swap_interval_s", 60.0)),
            disk_path=str(raw.get("disk_path", ".")),
            drain_timeout_s=float(raw.get("drain_timeout_s", 30.0)),
            queue_timeout_s=float(raw.get("queue_timeout_s", 120.0)),
            startup_timeout_s=float(raw.get("startup_timeout_s", 180.0)),
            llama_server_bin=str(raw.get("llama_server_bin", "llama-server")),
            ollama_bin=str(raw.get("ollama_bin", "ollama")),
            ollama_url=str(raw.get("ollama_url", "http://127.0.0.1:11434")),
            backend_log=str(raw.get("backend_log", "")),
            ema_alpha=float(raw.get("ema_alpha", 0.4)),
        )

    @staticmethod
    def load(path: str | Path) -> Config:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return Config.from_dict(raw)
