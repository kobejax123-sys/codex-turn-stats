#!/usr/bin/env python3
"""Stop hook: summarise how the turn that just finished performed.

Reads the hook payload on stdin, summarises the turn from the session
transcript, and prints a Claude-style hook result whose systemMessage the TUI
renders:

    148 tps · 965 out (71% reasoning) · CacheHit 90% · Tools 7 3.2s · $0.0592

Which segments appear is controlled by `turn_stats.json`, looked up in the
plugin's data directory first and next to this script second. Prints nothing
when no segment has anything to report, which the hook engine treats as a
clean no-op.
"""

import json
import os
import sys

# Items that stream model generation. Tool calls and approvals must not count.
#
# ContextCompaction is included because the compaction summary is model output: its
# tokens are counted in the response usage, so its generation time has to be in the
# denominator too, or turns that compact report a rate that is too high.
GEN_ITEM_TYPES = {"Reasoning", "AgentMessage", "Plan", "ContextCompaction"}

# Items for a tool the model invoked, rather than model output.
TOOL_ITEM_TYPES = {"CommandExecution", "McpToolCall", "FileChange", "Extension"}

# Items that do not stream arrive as a single chunk: their window spans a few
# milliseconds while the turn still reports a full token count. Below this the
# window carries no rate information, so the rate is left out.
MIN_WINDOW_MS = 500

# Shortest tool time worth showing; anything less renders as a misleading "0.0s".
MIN_TOOL_MS = 50

CONFIG_NAME = "turn_stats.json"

DEFAULT_SHOW = {
    "tps": True,
    "output_tokens": True,
    "reasoning_share": True,
    "cache_hit": True,
    "tools": True,
    "cost": True,
}

# Price multiplier applied when the turn ran on the Fast (priority) service tier.
# Fast buys roughly 2.5x the processing speed for 2x the money.
DEFAULT_FAST_MULTIPLIER = 2.0

# USD per million tokens, matching the model id as a substring. Prices move, so
# check https://openai.com/api/pricing/ before trusting these for real budgeting.
# Cached input bills at 10% of the input rate; cache writes at 1.25x.
MODEL_RATES = {
    "gpt-5.6-sol": {"input": 4.00, "cached_input": 0.40, "cache_write": 5.00, "output": 20.00},
    "gpt-5.6-terra": {"input": 2.00, "cached_input": 0.20, "cache_write": 2.50, "output": 12.00},
    "gpt-5.6-luna": {"input": 0.20, "cached_input": 0.02, "cache_write": 0.25, "output": 1.20},
    "gpt-6-astra": {"input": 10.00, "cached_input": 1.00, "cache_write": 12.50, "output": 50.00},
}

# A request whose input crosses this line is repriced as a whole: the input-side rates
# double and the output rate rises by half. The band depends on input tokens alone and
# is decided per request, so cost has to be accumulated request by request rather than
# from the turn's cumulative token counts.
LONG_CONTEXT_TOKENS = 272_000
LONG_CONTEXT_INPUT_MULTIPLIER = 2.0
LONG_CONTEXT_OUTPUT_MULTIPLIER = 1.5


def find_config():
    """Locate the config file, preferring the location that survives upgrades.

    A plugin is installed under a versioned directory that is replaced on every
    upgrade, so a config written next to this script would be lost. The hook
    engine exports the plugin's data directory, which is kept across upgrades.
    Falling back to the script's own directory keeps the same file working when
    it is dropped into `~/.codex/hooks/` by hand.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    roots = (os.environ.get("PLUGIN_DATA"), os.environ.get("CLAUDE_PLUGIN_DATA"), script_dir)
    for root in roots:
        if not root:
            continue
        candidate = os.path.join(root, CONFIG_NAME)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(script_dir, CONFIG_NAME)


def load_config(config_path):
    """Read the display switches and the Fast multiplier, with defaults."""
    config = {"show": dict(DEFAULT_SHOW), "fast_multiplier": DEFAULT_FAST_MULTIPLIER}
    try:
        with open(config_path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return config
    if not isinstance(raw, dict):
        return config
    for key in config["show"]:
        if isinstance(raw.get(key), bool):
            config["show"][key] = raw[key]
    multiplier = raw.get("fast_multiplier")
    if isinstance(multiplier, (int, float)) and not isinstance(multiplier, bool) and multiplier > 0:
        config["fast_multiplier"] = float(multiplier)
    return config


def duration_ms(item):
    """Milliseconds from an item's `duration` field, or None when it has none."""
    duration = item.get("duration")
    if not isinstance(duration, dict):
        return None
    secs = duration.get("secs") or 0
    nanos = duration.get("nanos") or 0
    return int(secs) * 1000 + int(nanos) // 1_000_000


def rates_for(model):
    """Price table entry for a model id, matching on any known name it contains."""
    if not model:
        return None
    lowered = model.lower()
    for name, rates in MODEL_RATES.items():
        if name in lowered:
            return rates
    return None


def request_cost_usd(usage, rates):
    """Estimated USD for a single model request."""
    inputs = usage.get("input_tokens") or 0
    cached = usage.get("cached_input_tokens") or 0
    cache_write = usage.get("cache_write_input_tokens") or 0
    output = usage.get("output_tokens") or 0
    long_context = inputs > LONG_CONTEXT_TOKENS
    input_multiplier = LONG_CONTEXT_INPUT_MULTIPLIER if long_context else 1.0
    output_multiplier = LONG_CONTEXT_OUTPUT_MULTIPLIER if long_context else 1.0
    # input_tokens includes the cached and cache-write portions, which bill at their own
    # rates; the long-context band scales all three input-side rates together.
    non_cached = max(inputs - cached - cache_write, 0)
    per_million = (
        non_cached * rates["input"] * input_multiplier
        + cached * rates["cached_input"] * input_multiplier
        + cache_write * rates["cache_write"] * input_multiplier
        + output * rates["output"] * output_multiplier
    )
    return per_million / 1_000_000


