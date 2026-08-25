# Monitoring RAMP

An elastic daemon lives or dies on one number: **how often it actually
swaps.** A swap costs about 2 seconds warm and tens of seconds cold, so a
daemon that swaps a few times a day is invisible and one that swaps every
other minute is worse than no daemon at all. You cannot reason your way to
that number — tuning depends on your ladder, your machine, and what else you
run — so RAMP measures it.

## Two surfaces

**`GET /ramp/status`** — everything, as JSON, for eyeballing:

```bash
curl -s http://127.0.0.1:8090/ramp/status
```

```jsonc
{
  "tier": "qwen2.5-1.5b",
  "last_decision": "steady",        // why it's holding right now
  "memory": { "available_mb": 5805, ... },
  "gpu":    { "vram_free_mb": 6432, ... },
  "disk":   { "free_mb": 670312, "low": false },
  "metrics": {
    "uptime_s": 3600.0,
    "swaps_total": 4,
    "swaps_per_hour": 4.0,
    "swaps_by_reason": { "startup": 1, "memory-pressure": 2, "headroom-recovered": 1 },
    "swaps_suppressed": 3,          // times the cooldown blocked an upgrade
    "swap_time_percent": 0.21,      // share of wall clock spent swapping
    "mean_swap_s": 1.9,
    "tier_seconds": { "qwen2.5-1.5b": 3100.2, "qwen2.5-0.5b": 480.5 },
    "requests_total": 212,
    "requests_waited": 3            // requests that waited on a swap gate
  },
  "events": [ /* the last 100 transitions, with reasons */ ]
}
```

**`GET /ramp/metrics`** — Prometheus text format, for scraping:

```bash
curl -s http://127.0.0.1:8090/ramp/metrics
```

Ready-made configs live in [`examples/monitoring/`](../examples/monitoring/):

```bash
prometheus --config.file=examples/monitoring/prometheus.yml
```

## What to actually watch

### 1. Swap rate — the headline number

```promql
sum(rate(ramp_swaps_total[15m])) * 3600      # swaps per hour
```

Rough reading of the result:

| Swaps/hour | Verdict |
|---|---|
| < 2 | Healthy. RAMP is invisible, which is the goal. |
| 2–12 | Fine — it's tracking real changes in your workload. |
| > 12 | Churn. Tune it (below). |

### 2. Swap overhead — what churn costs you

```promql
rate(ramp_swap_seconds_total[30m])           # fraction of time spent swapping
```

This is a unitless ratio: `0.02` means 2% of wall-clock time is spent loading
models rather than serving. Above ~5%, the elasticity is costing more than
it's saving.

### 3. Occupancy — is your ladder calibrated?

```promql
rate(ramp_tier_seconds_total[1h])            # share of time on each tier
```

This is the most useful *diagnostic* series, because it tells you whether the
ladder you configured matches the machine you have:

- **Top tier ~100%** → your footprint estimates are too conservative, or you
  simply have enough RAM. Try a bigger top tier.
- **Bottom tier ~100%** → the machine can't sustain the upper tiers. Either
  the estimates are too optimistic, or the ladder needs a realistic top.
- **Roughly split** → the ladder is doing its job.

### 4. Suppressions — is the rate limiter load-bearing?

```promql
increase(ramp_swaps_suppressed_total[1h])
```

Each suppression is an upgrade the cooldown refused. A steady trickle means
hysteresis alone wasn't enough and `min_swap_interval_s` is actively saving
you from churn. Zero forever means you could lower it safely.

### 5. User-visible pain

```promql
increase(ramp_requests_rejected_total[1h])   # 503s: nothing was loaded
increase(ramp_requests_waited_total[1h])     # requests that waited on a swap
```

Rejections mean requests arrived while no tier could fit. If this is non-zero
in normal use, your smallest tier is too big for the machine's bad moments.

## Tuning from what you see

| Symptom | Likely fix |
|---|---|
| High swap rate | Raise `min_swap_interval_s` (hard floor between upgrades), or `upgrade_after_s`. |
| Swaps cluster around one boundary | Raise `upgrade_extra_mb` — tiers are too close, so the new tier immediately eats its own headroom. |
| Downgrades feel late; machine stutters first | Raise `safety_margin_mb`, or lower `downgrade_after_samples` to 1. |
| Stuck on the small tier with memory free | Check `last_decision`: `disk-low` means free disk, `cooldown` means wait, `upgrade-pending` means it's counting down. |
| Frequent 503s | Add a smaller bottom tier, or reduce its `est_ram_mb` if the estimate is inflated. |
| `swaps_failed` climbing | A model path is wrong, or a tier's footprint estimate is far below reality so it OOMs on load. |

## Calibrating tier footprints

`est_ram_mb` / `est_vram_mb` are *your* estimates, and every decision depends
on them. To measure rather than guess:

1. Pin the tier: `curl -X POST localhost:8090/ramp/pin/<tier>`
2. Read the real usage (Task Manager, `htop`, or `nvidia-smi`).
3. Round **up** — an underestimate is far more dangerous than an overestimate,
   because it lets RAMP load something that doesn't actually fit.
4. Release: `curl -X DELETE localhost:8090/ramp/pin`

## A note on what isn't measured

RAMP tracks its own behaviour, not model quality. It cannot tell you whether
the smaller tier's answers were good enough — only that it switched, when,
and why. If output quality matters for your use case, log the `x-ramp-tier`
response header alongside your own evaluations and judge the ladder on that.
