#!/usr/bin/env python3
"""Summarize Gunicorn access logs.

Reads Gunicorn access log lines (stdin or a file) and prints:
- total request count
- top IPs and request paths
- HTTP status breakdown
- non-OK statuses flagged for quick investigation

Example:
    python tools/gunicorn_log_summary.py access.log --top 3

The parser is tolerant of the typical `gunicorn` syslog format, e.g.:
    11月 23 11:20:01 slkSrv gunicorn[366554]: 104.194.90.79:0 - "GET /api/healthz HTTP/1.1" 200
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, List, TextIO

LOG_PATTERN = re.compile(
    r"^.+?\s+\S+\s+gunicorn\[\d+\]:\s+"  # timestamp and host
    r"(?P<ip>[0-9a-fA-F.:]+)\S*\s+-\s+"       # IP with optional port
    r"\"(?P<method>\S+)\s+(?P<path>\S+)\s+HTTP/\d\.\d\"\s+"
    r"(?P<status>\d{2,3})"
)

OK_STATUSES = {200, 201, 202, 203, 204, 206, 304}


@dataclass
class LogSummary:
    total_requests: int = 0
    ip_counter: Counter[str] = field(default_factory=Counter)
    path_counter: Counter[str] = field(default_factory=Counter)
    status_counter: Counter[int] = field(default_factory=Counter)
    anomalies: List[str] = field(default_factory=list)
    unparsable: List[str] = field(default_factory=list)


def parse_lines(lines: Iterable[str]) -> LogSummary:
    summary = LogSummary()
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        match = LOG_PATTERN.match(line)
        if not match:
            summary.unparsable.append(line)
            continue

        ip = match.group("ip")
        path = match.group("path")
        method = match.group("method")
        status = int(match.group("status"))

        summary.total_requests += 1
        summary.ip_counter[ip] += 1
        summary.path_counter[path] += 1
        summary.status_counter[status] += 1

        if status not in OK_STATUSES:
            summary.anomalies.append(f"{status} {method} {path} (from {ip})")
    return summary


def format_counter(counter: Counter[str], top_n: int) -> str:
    lines = []
    for value, count in counter.most_common(top_n):
        lines.append(f"- {value}: {count}")
    return "\n".join(lines) if lines else "(none)"


def print_summary(summary: LogSummary, top_n: int, show_unparsable: bool) -> None:
    print(f"Total requests: {summary.total_requests}")
    print("\nTop IPs:")
    print(format_counter(summary.ip_counter, top_n))

    print("\nTop paths:")
    print(format_counter(summary.path_counter, top_n))

    print("\nStatus breakdown:")
    for status, count in summary.status_counter.most_common():
        print(f"- {status}: {count}")
    if not summary.status_counter:
        print("(none)")

    print("\nFlagged statuses (non-OK):")
    if summary.anomalies:
        for entry in summary.anomalies:
            print(f"- {entry}")
    else:
        print("(none)")

    if show_unparsable:
        print("\nUnparsable lines:")
        if summary.unparsable:
            for line in summary.unparsable:
                print(f"- {line}")
        else:
            print("(none)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize Gunicorn access logs")
    parser.add_argument(
        "logfile",
        nargs="?",
        type=argparse.FileType("r"),
        default=sys.stdin,
        help="Path to a Gunicorn access log file (defaults to stdin)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="How many top IPs and paths to show (default: 5)",
    )
    parser.add_argument(
        "--show-unparsable",
        action="store_true",
        help="Print lines that could not be parsed",
    )

    args = parser.parse_args()

    summary = parse_lines(args.logfile)
    print_summary(summary, top_n=args.top, show_unparsable=args.show_unparsable)


if __name__ == "__main__":
    main()