def cost_usd(summary, fast_multiplier):
    """Estimated USD for the turn, or None when the model has no known rates."""
    rates = rates_for(summary["model"])
    if rates is None:
        return None
    total = sum(request_cost_usd(usage, rates) for usage in summary["requests"])
    if summary["fast"]:
        total *= fast_multiplier
    return total


def measure(transcript_path, turn_id):
    """Summarise one turn: usage, generation window, tools, model and service tier."""
    summary = {
        "usage": {},
        # Per-request usages: the long-context band is decided per request, so cost is
        # accumulated from these rather than from the cumulative turn totals.
        "requests": [],
        "window_ms": 0,
        "tool_calls": 0,
        "tool_ms": 0,
        "model": None,
        "fast": False,
    }
    # thread_settings_applied carries no turn id, so the tier is snapshotted at the
    # last record that does belong to this turn.
    tier = None
    with open(transcript_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            entry_type = entry.get("type")

            if entry_type == "event_msg" and payload.get("type") == "thread_settings_applied":
                settings = payload.get("thread_settings") or {}
                tier = settings.get("service_tier")
                continue

            if entry_type == "turn_context" and payload.get("turn_id") == turn_id:
                summary["model"] = payload.get("model")
                continue

            if payload.get("turn_id") != turn_id:
                continue

            if tier is not None:
                summary["fast"] = tier == "priority"

            if entry_type == "token_usage_record":
                request_usage = payload.get("usage")
                if isinstance(request_usage, dict):
                    summary["requests"].append(request_usage)
                # turn_token_usage is cumulative, so keep the largest seen.
                turn_usage = payload.get("turn_token_usage") or {}
                if (turn_usage.get("output_tokens") or 0) >= (
                    summary["usage"].get("output_tokens") or 0
                ):
                    summary["usage"] = turn_usage
            elif entry_type == "event_msg" and payload.get("type") == "item_completed":
                item = payload.get("item") or {}
                item_type = item.get("type")
                started = payload.get("started_at_ms")
                completed = payload.get("completed_at_ms")
                span = (
                    max(completed - started, 0)
                    if isinstance(started, int) and isinstance(completed, int)
                    else 0
                )
                if item_type in GEN_ITEM_TYPES:
                    summary["window_ms"] += span
                elif item_type in TOOL_ITEM_TYPES:
                    summary["tool_calls"] += 1
                    summary["tool_ms"] += duration_ms(item) or span
    return summary


def format_usd(amount):
    if amount < 0.1:
        return f"${amount:.4f}"
    if amount < 1:
        return f"${amount:.3f}"
    return f"${amount:.2f}"


def format_line(summary, show, fast_multiplier):
    usage = summary["usage"]
    window_ms = summary["window_ms"]
    tokens = usage.get("output_tokens") or 0
    parts = []

    # The rate is the only segment that can be unmeasurable, so it alone is gated;
    # the remaining counts are exact and stay worth showing either way.
    if show["tps"] and window_ms >= MIN_WINDOW_MS:
        parts.append(f"{round(tokens * 1000 / window_ms)} tps")

    reasoning = usage.get("reasoning_output_tokens") or 0
    share_pct = round(reasoning * 100 / tokens) if tokens and reasoning else None

    if show["output_tokens"]:
        # With the token count shown, the share rides along as a parenthetical.
        head = f"{tokens} out"
        if show["reasoning_share"] and share_pct is not None:
            head += f" ({share_pct}% reasoning)"
        parts.append(head)
    elif show["reasoning_share"] and share_pct is not None:
        # Otherwise it stands alone, so its switch is never inert.
        parts.append(f"reasoning {share_pct}%")

    inputs = usage.get("input_tokens") or 0
    if show["cache_hit"] and inputs:
        cached = usage.get("cached_input_tokens") or 0
        parts.append(f"CacheHit {round(cached * 100 / inputs)}%")

    if show["tools"] and summary["tool_calls"]:
        # Some tool items carry no usable timing (e.g. FileChange spans a few ms), so
        # report the count on its own rather than a misleading "0.0s".
        if summary["tool_ms"] >= MIN_TOOL_MS:
            parts.append(f"Tools {summary['tool_calls']} {summary['tool_ms'] / 1000:.1f}s")
        else:
            parts.append(f"Tools {summary['tool_calls']}")

    if show["cost"]:
        amount = cost_usd(summary, fast_multiplier)
        if amount is not None:
            parts.append(format_usd(amount))

    return " · ".join(parts)


def main():
    config = load_config(find_config())

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    transcript_path = hook_input.get("transcript_path")
    turn_id = hook_input.get("turn_id")
    if not transcript_path or not turn_id:
        return
    try:
        summary = measure(transcript_path, turn_id)
    except OSError:
        return
    if (summary["usage"].get("output_tokens") or 0) <= 0:
        return

    line = format_line(summary, config["show"], config["fast_multiplier"])
    if not line:
        return
    json.dump({"systemMessage": line}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
