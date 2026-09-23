#!/usr/bin/env python3
"""Cost per turn, adjusted for how deep in a session those turns sat.

Two problems with normalizing cost by output tokens, which is what
decompose.py does:

1. The denominator is not independent of the change being measured. A setup
   that instructs shorter write-ups, or that shifts work from generating code
   to running tools, reduces output tokens for the same work. Cost per output
   token then rises even when nothing became less efficient.

2. Cache read at turn N is largely a function of N, because every turn re-reads
   the transcript so far. If the mix of turn depths differs between periods,
   the average moves for reasons unrelated to efficiency.

This script uses the turn as the unit of work instead, and controls for the
second problem by direct standardization: it computes cost per turn inside
matched depth bands, then re-weights the later period to the earlier period's
depth mix. Subagent cost is included in the numerator and attributed to the
main-thread turn it was dispatched from, so delegation is charged for.

A turn is not a perfect unit of work either. Report both this and decompose.py.

Reads ~/.claude/projects/**/*.jsonl. Nothing is transmitted. No conversation
content is printed.

Usage:
    python3 per_turn.py 2026-08-03
"""
import collections
import glob
import json
import os
import sys
from datetime import datetime

# USD per million tokens (input, output, cache read), matched by model id
# prefix, most specific first. Cache read is priced per model because it is not
# a fixed multiple of input: Fable 5.1 reads at 0.025x and Opus 5.5 at 0.05x.
# Update when list prices change.
RATES = [
    ("claude-fable-5-1", (10.0, 50.0, 0.25)),
    ("claude-fable", (10.0, 50.0, 1.0)),
    ("claude-opus-5-5", (4.0, 20.0, 0.20)),
    ("claude-opus", (5.0, 25.0, 0.50)),
    ("claude-sonnet-5", (2.0, 10.0, 0.20)),
    ("claude-sonnet", (3.0, 15.0, 0.30)),
    ("claude-haiku", (1.0, 5.0, 0.10)),
]
TIERS = ("fable", "opus", "sonnet", "haiku")
# Cache writes, as multiples of the input rate. Claude Code writes mostly with
# the 1-hour TTL. Records without the TTL breakdown are priced as 5-minute.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0

TRANSCRIPT_ROOT = os.path.expanduser("~/.claude/projects")
BANDS = [(1, 25), (26, 50), (51, 100), (101, 200), (201, 400), (401, 800),
         (801, 1600), (1601, 10 ** 9)]
MIN_TURNS_PER_BAND = 30


def rates_of(model):
    """(input, output, cache read) in USD per million tokens."""
    name = (model or "").lower()
    for prefix, rates in RATES:
        if name.startswith(prefix):
            return rates
    return 0.0, 0.0, 0.0


def cache_write_cost(usage, rate_in):
    """USD for one message's cache writes, split by TTL where the record has it."""
    total = usage.get("cache_creation_input_tokens", 0)
    long_ttl = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0)
    return ((total - long_ttl) * CACHE_WRITE_5M_MULTIPLIER
            + long_ttl * CACHE_WRITE_1H_MULTIPLIER) * rate_in / 1e6


def session_meta(path):
    """(start time, session id). See decompose.py for why mtime is not used."""
    started = sid = None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if sid is None:
                    sid = record.get("sessionId")
                if started is None and record.get("timestamp"):
                    try:
                        started = (datetime
                                   .fromisoformat(record["timestamp"].replace("Z", "+00:00"))
                                   .astimezone().replace(tzinfo=None))
                    except ValueError:
                        pass
                if started and sid:
                    break
    except OSError:
        pass
    if started is None:
        started = datetime.fromtimestamp(os.path.getmtime(path))
    return started, sid or path


def band_of(depth):
    for index, (low, high) in enumerate(BANDS):
        if low <= depth <= high:
            return index
    return len(BANDS) - 1


