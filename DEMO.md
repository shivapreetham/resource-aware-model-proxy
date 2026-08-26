# See RAMP working in 5 minutes

RAMP makes a local AI model **shrink when your computer runs low on memory,
and grow back when it frees up** — so your assistant never freezes your
machine, and you never have to pick between "good but heavy" and "light but
dumb."

This demo shows that happening live. **No AI models to download** — it uses
stand-in servers that announce which "size" answered you, so you can watch
RAMP switch between them in real time.

**You need:** Python 3.10 or newer. That's it.

---

## Step 1 — Install

```
pip install ramp-llm
```

## Step 2 — Save this as `demo.yaml`

Three pretend models: big, medium, small. RAMP will pick between them.

```yaml
backend: mock
listen: { port: 8090 }
poll_interval_s: 2
safety_margin_mb: 1500
hysteresis:
  downgrade_after_samples: 2
  upgrade_after_s: 15
  upgrade_extra_mb: 256
  critical_free_mb: 400
tiers:
  - name: big-model
    est_ram_mb: 1500
    mock_ballast_mb: 60
  - name: medium-model
    est_ram_mb: 600
    mock_ballast_mb: 30
  - name: small-model
    est_ram_mb: 200
    mock_ballast_mb: 10
```

## Step 3 — Start it

```
ramp run -c demo.yaml
```

Leave this running. **Open a second terminal** for everything below.

## Step 4 — Ask it something

```
curl http://127.0.0.1:8090/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
```

The reply tells you which model answered:

```
[big-model] you said: hi
```

Plenty of memory free, so you got the **big** one. Also try:

```
ramp status
```

## Step 5 — Now squeeze the memory

Save this as `hog.py` — it fills up your RAM for 45 seconds, then lets go.
(Nothing permanent; it's just a big empty list.)

```python
import psutil, time
chunks = []
while psutil.virtual_memory().available / 1024**2 > 1000:
    b = bytearray(256 * 1024**2)
    for i in range(0, len(b), 4096):
        b[i] = 1
    chunks.append(b)
    if len(chunks) > 40:
        break
print(f"holding {len(chunks)*256} MB for 45s...")
end = time.time() + 45
while time.time() < end:
    for b in chunks:                       # keep the pages hot
        for i in range(0, len(b), 1024*1024):
            b[i] = 1
    print(f"  free: {psutil.virtual_memory().available/1024**2:.0f} MB")
    time.sleep(2)
```

Run it:

```
python hog.py
```

While it's running, ask again from your second terminal (Step 4 command).
**The answer now comes from a smaller model:**

```
[small-model] you said: hi
```

Nobody reconfigured anything. RAMP noticed the squeeze and stepped down.

## Step 6 — Let it recover

`hog.py` releases the memory after 45 seconds. Wait ~20 more seconds, ask
again, and you're back to:

```
[big-model] you said: hi
```

## Step 7 — See the whole story

```
ramp status
```

The bottom shows exactly what it did and why:

```
big-model    -> medium-model  (memory-pressure)
medium-model -> small-model   (memory-pressure)
small-model  -> big-model     (headroom-recovered)
```

---

## What you just watched

A normal AI setup loads one model and keeps it, whatever else your computer
is doing. If it's too big, your machine crawls. If it's small enough to
always be safe, you get worse answers all day for a problem that only
happens occasionally.

RAMP watches your **RAM, GPU memory, and disk** a few times a second and
moves between models automatically. It drops fast when things get tight
(waiting is what makes machines freeze) and climbs back slowly, only once
memory has been comfortably free for a while — otherwise it would flap up
and down forever. Swapping takes about **2 seconds**, and RAMP itself uses
around **65 MB**.

## Using it for real

With [Ollama](https://ollama.com) installed and a couple of models pulled:

```
ollama pull qwen2.5:1.5b
ollama pull qwen2.5:0.5b
ramp doctor      # checks your machine, tells you how to fix anything missing
ramp             # finds your models and builds the ladder itself
```

Then point any AI tool at `http://localhost:8090/v1` instead of its usual
address — that's the whole integration. It works with anything that speaks
the OpenAI API (most things do).

Or don't change anything at all:

```
ramp run --transparent
```

RAMP steps in front of Ollama on its own port, so every tool you already
have routes through it untouched. It asks first, and puts everything back
when it exits.

## Done?

Stop RAMP with `Ctrl+C`, then delete `demo.yaml` and `hog.py`. To remove it
completely: `pip uninstall ramp-llm`.

---

Code, docs, and the full story: **https://github.com/shivapreetham/resource-aware-model-proxy**
