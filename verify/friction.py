#!/usr/bin/env python3
"""Friction and rework analysis across Claude Code transcripts.

Measures proxies for how often work had to be redone:

  tool error rate  fraction of tool calls returning is_error
  correction rate  fraction of human turns containing correction language
  edit churn       repeat Edit calls against the same file within a session
  context growth   cache read per turn, which drives cost on long sessions

These measure friction, not correctness. A session with no corrections can still
have shipped a defect. Regex detection also misses corrections phrased indirectly.
Rates are reported per turn as well as per session, because session length varies
between periods and per-session counts are not comparable on their own.

Reads ~/.claude/projects/**/*.jsonl. Nothing is transmitted. No conversation
content is printed; only counts and session identifiers.

Usage:
    python3 friction.py 2026-08-03
"""
import collections
import glob
import json
import os
import re
import sys
from datetime import datetime

TRANSCRIPT_ROOT = os.path.expanduser("~/.claude/projects")

CORRECTION = re.compile(
    r"\b(no,|nope\b|that'?s wrong|that is wrong|incorrect\b|not (?:what|quite) i|"
    r"doesn'?t work|does not work|didn'?t work|did not work|still (?:not|broken|failing|doesn)|"
    r"revert\b|undo\b|you broke|broke it|that'?s not right|wrong again|try again|"
    r"you (?:missed|forgot|misunderstood)|not right\b|fix (?:it|that) again|"
    r"why did you|you shouldn'?t have|i said\b)", re.I)

MIN_TURNS = 3  # ignore trivial sessions


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



def human_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    split = datetime.strptime(sys.argv[1], "%Y-%m-%d")

    stats = collections.defaultdict(collections.Counter)
    session_ids = collections.defaultdict(set)

    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "**", "*.jsonl"), recursive=True):
        started, sid = session_meta(path)
        period = "AFTER" if started >= split else "BEFORE"
        tool_calls = tool_errors = human_turns = corrections = assistant_turns = 0
        cache_read = 0
        edits = collections.Counter()
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
                content = message.get("content")
                if record.get("type") == "assistant":
                    assistant_turns += 1
                    cache_read += (message.get("usage") or {}).get("cache_read_input_tokens", 0)
                if record.get("type") == "user":
                    text = human_text(content).strip()
                    # Exclude tool results and injected reminders; count real human turns only.
                    if text and not text.startswith("<") and "system-reminder" not in text[:200]:
                        human_turns += 1
                        if CORRECTION.search(text[:1500]):
                            corrections += 1
                for block in (content or []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_calls += 1
                        if block.get("name") == "Edit":
                            target = (block.get("input") or {}).get("file_path")
                            if target:
                                edits[target] += 1
                    if block.get("type") == "tool_result" and block.get("is_error"):
                        tool_errors += 1
        if assistant_turns < MIN_TURNS:
            continue
        bucket = stats[period]
        session_ids[period].add(sid)
        bucket["tool_calls"] += tool_calls
        bucket["tool_errors"] += tool_errors
        bucket["human_turns"] += human_turns
        bucket["corrections"] += corrections
        bucket["turns"] += assistant_turns
        bucket["cache_read"] += cache_read
        bucket["edits"] += sum(edits.values())
        bucket["churn"] += sum(v - 1 for v in edits.values() if v > 1)

    for period, ids in session_ids.items():
        stats[period]["sessions"] = len(ids)

    if not stats.get("BEFORE") or not stats.get("AFTER"):
        print("Need transcripts on both sides of the split date.")
        sys.exit(1)

    def row(label, fn, fmt="%.2f"):
        values = []
        for period in ("BEFORE", "AFTER"):
            try:
                values.append(fmt % fn(stats[period]))
            except ZeroDivisionError:
                values.append("-")
        print("%-26s%12s%12s" % (label, values[0], values[1]))

    print("%-26s%12s%12s" % ("", "BEFORE", "AFTER"))
    row("sessions", lambda s: s["sessions"], "%.0f")
    row("tool calls", lambda s: s["tool_calls"], "%.0f")
    print("\nFRICTION (lower is better)")
    row("tool error rate %", lambda s: 100 * s["tool_errors"] / s["tool_calls"])
    row("tool errors per turn", lambda s: s["tool_errors"] / s["turns"], "%.4f")
    row("correction rate %", lambda s: 100 * s["corrections"] / s["human_turns"])
    row("corrections per turn", lambda s: s["corrections"] / s["turns"], "%.5f")
    row("edit churn %", lambda s: 100 * s["churn"] / max(s["edits"], 1))
    print("\nWORKLOAD SHAPE")
    row("turns per session", lambda s: s["turns"] / s["sessions"], "%.1f")
    row("cache read M per turn", lambda s: s["cache_read"] / s["turns"] / 1e6)
    row("tool calls per turn", lambda s: s["tool_calls"] / s["turns"])

    print("\nNot measured: whether the output was correct.")


if __name__ == "__main__":
    main()
