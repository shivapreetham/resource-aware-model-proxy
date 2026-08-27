# Launch kit

Copy-paste material for going public. Keep the claims here in sync with what
is actually measured — every number below was verified on the dev machine and
is reproducible with `ramp status`.

---

## GitHub repo settings

**Description** (350 char limit; this is what shows in search):

> Your local LLM should get out of the way when you need the RAM back. RAMP is a daemon that watches RAM, VRAM and disk, and swaps your model for a smaller one under memory pressure — then back when it frees up. OpenAI-compatible, so existing tools work unchanged.

**Website:** `https://pypi.org/project/ramp-llm/`

**Topics** (GitHub allows 20; these are the ones people actually search):

```
llm  local-llm  ollama  llama-cpp  lm-studio  openai-api  proxy
memory-management  inference  quantization  gguf  self-hosted
python  daemon  local-first  ai-tools  llmops  edge-ai
```

**Release notes for v0.7.0** — link the changelog; GitHub auto-generates the
commit list, and the changelog explains the *why*.

---

## r/LocalLLaMA

The audience that actually has this problem. Lead with the pain, not the tool.

**Title:**

> My PC hung, Ollama crashed, and I lost an afternoon to an OOM. So I made the model resize itself instead.

**Body:**

> I wasn't trying to build this. I was building an assistant on a local model
> when my machine just stopped — OOM, cursor frozen, Ollama dead, session gone.
> If you run models locally you've had that afternoon.
>
> The usual fix is to drop to a smaller model permanently: you pay for a worse
> assistant every hour to prevent something that happens occasionally. That
> felt like the wrong trade, because it's a one-time decision about a
> constraint that changes minute to minute.
>
> So I built a small daemon that watches RAM, VRAM and disk and moves between
> models automatically. Open Chrome with 40 tabs → your 7B becomes a 3B. Close
> them → the 7B comes back. It speaks the OpenAI API, so you change one line
> (`base_url`) and every tool you already use keeps working. It also proxies
> Ollama's native `/api/*`, and it can take Ollama's port outright so you don't
> even change that.
>
> Some numbers, all measured rather than guessed:
> - a swap costs **1.9s** warm (16.5s cold — the OS page-caches the weights)
> - the daemon itself is **65 MB**, and it reports its own footprint so you can
>   check rather than trust me
> - it drops a tier in ~6s under pressure, and waits ~90s of sustained headroom
>   before climbing back, so it can't flap
>
> The thing that surprised me while building it: I pushed Llama 3.1's context
> to 40k and **free VRAM went up**. The KV cache no longer fit on the card, so
> the runtime silently spilled layers to system RAM. VRAM pressure became RAM
> pressure with no announcement — which is exactly why watching one resource
> isn't enough.
>
> Try it without downloading anything:
> ```
> pip install ramp-llm
> ramp demo      # stand-in servers announce which "size" answered
> ramp stress    # fill memory, watch it step down
> ```
>
> MIT, and honest about what isn't proven: llama.cpp and LM Studio paths are
> unit-tested but I don't have either installed, so Ollama is the only runtime
> I've verified end to end. Would genuinely like reports from people who do.
>
> https://github.com/shivapreetham/resource-aware-model-proxy

**Also worth doing:** a short comment on
[ollama#14674](https://github.com/ollama/ollama/issues/14674) — those people
asked for exactly this, in their own words.

---

## Hacker News

**Title:** `Show HN: RAMP – a local LLM that shrinks when you need the RAM back`

Keep the first comment factual: what it does, the two measured numbers, and
the honest gaps. HN rewards the caveats more than the pitch.

---

## LinkedIn

Different audience — recruiters and peers, not users. Tell the story, not the
feature list. Put the link in the first comment; the algorithm buries posts
with outbound links.

> Last week my laptop stopped responding. Out of memory. Cursor frozen, the
> model server dead, the work I had open gone with it.
>
> I wasn't building anything to do with memory — I was building an assistant on
> a local AI model. The machine simply couldn't hold both that and everything
> else I had open.
>
> The standard fix is to permanently downgrade to a smaller model: you accept a
> worse assistant every hour of every day to prevent something that happens
> occasionally. That struck me as the wrong trade, because it's a one-time
> decision about a constraint that changes minute to minute.
>
> So I spent a week making the choice unnecessary instead.
>
> While measuring where the cliff actually was, I found something that didn't
> make sense: I gave the model a *bigger* context window and my GPU reported
> *more* free memory. The cache had outgrown the card, so the runtime quietly
> moved work to system RAM. The shortage hadn't gone away — it had moved house,
> silently.
>
> That's the whole problem in one measurement. Tools that watch a single number
> will always draw the wrong conclusion.
>
> So RAMP watches RAM, GPU memory and disk together, and resizes the model
> instead of freezing the machine. A swap takes about two seconds. The daemon
> itself costs 65 MB — and reports that number, because "the memory watchdog is
> the leak" is a fair thing to suspect.
>
> The part I did not expect: not one of the serious bugs was caught by its 166
> tests. Every single one came from running it — a race that reverted my own
> changes two seconds later, a crash that left my model server dead, and a
> memory calculation so cautious it silently refused to ever use my best model.
>
> Shipping something teaches you things reading about it cannot.
>
> It's open source and on PyPI. Link in the comments.

---

## Where the blog lives

`docs/index.html` is a standalone copy of the post. Turn it into a live page:

**Settings → Pages → Source: "Deploy from a branch" → Branch: `main`, folder
`/docs` → Save.**

It appears at `https://shivapreetham.github.io/resource-aware-model-proxy/`
within a minute or two. `docs/.nojekyll` is there so GitHub serves the file
as written instead of running it through Jekyll.

Own URL, no paywall, no ads, and it sits beside the code - which is what you
want when the post *is* the argument for the project. Add the same URL to the
repo's "Website" field.

Cross-post afterwards to [dev.to](https://dev.to) with a canonical link back
to that URL, so the traffic aggregates rather than splitting. Avoid Medium:
its interstitials and paywall prompts cost credibility on HN specifically.

## Sequencing

1. **Push and check CI is green.** Three of my releases went out red before the
   release workflow was gated on tests.
2. **Run it for a day first.** `ramp status` prints a real swaps-per-hour
   figure — the one number the README still lacks, and the first thing anyone
   will ask for.
3. r/LocalLLaMA → the Ollama issue → blog → HN → LinkedIn last.
4. Have a GIF ready: `ramp watch` beside `ramp stress`, showing the tier drop
   and recover. That clip is the entire pitch.
