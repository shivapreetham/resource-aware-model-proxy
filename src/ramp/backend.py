"""Backend process management.

A backend is one child process serving an OpenAI-compatible HTTP API for the
currently active tier. Two implementations:

- ``LlamaServerBackend`` spawns llama.cpp's ``llama-server`` with the tier's
  GGUF model and context size.
- ``MockBackend`` spawns ``ramp.mock_llm``, a tiny fake OpenAI server that
  echoes and can allocate ballast memory - so the whole daemon can be
  demoed and integration-tested with no model downloads.

Both expose ``/health`` for readiness checks (llama-server has it natively).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import sys
from typing import Optional

import httpx

from .config import Config, TierConfig

log = logging.getLogger("ramp.backend")


class BackendError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProcessBackend:
    """Manages the lifecycle of a single child inference server."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.port: Optional[int] = None
        self.tier: Optional[TierConfig] = None
        self._log_handle = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def serving_model(self) -> Optional[str]:
        """Model name the proxy should rewrite requests to (None = leave as-is)."""
        return None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def _command(self, tier: TierConfig, port: int) -> list[str]:
        raise NotImplementedError

    def _stdio(self):
        if self.cfg.backend_log:
            if self._log_handle is None:
                self._log_handle = open(self.cfg.backend_log, "ab")
            return self._log_handle
        return asyncio.subprocess.DEVNULL

    async def start(self, tier: TierConfig) -> None:
        if self.alive():
            raise BackendError("backend already running; stop it first")
        port = _free_port()
        cmd = self._command(tier, port)
        log.info("starting backend for tier %r: %s", tier.name, " ".join(cmd))
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        out = self._stdio()
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=out, stderr=out, **kwargs
        )
        self.port = port
        self.tier = tier
        try:
            await self._wait_healthy()
        except BaseException:
            await self.stop()
            raise
        log.info("tier %r healthy on port %d", tier.name, port)

    async def _wait_healthy(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.startup_timeout_s
        async with httpx.AsyncClient() as client:
            while True:
                if self.proc is None or self.proc.returncode is not None:
                    code = None if self.proc is None else self.proc.returncode
                    raise BackendError(f"backend process exited during startup (code {code})")
                try:
                    r = await client.get(f"{self.base_url}/health", timeout=2.0)
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                if loop.time() > deadline:
                    raise BackendError(
                        f"backend for tier {self.tier.name!r} not healthy "
                        f"after {self.cfg.startup_timeout_s}s"
                    )
                await asyncio.sleep(0.25)

    async def stop(self) -> None:
        proc, self.proc = self.proc, None
        self.tier = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def close(self) -> None:
        await self.stop()
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


class LlamaServerBackend(ProcessBackend):
    def _command(self, tier: TierConfig, port: int) -> list[str]:
        return [
            self.cfg.llama_server_bin,
            "-m", tier.model,
            "-c", str(tier.ctx),
            "--host", "127.0.0.1",
            "--port", str(port),
            *tier.args,
        ]


class MockBackend(ProcessBackend):
    def _command(self, tier: TierConfig, port: int) -> list[str]:
        return [
            sys.executable,
            "-m", "ramp.mock_llm",
            "--port", str(port),
            "--name", tier.name,
            "--ballast-mb", str(tier.mock_ballast_mb),
        ]


class OllamaBackend:
    """Drives a local Ollama server instead of spawning per-tier processes.

    Ollama is a signed, widely-installed runtime (useful e.g. on Windows
    machines where Smart App Control blocks unsigned llama-server builds).
    Tiers reference Ollama model tags; RAMP loads/unloads them via
    ``/api/generate`` with ``keep_alive`` and proxies ``/v1/*`` to Ollama's
    OpenAI-compatible API. If no server is reachable at ``ollama_url``,
    ``ollama serve`` is spawned and owned by RAMP.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.tier: Optional[TierConfig] = None
        self.proc: Optional[asyncio.subprocess.Process] = None  # only if we spawned serve

    @property
    def base_url(self) -> str:
        return self.cfg.ollama_url

    @property
    def serving_model(self) -> Optional[str]:
        return self.tier.model if self.tier is not None else None

    def alive(self) -> bool:
        if self.tier is None:
            return False
        # If we own the server process, its death means we're down.
        return self.proc is None or self.proc.returncode is None

    async def _ensure_server(self) -> None:
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(f"{self.base_url}/api/version", timeout=2.0)
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            log.info("no ollama server at %s; spawning 'ollama serve'", self.base_url)
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.proc = await asyncio.create_subprocess_exec(
                self.cfg.ollama_bin, "serve",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **kwargs,
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30.0
            while True:
                if self.proc.returncode is not None:
                    raise BackendError(f"'ollama serve' exited with code {self.proc.returncode}")
                try:
                    r = await client.get(f"{self.base_url}/api/version", timeout=2.0)
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                if loop.time() > deadline:
                    raise BackendError("ollama server did not become healthy in 30s")
                await asyncio.sleep(0.25)

    async def start(self, tier: TierConfig) -> None:
        await self._ensure_server()
        log.info("loading ollama model %r", tier.model)
        async with httpx.AsyncClient() as client:
            try:
                # Empty-prompt generate = load the model; keep_alive=-1 pins
                # it in memory until RAMP explicitly unloads it.
                r = await client.post(
                    f"{self.base_url}/api/generate",
                    json={"model": tier.model, "keep_alive": -1},
                    timeout=self.cfg.startup_timeout_s,
                )
            except httpx.HTTPError as e:
                raise BackendError(f"failed to load ollama model {tier.model!r}: {e}")
        if r.status_code != 200:
            raise BackendError(
                f"ollama refused to load {tier.model!r}: {r.status_code} {r.text[:200]}"
            )
        self.tier = tier
        log.info("ollama model %r loaded", tier.model)

    async def stop(self) -> None:
        tier, self.tier = self.tier, None
        if tier is None:
            return
        async with httpx.AsyncClient() as client:
            try:
                await client.post(
                    f"{self.base_url}/api/generate",
                    json={"model": tier.model, "keep_alive": 0},
                    timeout=30.0,
                )
                log.info("ollama model %r unloaded", tier.model)
            except httpx.HTTPError as e:
                log.warning("failed to unload ollama model %r: %s", tier.model, e)

    async def close(self) -> None:
        await self.stop()
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass


def make_backend(cfg: Config):
    if cfg.backend == "mock":
        return MockBackend(cfg)
    if cfg.backend == "ollama":
        return OllamaBackend(cfg)
    return LlamaServerBackend(cfg)
