# Try RAMP

RAMP makes a local AI model **shrink when your computer runs low on memory,
and grow back when it frees up** — so your assistant never freezes your
machine, and you never have to choose between "good but heavy" and "light
but dumb."

There are two demos here:

- **[Demo 1](#demo-1--see-it-work-3-minutes)** — see it work. 3 minutes, nothing to download.
- **[Demo 2](#demo-2--the-real-thing-15-minutes)** — the real thing, with actual AI models.

Works on **Windows, macOS, and Linux**.

---

## First: install it

You need **Python 3.10 or newer**. Check with:

| Your system | Command |
|---|---|
| macOS / Linux | `python3 --version` |
| Windows | `py --version` |

If that errors, install Python from [python.org](https://www.python.org/downloads/)
(on Windows, tick **"Add Python to PATH"** during setup).

Then install RAMP:

| Your system | Command |
|---|---|
| macOS / Linux | `python3 -m pip install ramp-llm` |
| Windows | `py -m pip install ramp-llm` |

Check it worked:

```
ramp --version
```

<details>
<summary>If <code>ramp</code> isn't found</summary>

Python installed it somewhere that isn't on your PATH. Just put `python3 -m`
(or `py -m` on Windows) in front of every `ramp` command in this guide:

```
python3 -m ramp --version
```
</details>

---

## Demo 1 — See it work (3 minutes)

No AI models, no downloads. RAMP runs against three stand-in servers that
announce which "size" answered you, so you can watch it switch in real time.
The sizes are scaled to *your* machine, so this behaves the same on an 8 GB
laptop as on a 64 GB desktop.

### 1. Start it

```
ramp demo
```

It prints the ladder it built for your machine and then serves. **Leave it
running** and open a second terminal for everything below.

### 2. Ask it something

```
ramp ask hello
```

```
[big-model] you said: hello

answered by: big-model
```

Plenty of memory free, so you got the **big** one.

### 3. Now squeeze the memory

```
ramp stress
```

This fills up your RAM for 45 seconds and then releases it. Nothing
permanent — it's just a big empty list your computer reclaims instantly.

**While it's running**, in your second terminal:

```
ramp ask hello
```

```
[small-model] you said: hello

answered by: small-model
```

Nobody reconfigured anything. RAMP noticed the squeeze and stepped down.

### 4. Watch it recover

`ramp stress` releases the memory after 45 seconds. Wait about 20 seconds
more, then ask again — you're back to `[big-model]`.

### 5. See the whole story

```
ramp status
```

```
 * big-model     (1667 MB RAM)
   medium-model  (800 MB RAM)
   small-model   (333 MB RAM)

recent
   -            -> big-model     (startup)
   big-model    -> small-model   (memory-pressure)
   small-model  -> big-model     (headroom-recovered)
```

Stop the demo with `Ctrl+C`.

---

## Demo 2 — The real thing (15 minutes)

Same behaviour, real AI models. You'll download about **1.5 GB**.

### 1. Install Ollama

Get it from **[ollama.com/download](https://ollama.com/download)** — it's the
standard way to run AI models locally, on all three platforms.

### 2. Download two models of different sizes

```
ollama pull qwen2.5:1.5b
```

```
ollama pull qwen2.5:0.5b
```

The first is smarter, the second is lighter. RAMP will switch between them
based on what your machine can spare. (Add `ollama pull qwen2.5:3b` too if
you have plenty of RAM — more rungs, more interesting.)

### 3. Check your machine is ready

```
ramp doctor
```

Every line should say `OK`. If something's wrong, it tells you the exact
command to fix it. A `WARN` about llama.cpp is fine — you don't need it.

### 4. Start RAMP

```
ramp
```

No config file. It finds your models, measures your RAM and GPU, and builds
the ladder itself:

```
Auto-detected 2 tier(s) from Ollama (safety margin 2414 MB):
  1. qwen2.5:1.5b  (~1685 MB RAM, ~1425 MB VRAM)
  2. qwen2.5:0.5b  (~909 MB RAM, ~739 MB VRAM)

RAMP serving on http://127.0.0.1:8090/v1
```

### 5. Talk to it

```
ramp ask what is the capital of France
```

You get a real answer from a real model, and a line telling you which one
answered.

### 6. Squeeze it again

```
ramp stress --hold 60
```

Then, while that runs, ask something else. **A genuinely smaller AI model
answers you** — and when memory frees up, the bigger one comes back.

That's the whole idea: you keep the good model when you can afford it, and
you keep working when you can't.

### 7. Use it with your own tools

RAMP speaks the same API as OpenAI and Ollama, so anything you already use
works. Point it at:

```
http://localhost:8090/v1
```

Or don't change anything at all:

```
ramp run --transparent
```

RAMP steps in front of Ollama on its usual port, so every AI tool you have
routes through it untouched. It asks first, and puts everything back when it
exits.

---

## What you just watched

A normal setup loads one AI model and keeps it, no matter what else your
computer is doing. If it's too big, your machine crawls. If it's small
enough to always be safe, you get worse answers all day for a problem that
only happens occasionally.

RAMP checks your **RAM, GPU memory, and disk** every couple of seconds and
moves between models automatically. It drops fast when things get tight
(hesitating is what makes machines freeze) and climbs back slowly, only once
memory has been comfortably free for a while — otherwise it would flap up
and down forever. A switch takes about **2 seconds**, and RAMP itself uses
about **65 MB**.

## Finished?

Stop RAMP with `Ctrl+C`. To remove it completely:

```
pip uninstall ramp-llm
```

---

Code, docs, and the full story:
**https://github.com/shivapreetham/resource-aware-model-proxy**
