# RAMP Concepts

This document explains *what is actually going on* inside RAMP — the memory
mechanics of running an LLM locally, why "just pick a model" isn't good
enough, and the control-theory ideas the daemon is built on.

It assumes you can read code but not that you know LLM serving internals.
All the numbers come from real measurements on the development machine:
16 GB RAM, NVIDIA RTX 5060 Laptop (8 GB VRAM), Qwen2.5 1.5B/0.5B and
Llama 3.1 8B.

---

## 1. The problem

A local LLM is not like a normal program. A normal program uses a few hundred
MB and you never think about it. An LLM claims **gigabytes, all at once, for
as long as it's loaded** — and your laptop is also running Chrome, VS Code,
Docker, and Spotify.

That creates a dilemma every local-LLM user hits:

- Pick a **big model** → great answers, until you open a few browser tabs and
  the machine starts swapping, or the model gets OOM-killed.
- Pick a **small model** → always stable, but you're permanently paying for a
  worst case that only happens ten minutes a day.

Existing tools make you choose once, up front, forever. **RAMP's premise is
that this choice should be continuous and automatic** — big model when the
machine is idle, small model when it's busy, decided every few seconds
without you noticing.

To build that, you first have to understand exactly *what* is consuming
memory, and *where* it lives.

---

## 2. What consumes memory when an LLM runs

Three things, in decreasing order of obviousness.

### 2.1 Weights — the model itself

A model is a pile of numbers (parameters). Memory needed is simply:

```
bytes ≈ number_of_parameters × bytes_per_parameter
```

A 1.5-billion-parameter model at full 16-bit precision is
`1.5e9 × 2 bytes = 3 GB`. That's a lot for a small model, which is why nobody
runs raw FP16 locally.

### 2.2 Quantization — making weights smaller

**Quantization stores each weight in fewer bits.** Instead of 16 bits per
weight, use 8, or 4, or even 2. The model gets dramatically smaller and
faster, and gets slightly worse at its job.

| Format | Bits/weight | 1.5B model | Quality |
|---|---|---|---|
| FP16 | 16 | ~3.0 GB | reference |
| Q8_0 | 8 | ~1.6 GB | nearly identical |
| **Q4_K_M** | ~4.8 | **~1.1 GB** | mild loss, the common default |
| Q2_K | ~2.6 | ~0.6 GB | noticeably degraded |

The naming (`Q4_K_M`) decodes as: **Q4** = about 4 bits per weight, **K** =
"K-quant", a smarter scheme that stores per-block scaling factors instead of
one scale for the whole tensor, **M** = "medium" variant, meaning the
sensitive tensors (attention value projections, some feed-forward layers) are
kept at higher precision while the rest go to 4 bits. That's why the *effective*
bits-per-weight is ~4.8 rather than exactly 4.

This matters to RAMP because **quantization is one axis of the quality ladder**:
the same model at Q4 and Q2 are two different tiers with different footprints.

> Measured: `qwen2.5-1.5b-instruct-q4_k_m.gguf` is 1,117,320,736 bytes (1.04 GiB),
> and the 0.5B is 491,400,032 bytes (469 MiB).

### 2.3 The KV cache — the sneaky one

This is the part that surprises people, and it's the reason RAMP watches
memory *continuously* instead of just at load time.

When a transformer generates text, it computes a **key** and **value** vector
for every token, in every layer. To avoid recomputing them for the entire
conversation on every new token, they're cached. That cache is the **KV cache**,
and it **grows linearly with context length**:

```
kv_bytes_per_token = 2 (K and V) × n_layers × n_kv_heads × head_dim × bytes_per_element
```

For **Qwen2.5-1.5B** (28 layers, 2 KV heads, head_dim 128, FP16):

```
2 × 28 × 2 × 128 × 2 = 28,672 bytes ≈ 28 KiB per token
```

At a 4,096-token context that's **~117 MiB** — small. But for
**Llama 3.1 8B** (32 layers, 8 KV heads, head_dim 128):

```
2 × 32 × 8 × 128 × 2 = 131,072 bytes = 128 KiB per token
```

