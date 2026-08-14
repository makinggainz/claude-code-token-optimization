#!/usr/bin/env python3
"""Usage report for Claude Code, computed from local session transcripts.

Reads ~/.claude/projects/**/*.jsonl. Nothing is transmitted. No conversation
content is printed; output is aggregate counts only.

Usage:
    python3 usage_report.py                      # last 14 days
    python3 usage_report.py --days 30
    python3 usage_report.py --since 2026-08-03   # split into two periods

Cost-equivalent is an estimate produced by applying published API list rates to
observed token counts. It is a unit of comparison between periods. It is not a
bill, and it does not reflect subscription pricing.
"""
import argparse
import collections
import glob
import json
import os
from datetime import datetime, timedelta

# USD per million tokens (input, output). Update when list prices change.
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


def collect(cutoff):
    by_day = collections.defaultdict(lambda: collections.defaultdict(float))
    agents = collections.Counter()
    sessions = collections.defaultdict(set)

    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "**", "*.jsonl"), recursive=True):
        started, sid = session_meta(path)
        if started < cutoff:
            continue
        day = started.strftime("%Y-%m-%d")
        sessions[day].add(sid)
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
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" \
                            and block.get("name") in ("Agent", "Task"):
                        agents[(block.get("input") or {}).get("subagent_type") or "unspecified"] += 1
                usage = message.get("usage") or {}
                if not usage:
                    continue
                tier = tier_of(message.get("model"))
                rate_in, rate_out = RATES.get(tier, (0.0, 0.0))
                day_totals = by_day[day]
                day_totals[tier + "_in"] += usage.get("input_tokens", 0)
                day_totals[tier + "_out"] += usage.get("output_tokens", 0)
                day_totals[tier + "_cread"] += usage.get("cache_read_input_tokens", 0)
                day_totals[tier + "_cwrite"] += usage.get("cache_creation_input_tokens", 0)
                day_totals["cost"] += (
                    usage.get("input_tokens", 0) * rate_in
                    + usage.get("cache_read_input_tokens", 0) * rate_in * CACHE_READ_MULTIPLIER
                    + usage.get("cache_creation_input_tokens", 0) * rate_in * CACHE_WRITE_MULTIPLIER
                    + usage.get("output_tokens", 0) * rate_out
                ) / 1e6
    return by_day, agents, sessions


def summarize(title, days, by_day, sessions):
    totals = collections.defaultdict(float)
    for day in days:
        for key, value in by_day[day].items():
            totals[key] += value

    cache_read = sum(v for k, v in totals.items() if k.endswith("_cread"))
    cache_write = sum(v for k, v in totals.items() if k.endswith("_cwrite"))
    fresh_input = sum(v for k, v in totals.items() if k.endswith("_in"))
    output = sum(v for k, v in totals.items() if k.endswith("_out"))
    billed_input = cache_read + cache_write + fresh_input

    print("\n=== %s ===" % title)
    distinct = set().union(*(sessions[d] for d in days)) if days else set()
    print("sessions: %d   days: %d" % (len(distinct), len(days)))
    print("fresh input: %8.2fM   cache read: %9.2fM   cache write: %7.2fM   output: %6.2fM"
          % (fresh_input / 1e6, cache_read / 1e6, cache_write / 1e6, output / 1e6))
    if billed_input:
        print("cache hit rate: %.1f%%" % (100 * cache_read / billed_input))
    print("tier mix:")
    for tier in list(RATES) + ["other"]:
        subtotal = sum(v for k, v in totals.items() if k.startswith(tier + "_"))
        if subtotal:
            print("   %-7s %8.2fM  %5.1f%%"
                  % (tier, subtotal / 1e6, 100 * subtotal / (billed_input + output)))
    print("cost-equivalent: %.2f  (%.2f per day)" % (totals["cost"], totals["cost"] / max(len(days), 1)))
    if output:
        print("cost-equivalent per million output tokens: %.1f" % (totals["cost"] / (output / 1e6)))
    return totals, output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--since", help="YYYY-MM-DD. Splits the data into before and on/after.")
    args = parser.parse_args()

    cutoff = datetime.now() - timedelta(days=3650 if args.since else args.days)
    by_day, agents, sessions = collect(cutoff)
    if not by_day:
        print("No transcripts found in the window.")
        return

    all_days = sorted(by_day)
    if args.since:
        before = [d for d in all_days if d < args.since]
        after = [d for d in all_days if d >= args.since]
        results = {}
        if before:
            results["before"] = summarize("BEFORE " + args.since, before, by_day, sessions)
        if after:
            results["after"] = summarize("ON/AFTER " + args.since, after, by_day, sessions)
        if "before" in results and "after" in results:
            (before_totals, before_out) = results["before"]
            (after_totals, after_out) = results["after"]
            if before_out and after_out:
                before_unit = before_totals["cost"] / (before_out / 1e6)
                after_unit = after_totals["cost"] / (after_out / 1e6)
                print("\ncost-equivalent per unit of output: %.1f -> %.1f (%+.1f%%)"
                      % (before_unit, after_unit, (after_unit / before_unit - 1) * 100))
            print("Workload composition differs between periods. Treat as a signal, not proof.")
    else:
        summarize("LAST %d DAYS" % args.days, all_days, by_day, sessions)

    if agents:
        print("\nsubagent dispatches by type:")
        for name, count in agents.most_common():
            print("   %-22s %d" % (name, count))

    print("\nNot measured: output quality.")


if __name__ == "__main__":
    main()
