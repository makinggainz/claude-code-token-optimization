#!/usr/bin/env python3
"""Cost decomposition per unit of work, split across two periods.

Normalizes cost by output tokens so that periods with different workload volume
can be compared. Separates the cost into four components, which is what makes it
possible to distinguish an effect produced by model tiering from an effect
produced by a change in session length.

Reads ~/.claude/projects/**/*.jsonl. Nothing is transmitted. No conversation
content is printed.

Usage:
    python3 decompose.py 2026-08-03
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
    """(start time, session id) for a transcript file.

    Two corrections live here. File mtime is not the session start: resuming an
    old session updates its mtime, which sorts long-lived sessions into the
    later period and manufactures growth that did not occur. And a transcript
    file is not a session: one session writes several files, including one per
    subagent run, so counting files understates session length by a factor that
    itself varies between periods. Both are bucketed from the records instead.
    """
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


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    split = datetime.strptime(sys.argv[1], "%Y-%m-%d")

    totals = collections.defaultdict(lambda: collections.defaultdict(float))
    turns = collections.Counter()
    sessions = collections.defaultdict(set)

    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "**", "*.jsonl"), recursive=True):
        started, sid = session_meta(path)
        period = "AFTER" if started >= split else "BEFORE"
        sessions[period].add(sid)
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
                turns[period] += 1
                rate_in, rate_out, rate_read = rates_of(message.get("model"))
                bucket = totals[period]
                bucket["cache_read"] += usage.get("cache_read_input_tokens", 0) * rate_read / 1e6
                bucket["cache_write"] += cache_write_cost(usage, rate_in)
                bucket["output"] += usage.get("output_tokens", 0) * rate_out / 1e6
                bucket["fresh_input"] += usage.get("input_tokens", 0) * rate_in / 1e6
                bucket["output_tokens"] += usage.get("output_tokens", 0)
                bucket["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)

    if not totals.get("BEFORE") or not totals.get("AFTER"):
        print("Need transcripts on both sides of the split date.")
        sys.exit(1)

    print("COST-EQUIVALENT PER MILLION OUTPUT TOKENS (normalized by work produced)\n")
    print("%-14s%10s%10s%10s" % ("component", "BEFORE", "AFTER", "change"))
    grand = {"BEFORE": 0.0, "AFTER": 0.0}
    per_unit = {}
    for component in ("cache_read", "cache_write", "output", "fresh_input"):
        row = {}
        for period in ("BEFORE", "AFTER"):
            million_out = totals[period]["output_tokens"] / 1e6
            row[period] = totals[period][component] / million_out if million_out else 0.0
            grand[period] += row[period]
        per_unit[component] = row
        change = "%+.0f%%" % ((row["AFTER"] / row["BEFORE"] - 1) * 100) if row["BEFORE"] else "-"
        print("%-14s%10.1f%10.1f%10s" % (component, row["BEFORE"], row["AFTER"], change))
    print("%-14s%10.1f%10.1f%10s"
          % ("TOTAL", grand["BEFORE"], grand["AFTER"],
             "%+.1f%%" % ((grand["AFTER"] / grand["BEFORE"] - 1) * 100)))

    counterfactual = grand["AFTER"] - per_unit["cache_read"]["AFTER"] + per_unit["cache_read"]["BEFORE"]
    print("\nCounterfactual: holding cache_read per unit at its BEFORE value gives %.1f (%+.1f%%)."
          % (counterfactual, (counterfactual / grand["BEFORE"] - 1) * 100))
    print("That is a model, not a measurement. It assumes session length would have been unchanged.")

    print("\nWORKLOAD SHAPE")
    print("%-22s%10s%10s" % ("", "BEFORE", "AFTER"))
    print("%-22s%10.1f%10.1f" % ("turns per session",
                                 turns["BEFORE"] / len(sessions["BEFORE"]), turns["AFTER"] / len(sessions["AFTER"])))
    print("%-22s%10.2f%10.2f" % ("cache read M per turn",
                                 totals["BEFORE"]["cache_read_tokens"] / turns["BEFORE"] / 1e6,
                                 totals["AFTER"]["cache_read_tokens"] / turns["AFTER"] / 1e6))
    share_before = 100 * per_unit["cache_read"]["BEFORE"] / grand["BEFORE"]
    share_after = 100 * per_unit["cache_read"]["AFTER"] / grand["AFTER"]
    print("%-22s%9.1f%%%9.1f%%" % ("cache read share of cost", share_before, share_after))


if __name__ == "__main__":
    main()
