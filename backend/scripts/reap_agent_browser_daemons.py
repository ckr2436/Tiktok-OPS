#!/usr/bin/env python3
"""Reap idle agent-browser daemons without touching remote Chrome slots.

The native agent-browser process daemonizes and can outlive Celery, SSH, and
Codex clients.  This reaper uses the host-visible lock/activity files written
by ``direct_browser.py`` and enumerates /proc so overwritten PID files cannot
hide older daemons.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_RUNTIME_DIR = Path("/run/gmv/agent-browser-runtime")


def _read_environment(pid: int) -> dict[str, str]:
    try:
        entries = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    except OSError:
        return {}
    result: dict[str, str] = {}
    for entry in entries:
        if b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        if key in {b"AGENT_BROWSER_DAEMON", b"AGENT_BROWSER_SESSION", b"HOME"}:
            result[key.decode()] = value.decode(errors="replace")
    return result


def _process_age_seconds(pid: int) -> float:
    try:
        stat_fields = (Path("/proc") / str(pid) / "stat").read_text().split()
        start_ticks = int(stat_fields[21])
        uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
        return max(0.0, uptime_seconds - (start_ticks / os.sysconf("SC_CLK_TCK")))
    except (OSError, ValueError, IndexError):
        return 0.0


def _daemon_groups(owner_uid: int | None) -> dict[tuple[int, str], list[dict[str, Any]]]:
    groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for process_root in Path("/proc").iterdir():
        if not process_root.name.isdigit():
            continue
        try:
            uid = int(process_root.stat().st_uid)
        except OSError:
            continue
        if owner_uid is not None and uid != owner_uid:
            continue
        pid = int(process_root.name)
        environment = _read_environment(pid)
        session = str(environment.get("AGENT_BROWSER_SESSION") or "").strip()
        if environment.get("AGENT_BROWSER_DAEMON") != "1" or not session:
            continue
        try:
            command = (process_root / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace",
            )
        except OSError:
            continue
        if "agent-browser" not in command:
            continue
        groups[(uid, session)].append(
            {
                "pid": pid,
                "age_seconds": _process_age_seconds(pid),
                "home": str(environment.get("HOME") or ""),
            }
        )
    return dict(groups)


def _activity_age(runtime_dir: Path, uid: int, session: str, records: list[dict[str, Any]]) -> float:
    marker = runtime_dir / str(uid) / f"{session}.activity"
    try:
        return max(0.0, time.time() - marker.stat().st_mtime)
    except OSError:
        # A newly-created pre-deployment daemon has no marker.  Use the
        # youngest matching process so it still receives a full idle window.
        return min((float(item["age_seconds"]) for item in records), default=0.0)


def _try_lock(runtime_dir: Path, uid: int, session: str):
    owner_dir = runtime_dir / str(uid)
    lock_files = []
    try:
        owner_dir.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(owner_dir, uid, -1)
            owner_dir.chmod(0o700)
        # A command lock protects an in-flight CLI call.  The stage lock
        # protects the longer gaps while ChatGPT is generating and no CLI call
        # is active.  Both must be available before a daemon is considered an
        # orphan.
        for suffix in ("stage.lock", "lock"):
            lock_file = (owner_dir / f"{session}.{suffix}").open("a+", encoding="utf-8")
            lock_files.append(lock_file)
            if os.geteuid() == 0:
                os.fchown(lock_file.fileno(), uid, -1)
                os.fchmod(lock_file.fileno(), 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_files
    except (OSError, BlockingIOError):
        for lock_file in reversed(lock_files):
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                lock_file.close()
            except OSError:
                pass
        return None


def _remove_state(records: list[dict[str, Any]], session: str) -> None:
    homes = {str(item.get("home") or "").strip() for item in records}
    for home in homes:
        if not home:
            continue
        state_dir = Path(home) / ".agent-browser"
        for suffix in ("sock", "pid", "stream", "version", "engine"):
            try:
                (state_dir / f"{session}.{suffix}").unlink(missing_ok=True)
            except OSError:
                pass


def reap(
    *,
    idle_seconds: int,
    all_users: bool,
    dry_run: bool,
    runtime_dir: Path,
) -> dict[str, Any]:
    owner_uid = None if all_users else os.getuid()
    groups = _daemon_groups(owner_uid)
    result: dict[str, Any] = {
        "examined_sessions": len(groups),
        "examined_processes": sum(len(records) for records in groups.values()),
        "stopped_sessions": 0,
        "stopped_pids": [],
        "busy_sessions": [],
        "recent_sessions": [],
        "dry_run": dry_run,
    }

    for (uid, session), records in sorted(groups.items()):
        idle_age = _activity_age(runtime_dir, uid, session, records)
        if idle_age < idle_seconds:
            result["recent_sessions"].append({"uid": uid, "session": session})
            continue
        lock_files = _try_lock(runtime_dir, uid, session)
        if lock_files is None:
            result["busy_sessions"].append({"uid": uid, "session": session})
            continue
        try:
            pids = sorted({int(item["pid"]) for item in records})
            if dry_run:
                result["stopped_pids"].extend(pids)
                result["stopped_sessions"] += 1
                continue
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if not any(Path(f"/proc/{pid}").exists() for pid in pids):
                    break
                time.sleep(0.1)
            for pid in pids:
                if Path(f"/proc/{pid}").exists():
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            _remove_state(records, session)
            try:
                (runtime_dir / str(uid) / f"{session}.activity").unlink(missing_ok=True)
            except OSError:
                pass
            result["stopped_pids"].extend(pids)
            result["stopped_sessions"] += 1
        finally:
            for lock_file in reversed(lock_files):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()

    result["stopped_pids"] = sorted(set(result["stopped_pids"]))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--idle-seconds", type=int, default=900)
    parser.add_argument("--all-users", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path(os.getenv("HERMES_AGENT_BROWSER_RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR))),
    )
    args = parser.parse_args()
    if args.idle_seconds < 60:
        parser.error("--idle-seconds must be at least 60")
    result = reap(
        idle_seconds=args.idle_seconds,
        all_users=args.all_users,
        dry_run=args.dry_run,
        runtime_dir=args.runtime_dir,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
