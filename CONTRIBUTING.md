# Contributing to RAMP

Thanks for taking a look. RAMP is small and deliberately so — the goal is a
daemon people can read in an afternoon and trust with their machine.

## Getting set up

```bash
git clone https://github.com/shivapreetham/resource-aware-model-proxy
cd ramp
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
pytest -q
```

You don't need any model files to develop or test: the `mock` backend spawns
fake OpenAI servers that tag their replies with the tier name, so the whole
daemon can be exercised end-to-end in seconds.

```bash
ramp demo
curl http://127.0.0.1:8090/ramp/status
ramp stress                                          # watch it downgrade
```

## Before you open a PR

```bash
ruff check src tests
pytest -q
```

CI runs both across Linux, macOS, and Windows on Python 3.10–3.13.

## Design rules worth knowing

These aren't style preferences; breaking them reintroduces bugs we've already
had.

1. **`policy.py` stays pure.** Decisions are a function of
   `(state, sample, now)` — no process handling, no HTTP, no reading the
   clock. That purity is what lets the whole decision matrix be tested with
   fabricated readings instead of a real machine under real pressure.
2. **Downgrade eagerly, upgrade reluctantly.** A late downgrade can freeze the
   machine or get the model OOM-killed; a late upgrade costs a few minutes of
   slightly worse output. Never make the two symmetric.
3. **Match each resource to the action that can fix it.** RAM and VRAM
   pressure justify downgrading. Low disk does not — a smaller model frees no
   disk — so disk gates upgrades instead.
4. **Never make decisions mid-swap.** `_tick` returns early while
   `_swap_lock` is held. Without it, the transient stopped-backend state gets
   misread as a crash and clobbers the swap in progress. See
   [docs/CONCEPTS.md](docs/CONCEPTS.md) §6.1.
5. **Requests wait, they don't fail.** Swaps gate requests on an
   `asyncio.Event`, not an error response.
6. **Budgets are post-swap:** `available + current_tier_footprint`. Comparing
   against currently-free memory alone makes upgrades impossible, because the
   memory needed is occupied by the model being replaced.

## Testing expectations

- New policy rules need unit tests in `tests/test_policy.py`. They're cheap —
  build a `ResourceSample` with the `S()` helper and assert on the `Decision`.
- New daemon behaviour needs an integration test driving the real controller
  against `MockBackend` with a scripted `FakeMonitor`.
- **When you fix a bug, watch the regression test fail first.** A test written
  after the fix that passes without it proves nothing. This has already caught
  us out once: the first test for the mid-swap race passed against the broken
  code because it wasn't waiting long enough.

## Things that would genuinely help

- **Context-length tiers.** The same model at a smaller context is a ladder
  rung that costs zero extra disk and no quality loss — likely the highest
  value feature RAMP doesn't have.
- **Multi-GPU and non-NVIDIA GPU monitoring** (ROCm, Apple unified memory).
- **OS pressure signals** — Linux PSI, Windows memory notifications — instead
  of polling.
- **Real-world swap-rate reports.** Run the daemon for a day and post your
  `/ramp/metrics` output in an issue. Whether the tuning defaults are right is
  an empirical question and more data settles it.

## Reporting issues

Include your config (redact model paths if you like), the output of
`GET /ramp/status`, and your OS/GPU. The `events` list and `last_decision`
field in that response usually explain what RAMP thought it was doing.
