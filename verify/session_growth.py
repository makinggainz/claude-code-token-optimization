#!/usr/bin/env python3
"""Per-session context growth for Claude Code, computed from local transcripts.

Session length is a cost driver that model selection does not touch. Every turn
re-reads the transcript accumulated so far, so cache read cost compounds with
turn count. This report shows where that cost concentrates, per session, so the
sessions that grew past their task are visible.

Reads ~/.claude/projects/**/*.jsonl. Nothing is transmitted. No conversation
content is printed; output is aggregate numbers and truncated session ids.

Usage:
    python3 session_growth.py             # last 14 days
    python3 session_growth.py --days 30
    python3 session_growth.py --top 15    # rows in the largest-sessions table

Peak context approximates context occupancy at the session's largest request:
fresh input plus cache read plus cache write tokens on a single call. Subagent
transcripts are separate files and are counted as sessions of their own, which
matches the other scripts here. Cost-equivalent applies published API list
rates to token counts; it is a comparison unit, not a bill.
"""
import argparse
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

MIN_TURNS = 3  # ignore trivial sessions


def tier_of(model):
    name = (model or "").lower()
    for tier in RATES:
        if tier in name:
            return tier
    return "other"


def percentile(sorted_values, q):
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1))))
    return sorted_values[index]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    cutoff = datetime.now() - timedelta(days=args.days)
    sessions = []
    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "**", "*.jsonl"), recursive=True):
        if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
            continue
        turns = 0
        cost = cread_cost = 0.0
        cread_tokens = peak = 0
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
                turns += 1
                rate_in, rate_out = RATES.get(tier_of(message.get("model")), (0.0, 0.0))
                cread = usage.get("cache_read_input_tokens", 0)
                cwrite = usage.get("cache_creation_input_tokens", 0)
                fresh = usage.get("input_tokens", 0)
                cread_tokens += cread
                cread_cost += cread * rate_in * CACHE_READ_MULTIPLIER / 1e6
                cost += (fresh * rate_in
                         + cread * rate_in * CACHE_READ_MULTIPLIER
                         + cwrite * rate_in * CACHE_WRITE_MULTIPLIER
                         + usage.get("output_tokens", 0) * rate_out) / 1e6
                peak = max(peak, fresh + cread + cwrite)
        if turns < MIN_TURNS:
            continue
        sessions.append({
            "id": os.path.basename(path)[:8],
            "turns": turns,
            "cost": cost,
            "cread_cost": cread_cost,
            "cread_tokens": cread_tokens,
            "peak": peak,
        })

    if not sessions:
        print("No transcripts found in the window.")
        return

    total_cost = max(sum(s["cost"] for s in sessions), 1e-9)
    total_cread_cost = max(sum(s["cread_cost"] for s in sessions), 1e-9)
    turns_sorted = sorted(s["turns"] for s in sessions)
    peaks_sorted = sorted(s["peak"] for s in sessions)

    print("SESSION GROWTH, LAST %d DAYS" % args.days)
    print("sessions: %d (sessions under %d turns ignored)" % (len(sessions), MIN_TURNS))
    print("\n%-24s%10s%10s%10s" % ("", "p50", "p90", "max"))
    print("%-24s%10d%10d%10d"
          % ("turns per session", percentile(turns_sorted, 0.5),
             percentile(turns_sorted, 0.9), turns_sorted[-1]))
    print("%-24s%9.2fM%9.2fM%9.2fM"
          % ("peak context tokens", percentile(peaks_sorted, 0.5) / 1e6,
             percentile(peaks_sorted, 0.9) / 1e6, peaks_sorted[-1] / 1e6))

    by_cost = sorted(sessions, key=lambda s: s["cost"], reverse=True)
    decile = max(1, len(sessions) // 10)
    decile_cost = sum(s["cost"] for s in by_cost[:decile])
    p90_turns = percentile(turns_sorted, 0.9)
    long_cread = sum(s["cread_cost"] for s in sessions if s["turns"] >= p90_turns)

    print("\nCONCENTRATION")
    print("cache read share of cost-equivalent: %.1f%%" % (100 * total_cread_cost / total_cost))
    print("top %d sessions by cost hold %.1f%% of cost-equivalent"
          % (decile, 100 * decile_cost / total_cost))
    print("sessions of %d+ turns (p90) hold %.1f%% of cache read cost"
          % (p90_turns, 100 * long_cread / total_cread_cost))

    print("\nLARGEST SESSIONS BY COST-EQUIVALENT")
    print("%-10s%8s%12s%13s%10s%8s"
          % ("session", "turns", "peak ctx", "cache read", "cost", "share"))
    for s in by_cost[:args.top]:
        print("%-10s%8d%11.2fM%12.1fM%10.2f%7.1f%%"
              % (s["id"], s["turns"], s["peak"] / 1e6, s["cread_tokens"] / 1e6,
                 s["cost"], 100 * s["cost"] / total_cost))

    print("\nEvery turn re-reads the accumulated transcript. A session that has grown")
    print("past its task pays for its full history on every subsequent turn. Starting")
    print("a fresh session resets that cost to zero.")
    print("\nNot measured: whether long sessions carried context the work actually needed.")


if __name__ == "__main__":
    main()
