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

RATES = {
    "fable": (10.0, 50.0),
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

TRANSCRIPT_ROOT = os.path.expanduser("~/.claude/projects")
BANDS = [(1, 25), (26, 50), (51, 100), (101, 200), (201, 400), (401, 800),
         (801, 1600), (1601, 10 ** 9)]
MIN_TURNS_PER_BAND = 30


def tier_of(model):
    name = (model or "").lower()
    for tier in RATES:
        if tier in name:
            return tier
    return "other"


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
    rate_in, rate_out = RATES.get(tier_of(model), (0.0, 0.0))
    return (usage.get("input_tokens", 0) * rate_in
            + usage.get("cache_read_input_tokens", 0) * rate_in * CACHE_READ_MULTIPLIER
            + usage.get("cache_creation_input_tokens", 0) * rate_in * CACHE_WRITE_MULTIPLIER
            + usage.get("output_tokens", 0) * rate_out) / 1e6


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