def cost_of(usage, model):
    rate_in, rate_out, rate_read = rates_of(model)
    return ((usage.get("input_tokens", 0) * rate_in
             + usage.get("cache_read_input_tokens", 0) * rate_read
             + usage.get("output_tokens", 0) * rate_out) / 1e6
            + cache_write_cost(usage, rate_in))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    split = datetime.strptime(sys.argv[1], "%Y-%m-%d")

    sessions = collections.defaultdict(list)
    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "**", "*.jsonl"), recursive=True):
        started, sid = session_meta(path)
        sessions[sid].append((started, path))

    cost = collections.defaultdict(lambda: collections.defaultdict(float))
    output = collections.defaultdict(lambda: collections.defaultdict(float))
    turns = collections.defaultdict(collections.Counter)

    for parts in sessions.values():
        parts.sort()
        period = "AFTER" if parts[0][0] >= split else "BEFORE"
        depth = 0
        for _, path in parts:
            try:
                handle = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    message = record.get("message") or {}
                    usage = message.get("usage") or {}
                    if not usage:
                        continue
                    if not record.get("isSidechain"):
                        depth += 1
                        turns[period][band_of(depth)] += 1
                    index = band_of(max(depth, 1))
                    cost[period][index] += cost_of(usage, message.get("model"))
                    output[period][index] += usage.get("output_tokens", 0)

    bands = [i for i in range(len(BANDS))
             if turns["BEFORE"][i] >= MIN_TURNS_PER_BAND
             and turns["AFTER"][i] >= MIN_TURNS_PER_BAND]
    if not bands:
        print("Not enough turns on both sides of the split date.")
        sys.exit(1)

    before_turns = sum(turns["BEFORE"][i] for i in bands)
    after_turns = sum(turns["AFTER"][i] for i in bands)

    print("COST-EQUIVALENT PER MAIN-THREAD TURN, BY DEPTH IN SESSION\n")
    print("%-14s%12s%12s%10s%14s" % ("turn depth", "BEFORE", "AFTER", "change", "turns b/a"))
    for i in bands:
        low, high = BANDS[i]
        rate_b = cost["BEFORE"][i] / turns["BEFORE"][i]
        rate_a = cost["AFTER"][i] / turns["AFTER"][i]
        label = "%d-%d" % (low, high) if high < 10 ** 9 else "%d+" % low
        print("%-14s%12.4f%12.4f%10s%14s"
              % (label, rate_b, rate_a, "%+.0f%%" % ((rate_a / rate_b - 1) * 100),
                 "%d/%d" % (turns["BEFORE"][i], turns["AFTER"][i])))

    crude_b = sum(cost["BEFORE"][i] for i in bands) / before_turns
    crude_a = sum(cost["AFTER"][i] for i in bands) / after_turns
    adjusted = sum((cost["AFTER"][i] / turns["AFTER"][i]) * (turns["BEFORE"][i] / before_turns)
                   for i in bands)

    print("\n%-46s%12.4f" % ("cost per turn, baseline", crude_b))
    print("%-46s%12.4f  %+.1f%%" % ("cost per turn, after", crude_a,
                                    (crude_a / crude_b - 1) * 100))
    print("%-46s%12.4f  %+.1f%%" % ("cost per turn, adjusted to baseline depth mix",
                                    adjusted, (adjusted / crude_b - 1) * 100))

    out_b = sum(output["BEFORE"][i] for i in bands) / before_turns
    out_a = sum(output["AFTER"][i] for i in bands) / after_turns
    print("\n%-46s%12.0f%12.0f  %+.0f%%"
          % ("output tokens per turn", out_b, out_a, (out_a / out_b - 1) * 100))
    print("If this fell, cost per output token overstates cost: the same work")
    print("produced fewer tokens to divide by. Compare against decompose.py.")

    print("\nshare of turns by depth band")
    for i in bands:
        low, high = BANDS[i]
        label = "%d-%d" % (low, high) if high < 10 ** 9 else "%d+" % low
        print("   %-12s%9.1f%%%9.1f%%" % (label, 100 * turns["BEFORE"][i] / before_turns,
                                          100 * turns["AFTER"][i] / after_turns))


if __name__ == "__main__":
    main()
