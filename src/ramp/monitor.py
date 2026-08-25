"""System resource sampling: RAM, GPU/VRAM, and disk.

Each sample carries raw readings (used for downgrade decisions, where
reacting fast matters) and exponentially smoothed ones (used for upgrade
decisions, where a momentary dip shouldn't cancel a recovery).

GPU support is NVIDIA-only for now, via ``nvidia-smi``. If the tool is
missing or fails, GPU readings are simply absent and VRAM constraints are
not enforced. Disk is sampled with ``shutil.disk_usage`` on the configured
path (default: the working directory's drive).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

import psutil

_MB = 1024 * 1024


@dataclass
class MemorySample:
    raw_mb: float
    ema_mb: float
    total_mb: float
    percent: float


@dataclass
class GpuSample:
    name: str
    total_mb: float
    free_raw_mb: float
    free_ema_mb: float
    used_mb: float
    util_percent: float


@dataclass
class DiskSample:
    path: str
    free_mb: float
    total_mb: float


@dataclass
class ResourceSample:
    ram: MemorySample
    gpu: Optional[GpuSample]
    disk: Optional[DiskSample]
    ts: float


class ResourceMonitor:
    def __init__(
        self,
        ema_alpha: float = 0.4,
        disk_path: str = ".",
        nvidia_smi: str = "nvidia-smi",
    ) -> None:
        self.alpha = ema_alpha
        self.disk_path = disk_path
        self._ram_ema: Optional[float] = None
        self._vram_ema: Optional[float] = None
        self._nvidia_smi = shutil.which(nvidia_smi)
        self._gpu_broken = False

    def _ema(self, prev: Optional[float], raw: float) -> float:
        if prev is None:
            return raw
        return self.alpha * raw + (1 - self.alpha) * prev

    def _sample_ram(self) -> MemorySample:
        vm = psutil.virtual_memory()
        raw = vm.available / _MB
        self._ram_ema = self._ema(self._ram_ema, raw)
        return MemorySample(
            raw_mb=raw,
            ema_mb=self._ram_ema,
            total_mb=vm.total / _MB,
            percent=vm.percent,
        )

    def _sample_gpu(self) -> Optional[GpuSample]:
        if self._nvidia_smi is None or self._gpu_broken:
            return None
        try:
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            out = subprocess.run(
                [
                    self._nvidia_smi,
                    "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                **kwargs,
            )
            if out.returncode != 0 or not out.stdout.strip():
                self._gpu_broken = True
                return None
            # First GPU only for now.
            parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
            name, total, used, free, util = parts[:5]
            free_raw = float(free)
            self._vram_ema = self._ema(self._vram_ema, free_raw)
            return GpuSample(
                name=name,
                total_mb=float(total),
                free_raw_mb=free_raw,
                free_ema_mb=self._vram_ema,
                used_mb=float(used),
                util_percent=float(util),
            )
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            self._gpu_broken = True
            return None

    def _sample_disk(self) -> Optional[DiskSample]:
        try:
            usage = shutil.disk_usage(self.disk_path)
            return DiskSample(
                path=self.disk_path,
                free_mb=usage.free / _MB,
                total_mb=usage.total / _MB,
            )
        except OSError:
            return None

    def sample(self) -> ResourceSample:
        return ResourceSample(
            ram=self._sample_ram(),
            gpu=self._sample_gpu(),
            disk=self._sample_disk(),
            ts=time.time(),
        )
