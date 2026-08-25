# RAMP — context for Claude Code

RAMP (Resource-Aware Model Proxy) is an **elastic local LLM daemon**: one
OpenAI-compatible endpoint that continuously watches system resources and
transparently moves the loaded model up and down a quality ladder of "tiers"
(model size / quantization / context length).

Read [docs/CONCEPTS.md](docs/CONCEPTS.md) for the *why* behind the design —
memory mechanics, hysteresis, the swap lifecycle. This file is the operational
context: environment, commands, layout, and current state.

## Why this project exists

The local-LLM assistant space is saturated (Khoj, Open-LLM-VTuber, Letta,
Mem0). The gap this fills: **nothing ships a runtime that adapts to live
memory pressure.** Ollama/LM Studio decide once at load time; llama-swap swaps
on *client request*, never on system state; FlexQuant/Voltron/Any-Precision
validated the idea academically but shipped no usable daemon. RAMP is the
missing controller. It is intended for **open-source release**.

This replaced an earlier project (SoulSync, a local emotion-aware companion)
after research showed that space was already well covered. Legacy SoulSync
code lives in the parent directory and is **not** part of RAMP.

## Environment (important — this machine has quirks)

- **`python` is NOT on PATH.** There is no `py` launcher either. Python lives
  at `C:\Users\Shivapreetham\miniconda3\python.exe` (3.14.6).
- **Always use the project venv:** `.venv\Scripts\python.exe`.
- **Smart App Control is ENFORCED.** Unsigned executables are blocked with
  `OSError: [WinError 4551]`. Every llama.cpp release binary is refused, so
  **the `llama` backend cannot run on this machine** — do not spend time
  trying. Use the `ollama` backend (Ollama is signed and installed at
  `C:\Users\Shivapreetham\AppData\Local\Programs\Ollama\ollama.exe`).
- Hardware: 16 GB RAM, NVIDIA RTX 5060 Laptop (8 GB VRAM), `nvidia-smi`
  available. Useful for testing the VRAM code path for real.
- Shell is PowerShell; a Bash tool is also available. Prefer absolute paths.

## Commands

```bash
.venv\Scripts\python.exe -m pytest -q                              # all tests (36)
.venv\Scripts\python.exe -m ruff check src tests scripts           # lint (must be clean)
.venv\Scripts\python.exe -m pytest tests/test_policy.py -q         # policy units only
.venv\Scripts\python.exe -m ramp -c examples\ramp.mock.yaml        # demo, no models
.venv\Scripts\python.exe -m ramp -c examples\ramp.ollama.yaml      # real models
.venv\Scripts\python.exe scripts\stress_ram.py --mb 4000 --hold-s 60
```

While running: `curl http://127.0.0.1:8090/ramp/status` (mock, port 8090) or
`:8091` (ollama/vram example configs); `/ramp/metrics` for Prometheus text.

CI (`.github/workflows/ci.yml`) runs pytest on Linux/macOS/Windows × Python
3.10–3.13 plus `ruff check`. Keep both green.

## Layout

| Path | Role |
|---|---|
| `src/ramp/monitor.py` | Samples RAM (psutil), VRAM (`nvidia-smi`), disk. Raw + EMA readings. |
| `src/ramp/policy.py` | **Pure** state machine: sample → `stay`/`switch`/`unload`. No I/O — keep it that way, it's what makes the rules testable. |
| `src/ramp/controller.py` | Executes decisions: drain → stop → start → gate. Crash recovery, event log, pin/unpin. |
| `src/ramp/backend.py` | Child-process/model lifecycle: `LlamaServerBackend`, `OllamaBackend`, `MockBackend`. |
| `src/ramp/server.py` | FastAPI: `/v1/*` passthrough (SSE-safe) + `/ramp/*` control API. |
| `src/ramp/metrics.py` | Swap-rate / occupancy / request telemetry; Prometheus exposition. |
| `src/ramp/mock_llm.py` | Fake OpenAI server used by `MockBackend` — enables full E2E tests with no models. |
| `examples/*.yaml` | `ramp.mock.yaml` (demo), `ramp.ollama.yaml` (real), `ramp.yaml` (llama.cpp), `ramp.vram-test.yaml` (isolates VRAM pressure). |

## Design rules to preserve

1. **`policy.py` stays pure.** Decisions are a function of `(state, sample, now)`.
   No process handling, no HTTP, no clock reads beyond the passed-in `now`.
2. **Downgrade fast, upgrade slow.** The asymmetry is intentional: a late
   downgrade freezes the machine, a late upgrade costs nothing.
3. **Match each resource to the action that can fix it.** RAM/VRAM pressure
   → downgrade. Low disk → *gate upgrades only* (a smaller model can't free
   disk).
4. **Never make decisions mid-swap.** `_tick` returns early if
   `_swap_lock.locked()`. Removing this reintroduces a real bug where the
   transient stopped-backend state is misread as a crash and clobbers the
   swap (see CONCEPTS §6.1).
5. **Requests wait, they don't fail.** Swaps gate requests on an
   `asyncio.Event`, not an error response.
6. **Budgets are post-swap:** `available + current_tier_footprint`. Comparing
   against currently-free memory alone makes upgrades impossible.
7. **The cooldown (`min_swap_interval_s`) applies to upgrades only.** Critical
   pressure bypasses it. Never let it delay a downgrade.

## State of the work

**Verified live on this machine** (real Qwen2.5 1.5B/0.5B via Ollama):
- Startup tier selection, RAM-pressure downgrade, recovery upgrade.
- VRAM-pressure downgrade to a CPU-only tier while RAM stayed healthy
  (isolated with `examples/ramp.vram-test.yaml` + occupying the GPU with
  llama3.1).
- Disk gate blocking an upgrade (`last_decision: disk-low`).
- SSE streaming passthrough; pin/unpin.

**Measured swap costs** (Ollama, same machine): cold load 16.5 s; warm
unload+reload 1.9 s; full downgrade or upgrade 1.9 s; complete down-up cycle
3.6 s. Steady-state swaps are cheap because the OS page-caches the GGUF.

**Not verified live:** the `llama` backend (blocked by Smart App Control), the
disk gate's *release* path (integration-tested only — filling a 670 GB drive
wasn't practical), multi-GPU, non-NVIDIA GPUs, and **swap frequency over a
real multi-hour workload** (that's what `/ramp/metrics` now exists to answer —
run it for a day and read `swaps_per_hour`).

**Tests: 36, all passing; ruff clean.** Unit tests cover the policy
exhaustively; integration tests run the real daemon against mock child
processes with scripted resource readings.

## Roadmap

- Context-length scaling (shrink KV cache before swapping models — cheaper
  first response to pressure).
- Any-Precision backend ([paper](https://arxiv.org/abs/2402.10517)) to make
  downgrades near-free.
- OS pressure signals (Windows memory notifications / Linux PSI) instead of
  polling.
- Multi-GPU and non-NVIDIA GPU support.
- Split `ramp/` into its own git repo for the open-source release.

## Conventions

- Comments explain *why*, not *what*. The codebase uses module-level
  docstrings to explain each component's role — match that style.
- Tests: `pytest`, `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).
- When fixing a bug, **watch the regression test fail first.** A test written
  after a fix that passes without it proves nothing — this already happened
  once here.