At 40,960 tokens that's **~5 GiB of KV cache — more than the model weights
themselves.**

**This is not theoretical.** During testing I loaded Llama 3.1 with three
different context settings and watched free VRAM on the same 8 GB GPU:

| Context | Free VRAM after load |
|---|---|
| default | 2,516 MiB |
| 32,768 | 1,018 MiB |
| 40,960 | 1,702 MiB *(see below)* |

The last row is the interesting one, and it teaches the single most important
lesson in this document.

### 2.4 The lesson: pressure moves between resources

At 40,960 tokens the KV cache no longer fit in VRAM — so the runtime
**offloaded layers to the CPU**, which *freed* VRAM (1,702 MiB, up from 1,018)
while system RAM dropped to 1,509 MB.

**VRAM pressure silently became RAM pressure.**

This is why RAMP's first live pressure test downgraded with reason
`memory-pressure` rather than `vram-pressure` — the GPU shortage had already
been converted into a RAM shortage by the runtime before RAMP ever saw it.

Any monitor watching only one resource would draw the wrong conclusion about
what's happening. That's the concrete justification for holding **RAM, VRAM,
and disk** accountable together.

---

## 3. Where memory lives: VRAM, RAM, disk

### VRAM (GPU memory)
Fast, and small — 8 GB on this machine. This is where you *want* the model,
because GPU inference is many times faster than CPU. When people say a model
"fits on the GPU", they mean weights + KV cache + overhead all fit in VRAM.

### RAM (system memory)
Bigger (16 GB here), slower, and shared with every other program you run.
Two ways an LLM uses it:
1. **CPU inference** — the whole model runs here (slow but always possible).
2. **Overflow from the GPU** — see below.

### Offloading — the dial between them
Runtimes split a model **by layer**. llama.cpp's `-ngl N` (and Ollama's
`num_gpu`) means "put N layers on the GPU, the rest on the CPU." So a model
isn't simply "on GPU" or "on CPU" — it's a *ratio*, and the runtime adjusts
that ratio automatically when things don't fit. That's exactly the mechanism
behind the surprise in §2.4.

**Consequence for RAMP:** a tier declares both `est_ram_mb` and `est_vram_mb`,
because most tiers occupy both. A tier with `est_vram_mb: 0` is a declared
CPU-only fallback — it can never be blocked by GPU pressure, which makes it
the safe landing spot when the GPU is full.

### Disk
Two roles:
1. **Storage** — GGUF files live here and are read at load time. This is why
   swapping tiers costs seconds, not milliseconds.
2. **The safety net** — when RAM runs out, the OS pages memory to disk. This
   is catastrophic for LLM performance (disk is orders of magnitude slower
   than RAM), so RAMP's whole job is to *avoid* ever getting there.

**Why RAMP treats disk differently:** loading a *smaller* model does not free
disk space. So low disk cannot be fixed by downgrading — it can only be a
reason not to make things worse. Disk therefore **gates upgrades** rather than
triggering downgrades. Matching each resource to the action that can actually
fix it is a core design idea.

> A note on `mmap`: llama.cpp memory-maps GGUF files, so weights are paged in
> lazily and the OS can evict them under pressure. This makes "how much RAM is
> this model using" genuinely fuzzy — another reason RAMP uses configured
> *estimates* (`est_ram_mb`) rather than trying to measure a process's usage.

---

## 4. Why elasticity is hard

If memory pressure could be handled by simply "using less", this project
wouldn't exist. Three hard constraints shape the entire design:

**1. You cannot resize a loaded model.**
There's no "shrink to 60%" call. Changing size means: unload the old model,
load the new one. This is why RAMP's unit of action is a *swap*, not an
adjustment.

**2. Swaps cost seconds — but far fewer than you'd think.**
A *cold* load reads gigabytes from disk: 16.5 s measured for the 1.5B tier.
But the OS keeps recently-read model files in its page cache, so in steady
state a swap is much cheaper. Measured on the same machine:

