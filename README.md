# codex-turn-stats

English | [简体中文](./README.zh-CN.md)

A Codex plugin that prints one line of stats after every agent turn: how fast
the model generated, how many tokens it produced, how much of the prompt hit
the cache, how long tools took, and roughly what the turn cost.

```text
↳ Hook · 148 tps · 965 out (71% reasoning) · CacheHit 90% · Tools 7 3.2s · $0.0592
```

Every segment is optional — see [Configuration](#configuration).

## Features

- One line per turn, emitted when the agent stops
- Six segments, each independently switchable
- Cost accumulated per request, so long-context repricing is handled correctly
- Distributed through the plugin marketplace, leaving your `~/.codex/hooks.json`
  untouched

## Requirements

- Codex with plugin support (the `plugins` feature is on by default)
- `python3` on `PATH`

## Quick start

### Install

```sh
codex plugin marketplace add kobejax123-sys/codex-turn-stats
codex plugin add turn-stats@codex-turn-stats
```

`marketplace add` also accepts a full URL (`https://github.com/kobejax123-sys/codex-turn-stats`)
or a local path, and takes `--ref <branch|tag>` if you want to pin a revision.

### Trust the hook

Plugin hooks never run until you approve them. The first time you start Codex
after installing, a screen appears:

```text
Hooks need review
1 hook is new or changed.
Hooks can run outside the sandbox after you trust them.

  1. Review hooks
  2. Trust all and continue
  3. Continue without trusting
```

Choose **Trust all and continue**. Pick *Continue without trusting* and the
plugin stays installed but silent.

This approval writes a `trusted_hash` into `[hooks.state]` in your config. The
hash covers the hook *declaration* in `hooks/hooks.json`, not the Python file —
so edits to `hooks.json` (or a plugin upgrade that changes it) will ask again,
while the script itself can change freely.

## What each segment means

| Segment | Meaning |
|---|---|
| `148 tps` | Output tokens divided by the time the model spent generating them |
| `965 out` | Output tokens for the turn, summed across requests |
| `(71% reasoning)` | Share of output tokens that were reasoning |
| `CacheHit 90%` | Cached input tokens as a share of total input tokens |
| `Tools 7 3.2s` | Tool calls made in the turn, and total time inside them |
| `$0.0592` | Estimated API cost for the turn |

## Configuration

Every switch defaults to `true`, so with no config file at all every segment is
shown. To turn segments off, copy this repo's [`turn_stats.json`](./turn_stats.json)
into the plugin's data directory and edit it:

```sh
mkdir -p "$CODEX_HOME/plugins/data/turn-stats-codex-turn-stats"
cp turn_stats.json "$CODEX_HOME/plugins/data/turn-stats-codex-turn-stats/"
```

`$CODEX_HOME` is usually `~/.codex`. The directory is not created for you — the
`mkdir` above handles that. This location survives plugin upgrades, unlike the
plugin's own directory, which is replaced wholesale on every version bump. So
edit the copy, never a file inside the plugin.

```json
{
  "tps": true,
  "output_tokens": true,
  "reasoning_share": true,
  "cache_hit": true,
  "tools": true,
  "cost": true,
  "fast_multiplier": 2.0
}
```

| Switch | Controls | Example |
|---|---|---|
| `tps` | Generation speed | `148 tps` |
| `output_tokens` | Output token count | `965 out` |
| `reasoning_share` | Share of output tokens spent reasoning | `(71% reasoning)` |
| `cache_hit` | Prompt-cache hit rate | `CacheHit 90%` |
| `tools` | Tool call count and time | `Tools 7 3.2s` |
| `cost` | Estimated API cost | `$0.0592` |
| `fast_multiplier` | Cost multiplier on the Fast service tier | `2.0` |

Notes:

- `tps` is dropped when the turn's generation window is under 500 ms, because a
  window that short carries no rate information.
- `reasoning_share` renders on its own as `reasoning 71%` when `output_tokens`
  is off, so the switch is never inert.
- `fast_multiplier` scales the cost when the turn ran on the Fast service tier.

## How the numbers are computed

The hook fires once per turn, on `Stop`, and reads the session transcript that
Codex passes it. Nothing is measured speculatively: the timings and token counts
all come from what the transcript already recorded.

- **`tps`** — output tokens over the span from each generation item's start to
  its completion. Tool calls and approval waits are excluded, so this is model
  speed rather than wall-clock speed. If a turn contained no streamed
  generation, the rate is omitted rather than reported as zero.
- **`CacheHit`** — `cached_input_tokens / input_tokens` for the turn.
- **`cost`** — summed per request, because the long-context band is decided per
  request rather than per turn. A request whose input exceeds 272,000 tokens is
  repriced as a whole: input-side rates double and the output rate rises by
  half. A turn that crosses the threshold mid-way is billed partly at each rate,
  which accumulating the turn totals could not express.

Prices per million tokens, matched against the model id:

| Model | Input | Cached input | Cache write | Output |
|---|---|---|---|---|
| `gpt-5.6-sol` | $4.00 | $0.40 | $5.00 | $20.00 |
| `gpt-5.6-terra` | $2.00 | $0.20 | $2.50 | $12.00 |
| `gpt-5.6-luna` | $0.20 | $0.02 | $0.25 | $1.20 |
| `gpt-6-astra` | $10.00 | $1.00 | $12.50 | $50.00 |

**These are estimates, not billed amounts.** They come from third-party
listings rather than a first-party feed, they drift as prices change, and they
ignore any discount, credit or enterprise agreement on your account. Edit
`MODEL_RATES` in `hooks/turn_stats.py` to match your own rates. A model with no
matching entry reports no cost at all rather than guessing.

## Known limitations

- **The line disappears when you reopen the session.** Hook output is
  deliberately transient — Codex classifies `HookCompleted` as a non-durable
  event, so it is never written to the rollout and cannot be replayed. It stays
  in scrollback for the current session only.
- **A turn with no streamed generation reports no rate.** Items that arrive as a
  single chunk have a window of a few milliseconds, which says nothing about
  speed.
- **A `CacheHit` on the very first request is real.** It means the static
  prefix — system prompt plus tool definitions — was already warm on the
  backend. Cache measurement is block-based, so the figure is quantised.

## Uninstall

```sh
codex plugin remove turn-stats --marketplace codex-turn-stats
codex plugin marketplace remove codex-turn-stats
```

The `trusted_hash` entry left in `[hooks.state]` is inert once the plugin is
gone, and can be deleted from your config.

## License

MIT
