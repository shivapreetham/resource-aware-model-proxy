# RAMP — Resource-Aware Model Proxy

**Your local LLM should get out of the way when you need the RAM back.**

[![CI](https://github.com/shivapreetham/resource-aware-model-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/shivapreetham/resource-aware-model-proxy/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ramp-llm.svg)](https://pypi.org/project/ramp-llm/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

You know the afternoon. A decent model is loaded, you open a browser, and the
machine stops — frozen cursor, model server dead, the work you had open gone
with it. So you drop to a smaller model permanently, and spend every hour with
worse answers to prevent something that happens occasionally.

**RAMP removes that choice.** It watches RAM, VRAM and disk continuously and
moves your model down a ladder the moment memory gets tight — then back up when
it frees again.

```
memory gets tight   →   7B becomes 3B in ~6s      you keep working
memory comes back   →   7B returns after ~90s     you keep the good model
```

Your tools never find out. RAMP speaks the OpenAI API, so pointing them at it
is a one-line change — or no change at all, if you let it take the port your
model server already uses.

### It is more careful than it looks

Anything that moves models around under you has to earn trust, so:

- **It tells you what it costs.** ~2 s per swap, ~65 MB resident — and it
  reports its own footprint through `ramp status`, because *"the memory
  watchdog is the leak"* is a fair thing to suspect.
- **It won't thrash.** Separate thresholds for dropping and climbing, plus a
  rate limit, so it can't oscillate on a signal its own actions move.
- **It survives being killed.** If RAMP relocated your model server and is then
  hard-killed, `ramp restore` puts everything back. The arrangement is
  journalled to disk, not held in memory.
- **It refuses rather than guesses.** If it can't identify a runtime, or can't
  work out how to restart what it's about to stop, it declines and changes
  nothing.
- **Every number here was measured**, on a 16 GB laptop with an 8 GB card, and
  is reproducible with `ramp status` on yours.

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

**How greedy should it be?** By default RAMP keeps a slice of RAM free for the
rest of your machine. To use more of it:

```bash
ramp --aggressive     # leave only ~500 MB free, climb back in ~20s
ramp --profile safe   # the opposite: keep plenty free, move slowly
```

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

Here is the measurement that shaped the whole design. Loading Llama 3.1 on an
8 GB card, changing only the context length:

| Context | Free VRAM after load |
|---|---|
| default | 2,516 MiB |
| 32,768 | 1,018 MiB |
| **40,960** | **1,702 MiB** ← *went up?* |

More context, and the GPU reports **more** free memory. The KV cache had
outgrown the card — 5 GiB of cache against 4.9 GiB of weights — so the runtime
quietly moved layers into system RAM. RAM fell to 1.5 GB while VRAM *looked*
healthier.

> **The shortage doesn't stay put. It moves house, and nothing announces it.**

Which is why RAMP watches all three resources together, and why watching only
one is worse than useless:

- **Drops fast** under pressure — hesitating is what freezes machines.
- **Climbs back slowly**, only after sustained headroom, so it can't flap on a
  signal its own actions move.
- **Never mid-request**: in-flight generations finish; new ones wait ~2s.
- **Low disk blocks upgrades** rather than causing downgrades — a smaller model
  frees no disk, so disk gates rather than triggers.

**[docs/CONCEPTS.md](docs/CONCEPTS.md)** works through the mechanics: what
actually consumes memory when an LLM runs, the KV-cache arithmetic behind that
table, and the control theory that keeps the ladder from oscillating.

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
ruff check src tests
```

## License

MIT — see [LICENSE](LICENSE).
