from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
GUARD = SCRIPTS / "hermes_bridge_session_guard.py"
REAPER = SCRIPTS / "reap_bridge_ssh_orphans.py"
SPEC = importlib.util.spec_from_file_location("reap_bridge_ssh_orphans", REAPER)
assert SPEC and SPEC.loader
reaper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reaper)


def test_session_guard_exits_when_its_parent_dies():
    launcher = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys;"
                f"p=subprocess.Popen([sys.executable,{str(GUARD)!r}]);"
                "print(p.pid,flush=True)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert launcher.stdout is not None
    child_pid = int(launcher.stdout.readline().strip())
    assert launcher.wait(timeout=3) == 0

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and Path(f"/proc/{child_pid}").exists():
        time.sleep(0.05)
    assert not Path(f"/proc/{child_pid}").exists()


def _fake_process(
    proc_root: Path,
    *,
    pid: int,
    uid: int,
    command: list[str],
    ppid: int,
    ssh: bool,
    session_scope: bool,
) -> None:
    process_root = proc_root / str(pid)
    process_root.mkdir()
    (process_root / "cmdline").write_bytes(b"\0".join(part.encode() for part in command) + b"\0")
    (process_root / "environ").write_bytes(b"SSH_CONNECTION=x\0" if ssh else b"HOME=/opt/gmv\0")
    (process_root / "stat").write_text(
        f"{pid} (sleep) S {ppid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0",
        encoding="utf-8",
    )
    cgroup = (
        f"0::/user.slice/user-{uid}.slice/session-123.scope\n"
        if session_scope
        else "0::/system.slice/example.service\n"
    )
    (process_root / "cgroup").write_text(cgroup, encoding="utf-8")


def test_bridge_reaper_matches_only_exact_legacy_orphans(monkeypatch, tmp_path):
    uid = os.getuid()
    _fake_process(
        tmp_path, pid=101, uid=uid, command=["/usr/bin/sleep", "infinity"],
        ppid=1, ssh=True, session_scope=True,
    )
    _fake_process(
        tmp_path, pid=102, uid=uid, command=["/usr/bin/sleep", "infinity"],
        ppid=500, ssh=True, session_scope=True,
    )
    _fake_process(
        tmp_path, pid=103, uid=uid, command=["/usr/bin/sleep", "60"],
        ppid=1, ssh=True, session_scope=True,
    )
    monkeypatch.setattr(Path, "stat", lambda self: type("S", (), {"st_uid": uid})())

    result = reaper.reap(uid=uid, dry_run=True, proc_root=tmp_path)

    assert result["matched"] == 1
    assert result["pids"] == [101]
    assert result["scopes"] == ["session-123.scope"]
