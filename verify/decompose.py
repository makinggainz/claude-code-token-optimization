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

RATES = {
    "fable": (10.0, 50.0),
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

TRANSCRIPT_ROOT = os.path.expanduser("~/.claude/projects")


def tier_of(model):
    name = (model or "").lower()
    for tier in RATES:
        if tier in name:
            return tier
    return "other"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    split = datetime.strptime(sys.argv[1], "%Y-%m-%d")

    totals = collections.defaultdict(lambda: collections.defaultdict(float))
    turns = collections.Counter()
    sessions = collections.Counter()

    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "**", "*.jsonl"), recursive=True):
        modified = datetime.fromtimestamp(os.path.getmtime(path))
        period = "AFTER" if modified >= split else "BEFORE"
        sessions[period] += 1
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
                rate_in, rate_out = RATES.get(tier_of(message.get("model")), (0.0, 0.0))
                bucket = totals[period]
                bucket["cache_read"] += usage.get("cache_read_input_tokens", 0) * rate_in * CACHE_READ_MULTIPLIER / 1e6
                bucket["cache_write"] += usage.get("cache_creation_input_tokens", 0) * rate_in * CACHE_WRITE_MULTIPLIER / 1e6
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
                                 turns["BEFORE"] / sessions["BEFORE"], turns["AFTER"] / sessions["AFTER"]))
    print("%-22s%10.2f%10.2f" % ("cache read M per turn",
                                 totals["BEFORE"]["cache_read_tokens"] / turns["BEFORE"] / 1e6,
                                 totals["AFTER"]["cache_read_tokens"] / turns["AFTER"] / 1e6))
    share_before = 100 * per_unit["cache_read"]["BEFORE"] / grand["BEFORE"]
    share_after = 100 * per_unit["cache_read"]["AFTER"] / grand["AFTER"]
    print("%-22s%9.1f%%%9.1f%%" % ("cache read share of cost", share_before, share_after))


if __name__ == "__main__":
    main()
