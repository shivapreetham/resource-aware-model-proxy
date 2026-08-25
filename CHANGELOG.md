# Changelog

All notable changes to RAMP are documented here. This project follows
[Semantic Versioning](https://semver.org/) and
[Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [0.1.0] — 2026-08-26

First public release.

### Added

- **Elastic tier ladder.** One OpenAI-compatible endpoint that moves the
  loaded model up and down a configured ladder as system resources change.
- **Multi-resource policy.** Holds RAM, VRAM (NVIDIA via `nvidia-smi`), and
  free disk accountable together. RAM/VRAM pressure triggers downgrades; low
  disk gates upgrades only, since a smaller model cannot free disk.
- **Hysteresis and damping.** Fast downgrades (consecutive breaches, plus an
  instant critical floor), slow upgrades (sustained headroom + extra margin),
  and a `min_swap_interval_s` cooldown that rate-limits upgrades to bound
  churn.
- **Swap lifecycle with a request gate.** In-flight generations are drained
  before a swap; incoming requests wait on the gate rather than failing;
  failed tier starts fall through to progressively smaller tiers.
- **Backends.** `llama` (spawns `llama-server` per tier), `ollama` (loads and
  unloads via `keep_alive`, useful where OS policy blocks unsigned binaries),
  and `mock` (fake OpenAI servers for tests and demos, no model downloads).
- **Observability.** `GET /ramp/status` reports the active tier, resource
  readings, `last_decision`, tier occupancy, and an event log;
  `GET /ramp/metrics` exposes Prometheus text format for scraping.
- **Manual override.** `POST /ramp/pin/{tier}` and `DELETE /ramp/pin`.
- **Transparent proxying.** `/v1/*` passthrough including SSE streaming, an
  `x-ramp-tier` response header, and model-name rewriting for backends that
  route by name.
- Documentation: [README](README.md), a concepts guide
  ([docs/CONCEPTS.md](docs/CONCEPTS.md)), and example configs for mock,
  llama.cpp, Ollama, and VRAM-pressure testing.

### Known limitations

- The `llama` backend is untested on Windows machines with Smart App Control
  enforced, which blocks unsigned binaries (`WinError 4551`). Use the
  `ollama` backend there.
- GPU monitoring reads the first NVIDIA GPU only; multi-GPU and non-NVIDIA
  devices are not yet supported.
- Swaps happen between requests, never mid-generation.

[Unreleased]: https://github.com/shivapreetham/ramp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/shivapreetham/ramp/releases/tag/v0.1.0
