#!/usr/bin/env python3
"""Reap only legacy orphaned Hermes bridge SSH hold processes.

This deliberately matches the exact old forced command, the configured bridge
user, an abandoned systemd SSH session scope, PPID 1, and an SSH environment.
It must not terminate arbitrary sleeps or live SSH tunnel processes.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import signal
import time
from pathlib import Path
from typing import Any


def _cmdline(process_root: Path) -> list[str]:
    try:
        return [
            part.decode(errors="replace")
            for part in (process_root / "cmdline").read_bytes().split(b"\0")
            if part
        ]
    except OSError:
        return []


def _environment_keys(process_root: Path) -> set[str]:
    try:
        entries = (process_root / "environ").read_bytes().split(b"\0")
    except OSError:
        return set()
    return {
        entry.split(b"=", 1)[0].decode(errors="replace")
        for entry in entries
        if b"=" in entry
    }


def _ppid(process_root: Path) -> int:
    try:
        fields = (process_root / "stat").read_text().split()
        return int(fields[3])
    except (OSError, ValueError, IndexError):
        return 0


def _session_scope(process_root: Path, uid: int) -> str | None:
    try:
        lines = (process_root / "cgroup").read_text().splitlines()
    except OSError:
        return None
    prefix = f"/user.slice/user-{uid}.slice/session-"
    for line in lines:
        path = line.rsplit(":", 1)[-1]
        if path.startswith(prefix) and path.endswith(".scope"):
            return path.rsplit("/", 1)[-1]
    return None


def legacy_orphans(*, uid: int, proc_root: Path = Path("/proc")) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for process_root in proc_root.iterdir():
        if not process_root.name.isdigit():
            continue
        try:
            if process_root.stat().st_uid != uid:
                continue
        except OSError:
            continue
        if _cmdline(process_root) != ["/usr/bin/sleep", "infinity"]:
            continue
        if _ppid(process_root) != 1:
            continue
        if "SSH_CONNECTION" not in _environment_keys(process_root):
            continue
        scope = _session_scope(process_root, uid)
        if scope is None:
            continue
        matches.append({"pid": int(process_root.name), "scope": scope})
    return sorted(matches, key=lambda item: int(item["pid"]))


def reap(*, uid: int, dry_run: bool, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    matches = legacy_orphans(uid=uid, proc_root=proc_root)
    pids = [int(item["pid"]) for item in matches]
    if not dry_run:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not any((proc_root / str(pid)).exists() for pid in pids):
                break
            time.sleep(0.1)
        for pid in pids:
            if (proc_root / str(pid)).exists():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    return {
        "dry_run": dry_run,
        "matched": len(matches),
        "pids": pids,
        "scopes": sorted({str(item["scope"]) for item in matches}),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="gmv")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    try:
        uid = int(args.user)
    except ValueError:
        uid = int(pwd.getpwnam(args.user).pw_uid)
    result = reap(uid=uid, dry_run=args.dry_run)
    if not args.verbose:
        result = {
            "dry_run": bool(result["dry_run"]),
            "matched": int(result["matched"]),
            "pid_count": len(result["pids"]),
            "scope_count": len(result["scopes"]),
        }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
