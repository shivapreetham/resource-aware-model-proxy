# RAMP — Resource-Aware Model Proxy

**Your local LLM should get out of the way when you need the RAM back.**

[![CI](https://github.com/shivapreetham/resource-aware-model-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/shivapreetham/resource-aware-model-proxy/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ramp-llm.svg)](https://pypi.org/project/ramp-llm/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

RAMP is a daemon that sits in front of Ollama or llama.cpp and **swaps your
model for a smaller one when memory gets tight, then back when it frees up.**

- Open Chrome with 40 tabs → your 7B quietly becomes a 3B.
- Close them → a minute later the 7B is back.
- You never pick a model size again, and nothing gets OOM-killed.

It speaks the OpenAI API, so your existing tools work unchanged.

---

## Install

```bash
pip install ramp-llm
```

(`pipx install ramp-llm` to keep it isolated, or `uvx ramp-llm` to run it
without installing anything.)

```bash
ramp doctor    # can this machine run it? if not, what to fix
ramp           # start it
```

That's the setup. No config file — RAMP finds your models, measures your RAM
and GPU, and builds the ladder itself.

## Commands

RAMP runs in the background, so you inspect it with separate commands rather
than by watching a terminal.

| Command | What it does |
|---|---|
| **`ramp`** | Start the daemon in the background. Prints the URL and exits. |
| **`ramp stop`** | Stop it — cleanly, so transparent mode is undone properly. |
| **`ramp status`** | What's loaded, why, memory, swap count, overhead. |
| **`ramp watch`** | The same, live — repaints as the ladder moves. |
| **`ramp ask "hi"`** | Send one message and see which tier answered. |
| **`ramp doctor`** | Check the machine and say how to fix anything missing. |
| **`ramp demo`** | Watch it work with **no models downloaded**. |
| **`ramp stress`** | Fill memory so you can see it react. |
| `ramp init` | Write the auto-detected ladder to `ramp.yaml` to tune by hand. |
| `ramp restore` | Put your model server back if RAMP was killed mid-flight. |
| `ramp run` | Foreground instead of background (Docker, debugging). |

Add `-v` to any of them for more detail; every command has `--help`.

## Point your tools at it

Change one line:

```diff
- client = OpenAI(base_url="http://localhost:11434/v1")   # straight to Ollama
+ client = OpenAI(base_url="http://localhost:8090/v1")    # through RAMP
```

That's the whole integration — verified against the official `openai` Python
SDK, including streaming and model listing. Ollama's native `/api/*` routes
are proxied too, so tools written against Ollama work as well.

**Or change nothing at all:**

```bash
ramp --transparent
```

RAMP takes the port your model server already uses and moves that server one
port over, so *every* tool you have routes through it with no config. Works
with **Ollama, llama.cpp and LM Studio**. It asks first, proves the relocated
server healthy before touching the original, rolls back on any failure, and
puts everything back when it stops.

## Try it without any models

```bash
ramp demo        # terminal 1
ramp watch       # terminal 2 - leave this visible
ramp stress      # terminal 3 - fill memory and watch the tier drop
```

Nothing is downloaded. [DEMO.md](DEMO.md) walks through it, plus a second
walkthrough using real models.

## With real models

Install [Ollama](https://ollama.com), pull two models of different sizes, and
start:

```bash
ollama pull qwen2.5:1.5b
ollama pull qwen2.5:0.5b
ramp
```

RAMP builds the ladder from whatever you have. For llama.cpp instead, see
[examples/ramp.yaml](examples/ramp.yaml); to hand-tune anything, run
`ramp init` and edit the file it writes.

## How it works

RAMP watches **RAM, VRAM and free disk** every few seconds and moves between
models on its own:

- **Drops fast** when memory gets tight — hesitating is what freezes machines.
- **Climbs back slowly**, only once memory has been comfortably free for a
  while, so it can't flap up and down.
- **Never mid-request**: in-flight generations finish, new ones wait ~2s.
- **Low disk blocks upgrades** rather than causing downgrades, since a smaller
  model frees no disk.

A swap costs about **2 seconds** (measured), and RAMP itself uses about
**65 MB** — which it reports, so you can check rather than trust.

📖 **[docs/CONCEPTS.md](docs/CONCEPTS.md)** explains the mechanics properly:
what actually consumes memory when an LLM runs, why VRAM pressure silently
becomes RAM pressure, and the control theory behind the policy.

## Monitoring

An elastic daemon lives or dies on one number: **how often it actually swaps.**

```bash
ramp status                        # human readable
curl localhost:8090/ramp/metrics   # Prometheus
```

Under 2 swaps/hour means RAMP is invisible, which is the goal. Above ~12 means
churn worth tuning. [docs/MONITORING.md](docs/MONITORING.md) covers what to
watch and how to tune it; alert rules are in
[examples/monitoring/](examples/monitoring/).

<details>
<summary>Control API</summary>

| Endpoint | Purpose |
|---|---|
| `GET /ramp/status` | Everything: tier, resources, decisions, event log, metrics. |
| `GET /ramp/metrics` | Prometheus text format. |
| `GET /health` | Liveness, for orchestrators and Docker healthchecks. |
| `POST /ramp/pin/{tier}` | Force a tier; disables auto-calibration. |
| `DELETE /ramp/pin` | Resume auto-calibration. |
| `POST /ramp/shutdown` | Ask the daemon to exit cleanly. |

</details>

<details>
<summary>Docker</summary>

```bash
docker build -t ramp .
docker run --rm -p 8090:8090 \
  --add-host=host.docker.internal:host-gateway \
  -e RAMP_OLLAMA_URL=http://host.docker.internal:11434 \
  ramp
```

Add `--gpus all` for VRAM awareness. Inside a container RAMP reads the
*container's* memory limit, so `--memory` shapes its decisions.

</details>

## Why this doesn't already exist

Everyone running a local model has made the same bad trade: pick a big model
and let the machine choke, or pick a small one and pay for the worst case all
day. Existing tools make you choose **once, up front, forever**.

- **Ollama / LM Studio** pick a quantization once, at load time
  ([ollama#14674](https://github.com/ollama/ollama/issues/14674)).
- **[llama-swap](https://github.com/mostlygeek/llama-swap)** swaps on the
  *client's requested model*, never on system state.
- **FlexQuant / LSAQ / Voltron** proved elastic execution works academically;
  none shipped a usable daemon.

RAMP is the missing controller: policy-driven, damped against thrashing, and
measured — it reports its own swap rate so you can prove it isn't.

## Prior art & acknowledgements

RAMP didn't invent elastic inference; it productizes an idea others
established. Credit where it's due.

**Research** — [FlexQuant](https://arxiv.org/abs/2501.07139) (the closest
statement of this exact problem), [Any-Precision LLM](https://arxiv.org/abs/2402.10517)
(one weight file servable at several bit-widths — on the roadmap because of
it), [LSAQ](https://arxiv.org/abs/2412.18135) (memory budget as the primary
input), [Voltron](https://arxiv.org/abs/2607.07046) (scaling precision
mid-generation), [MoBiQuant](https://arxiv.org/abs/2602.20191), and
[PowerInfer](https://arxiv.org/abs/2312.12456) / [AirLLM](https://github.com/lyogavin/airllm)
for the complementary problem of *fitting* oversized models.

**Tools** — [llama-swap](https://github.com/mostlygeek/llama-swap) is the
direct inspiration for the shape of the solution: a transparent proxy in front
of local inference servers. RAMP differs in what triggers a swap, but that
architecture is its idea. [Ollama](https://ollama.com) and
[LM Studio](https://lmstudio.ai) proved local tooling has to be
zero-configuration to get adopted.

**Built on** [llama.cpp](https://github.com/ggml-org/llama.cpp), Ollama,
[psutil](https://github.com/giampaolo/psutil), [FastAPI](https://fastapi.tiangolo.com),
[httpx](https://www.python-httpx.org) and [uvicorn](https://www.uvicorn.org).
RAMP is a controller — it deliberately owns none of the hard parts of
inference.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). You need no model files to develop:
the mock backend runs the whole daemon end to end in seconds.

Real `/ramp/metrics` output from your own machine is especially welcome —
whether the tuning defaults are right is an empirical question, and more data
settles it.

```bash
pytest -q                        # 155 tests
ruff check src tests scripts
```

## License

MIT — see [LICENSE](LICENSE).