| Operation | Time |
|---|---|
| Cold load (1.5B) | 16.47 s |
| Unload + reload, same model (warm) | 1.88 s |
| Full downgrade (unload 1.5B → load 0.5B) | 1.87 s |
| Full upgrade (unload 0.5B → load 1.5B) | 1.87 s |
| Complete down-then-up cycle | ~3.6 s |

So the honest cost of elasticity in steady state is **about two seconds per
swap**, not twenty. Even at the worst rate the hysteresis permits — a full
cycle every two minutes — that is roughly 3% overhead.

There is a pleasing accident here. When pressure is severe enough that the OS
evicts a cached model, it's the *upgrade* that becomes slow again, never the
downgrade: the small model being loaded under pressure is small and likely
still cached. **The cost asymmetry lines up with the urgency asymmetry** — the
direction that matters when the machine is struggling stays fast.

Note also that swaps are *sequential*: the old backend is stopped before the
new one starts, so peak memory never exceeds the larger of the two tiers.
There is no moment where both models are resident.

**3. You cannot swap mid-generation.**
A response being streamed token-by-token is stateful; you can't hand it to a
different model halfway. So swaps happen *between* requests, and in-flight
work must be drained first.

Research systems like [Voltron](https://arxiv.org/abs/2607.07046) do scale
precision mid-generation, and [Any-Precision LLM](https://arxiv.org/abs/2402.10517)
stores one weight file servable at several bit-widths (which would make swaps
nearly free). Neither ships as a usable runtime today — which is why RAMP
takes the pragmatic route and optimizes *when* to swap instead.

---

## 5. The control loop

This is RAMP's core, and it's a classic control-systems problem: observe a
noisy signal, decide, act, avoid oscillation.

```
     ┌─────────────┐   every N seconds
     │   Monitor   │  RAM / VRAM / disk
     └──────┬──────┘
            │ ResourceSample
     ┌──────▼──────┐
     │   Policy    │  pure function: state + sample → decision
     └──────┬──────┘
            │ stay | switch(tier) | unload
     ┌──────▼──────┐
     │ Controller  │  drains, swaps processes, gates requests
     └─────────────┘
```

The **policy is a pure function** with no I/O — that's a deliberate design
choice, because it makes every rule below exhaustively unit-testable with
fabricated readings instead of requiring a real machine under real pressure.

### 5.1 Safety margins

RAMP never fills memory to the brim. A tier "fits" only if:

```
tier_footprint + safety_margin ≤ available
```

The margin (`safety_margin_mb`, default 1536 MB) is headroom reserved for the
*rest of your computer*. Without it, RAMP would happily consume every last
byte and make the machine unusable while technically satisfying its own rules.

### 5.2 Raw vs. smoothed readings

Memory readings are noisy — a single sample might catch a transient spike
from a compile job that ends a second later. RAMP keeps two views:

- **Raw** — the instantaneous reading. Used for **downgrades**, where being
  fast matters more than being sure.
- **EMA** (exponential moving average) — a smoothed reading, computed as
  `ema = α × raw + (1 − α) × previous_ema`. Used for **upgrades**, where a
  brief dip shouldn't cancel a legitimate recovery.

α (`ema_alpha`, default 0.4) controls reactivity: higher follows the raw
signal more closely, lower smooths harder.

### 5.3 Hysteresis — the thrashing problem

Here's the failure mode a naive implementation hits.

Suppose the rule is just "downgrade below the margin, upgrade above it." Free
memory hovers right at the threshold. RAMP downgrades. Unloading the big model
frees memory — which puts it *above* the threshold. So RAMP upgrades. Loading
the big model consumes memory — back below the threshold. Downgrade. Upgrade.
Downgrade.

Each cycle costs seconds of loading and makes the assistant unusable. **The
system oscillates because its own actions change the signal it's reacting to.**

The fix is **hysteresis**: make the thresholds for going up and going down
deliberately different, so the system has to travel a distance before
reversing. RAMP applies it three ways:

| Mechanism | Setting | Purpose |
|---|---|---|
| Consecutive breaches | `downgrade_after_samples` (2) | Ignore single-sample spikes |
| Sustained recovery | `upgrade_after_s` (120) | Memory must stay free for minutes |
| Extra headroom | `upgrade_extra_mb` (1024) | Upgrade only with room to spare |
| Cooldown | `min_swap_interval_s` (60) | Hard floor between upgrades |

The third one is the subtle and important one. If a bigger tier were loaded
the instant it *barely* fits, it would immediately consume the very headroom
that justified loading it, breach the margin, and be downgraded again. So
upgrading requires *more* free memory than merely fitting.

The fourth is a backstop. Hysteresis is a *heuristic* — it damps oscillation
but doesn't bound it. The cooldown is a hard rate limit: no upgrade may
happen within `min_swap_interval_s` of the last switch, whatever the readings
say. Note that it applies to **upgrades only** — delaying a downgrade risks an
OOM, while delaying an upgrade costs nothing. The critical floor bypasses it
entirely. Every suppression is counted in `/ramp/metrics`, so you can see
whether the limiter is actually load-bearing on your machine.

### 5.4 Deliberate asymmetry

Downgrades and upgrades are treated completely differently, because their
costs are asymmetric:

- **Downgrading late** → the machine swaps, freezes, or the model is
  OOM-killed. Severe.
- **Upgrading late** → you use a slightly worse model for a few more minutes.
  Trivial.

So: **downgrade eagerly, upgrade reluctantly.** Roughly 6 seconds of pressure
triggers a downgrade; 2 minutes of calm are required for an upgrade.

The extreme case is the **critical floor** (`critical_free_mb`): below it,
RAMP skips hysteresis entirely and acts on a single sample. If nothing fits,
it unloads everything — serving 503s beats taking the whole machine down.

### 5.5 Budgets are computed post-swap

A subtlety worth stating: when deciding what to switch *to*, RAMP doesn't
compare against currently-free memory. It compares against what would be free
**after unloading the current tier**:

```
budget = currently_free + current_tier_footprint
```

Without this, RAMP could never upgrade — the memory it needs is, by
definition, currently occupied by the model it's replacing.

---

## 6. The swap lifecycle

Executing a decision is a small state machine with a request gate:

```
1. Close the gate      new requests start waiting (they don't fail)
2. Drain               wait for in-flight generations (up to drain_timeout_s)
3. Stop old backend    terminate the process / unload the model
4. Start new backend   spawn and poll /health until it responds
5. Open the gate       queued requests proceed against the new tier
```

Two ideas here are worth internalizing.

**Waiting beats failing.** During a swap there is genuinely no model to answer
with. RAMP holds requests on an `asyncio.Event` for up to `queue_timeout_s`
rather than returning errors, so a client sees a slow response instead of a
broken one.

**Fall-through on failure.** If the target tier fails to start (bad path, not
enough memory after all), RAMP tries progressively smaller tiers rather than
giving up. Degraded service beats no service.

### 6.1 A real concurrency bug worth understanding

This one was found by running the real system, and it illustrates why
transitional states are dangerous.

The controller has crash recovery: if the backend process dies unexpectedly,
restart it. Naively:

```python
if self.current is not None and not self.backend.alive():
    await self._activate(self.current, reason="backend-crash")
```

The bug: **mid-swap, this condition is temporarily true for innocent reasons.**
Step 3 stops the old backend (`alive()` → False) while `self.current` still
names the *outgoing* tier. A poll tick landing in that window concludes the
backend crashed and "recovers" by reloading the old tier — silently undoing
the swap in progress. In testing, this reverted a manual pin two seconds
after it was applied.

The fix is to recognize that **a transition in progress is not a state to make
decisions about**:

```python
if self._swap_lock.locked():
    return   # a swap is underway; its transient state means nothing
```

The general lesson: when a system observes itself, it must distinguish
"broken" from "mid-change." Any check that reads shared state has to account
for the windows in which that state is deliberately inconsistent.

There's a second lesson from how this was verified. The first regression test
for it *passed without the fix applied* — it wasn't waiting long enough for
the spurious recovery to complete, so it proved nothing. **A regression test
you haven't watched fail is not a regression test.** Always break the fix and
confirm the test catches it.

---

## 7. Why a proxy?

RAMP could have been a library you import. Making it an **HTTP proxy** is what
makes it useful, for one reason: **the OpenAI API is the lingua franca of local
LLM tooling.**

Open-LLM-VTuber, Khoj, SillyTavern, LangChain, Continue — all of them accept a
`base_url`. Point that at RAMP instead of Ollama and they gain elastic memory
management with **zero code changes**. RAMP is invisible infrastructure.

This drives three implementation details:

**Transparent passthrough.** RAMP forwards `/v1/*` verbatim and streams
responses back chunk-by-chunk, so Server-Sent Events (token streaming) work
untouched. It deliberately does *not* parse or transform model output.

**A visible side-channel.** Every response carries an `x-ramp-tier` header
naming the tier that answered, and `/ramp/status` exposes full state including
`last_decision` (*why* RAMP is currently holding, e.g. `disk-low`). Dumb
clients ignore this; smart clients can adapt — deferring a heavy task while
degraded, for instance.

**Model-name rewriting.** Clients ask for a virtual model (`"auto"`). Backends
that route by name (Ollama) need the real tag, so RAMP rewrites the `model`
field in the request body to the active tier's model. Clients never learn or
care what's physically loaded.

---

## 8. Backends

RAMP separates *deciding* from *executing*. Three executors exist:

| Backend | What it does | When to use |
|---|---|---|
| `llama` | Spawns `llama-server` per tier with a GGUF path and `-c` context size | Most control; requires llama.cpp installed |
| `ollama` | Loads/unloads Ollama models via `keep_alive`, proxies to its OpenAI API | Already installed; **signed**, so it works where OS policy blocks unsigned binaries |
| `mock` | Fake OpenAI servers that tag replies with their tier name | Tests and demos, no downloads |

The `mock` backend is not a toy — it's what makes the integration tests
possible. The full daemon can be exercised end-to-end, with real child
processes and real HTTP, in seconds and with no model files.

> **Windows note:** this machine has **Smart App Control** enforced, which
> blocks unsigned executables. Every llama.cpp release binary is refused with
> `WinError 4551`. That's why the `ollama` backend exists and why it's the
> verified path here.

---

## 9. Glossary

| Term | Meaning |
|---|---|
| **GGUF** | The file format llama.cpp/Ollama use for quantized models |
| **Quantization** | Storing weights in fewer bits (Q4, Q8) to trade quality for size/speed |
| **KV cache** | Cached attention keys/values; grows linearly with context length |
| **Context (ctx)** | Max tokens the model can attend to; the main driver of KV cache size |
| **Offloading** | Splitting model layers between GPU and CPU (`-ngl` / `num_gpu`) |
| **Tier** | One rung of RAMP's quality ladder: a model + context + footprint estimates |
| **Hysteresis** | Using different up/down thresholds to prevent oscillation |
| **EMA** | Exponential moving average; smooths a noisy signal |
| **Thrashing** | Rapid useless back-and-forth switching |
| **Drain** | Waiting for in-flight requests to finish before acting |
| **Gate** | The mechanism that makes requests wait during a swap instead of failing |

---

## 10. Further reading

The idea RAMP implements is well-validated in research; what's missing is a
usable daemon.

- [FlexQuant](https://arxiv.org/abs/2501.07139) — elastic quantization for edge
  devices with fluctuating memory. The closest academic statement of this problem.
- [Any-Precision LLM](https://arxiv.org/abs/2402.10517) — one weight file
  servable at 3/4/…/n bits. Would make RAMP's swaps nearly free.
- [LSAQ](https://arxiv.org/abs/2412.18135) — layer-specific quantization chosen
  per memory budget.
- [Voltron](https://arxiv.org/abs/2607.07046) — scales precision *during*
  generation by watching KV-cache growth.
- [PowerInfer](https://arxiv.org/abs/2312.12456) — hot/cold neuron offloading to
  fit oversized models on consumer GPUs.

Adjacent tools: [llama-swap](https://github.com/mostlygeek/llama-swap) (swaps on
client request, not system state) and Ollama/LM Studio (one-time detection at
load; see [ollama#14674](https://github.com/ollama/ollama/issues/14674)).
