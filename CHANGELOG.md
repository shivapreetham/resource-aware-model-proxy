# Changelog

All notable changes to RAMP are documented here. This project follows
[Semantic Versioning](https://semver.org/) and
[Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [0.2.1] — 2026-08-26

### Changed

- Renamed `--takeover` to `--transparent`. "Transparent proxy" is both the
  accurate term and a fairer description of something that asks permission
  and reverses itself.

### Fixed

- **Transparent mode could leave Ollama down.** Restoring only happened on a
  clean exit, so a hard kill or crash skipped it entirely and left Ollama
  moved with nothing on its usual port. Found by killing a live session. The
  arrangement is now written to disk when engaged, `ramp restore` puts Ollama
  back, and `ramp` warns on startup if a previous session did not shut down
  cleanly. Repair is idempotent and will not start a second Ollama if one is
  already answering.

## [0.2.0] — 2026-08-26

### Added

- **Transparent mode** (`ramp run --transparent`). RAMP can serve on Ollama's
  port and relocate Ollama behind it, so every tool already pointed at
  `localhost:11434` routes through RAMP with **no client configuration at
  all** — including tools using Ollama's native `/api/*` routes, not just
  OpenAI-shaped ones.

  It is consent-based and reversible by construction:
  - Never proceeds without an explicit prompt (`--yes` to skip, and it
    refuses outright in a non-interactive shell without it).
  - The replacement Ollama is started and proven healthy **before** the
    original is stopped, so a failure at any step rolls back and leaves
    Ollama exactly as it was.
  - Refuses up front — changing nothing — if Ollama isn't on the target
    port, the relocation port is busy, or the `ollama` binary can't be
    found to restart it with.
  - Restores Ollama to its original port automatically when RAMP exits.

  Configurable via `--transparent-port`, `--relocate-port`, and `--ollama-bin`.

### Known limitations

- Transparent mode's success path has been exercised by unit tests but not yet on a
  machine where Ollama runs as a managed service or tray app, which may
  restart Ollama automatically and reclaim the port. Transparent mode detects this
  and rolls back rather than leaving a broken state.

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

[Unreleased]: https://github.com/shivapreetham/resource-aware-model-proxy/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/shivapreetham/resource-aware-model-proxy/releases/tag/v0.2.1
[0.2.0]: https://github.com/shivapreetham/resource-aware-model-proxy/releases/tag/v0.2.0
[0.1.0]: https://github.com/shivapreetham/resource-aware-model-proxy/releases/tag/v0.1.0
