#!/usr/bin/python3
"""Hold one restricted SSH tunnel and die with its owning sshd process.

The Windows bridge authenticates with a forced command so its key cannot run
arbitrary server commands.  A plain ``sleep infinity`` becomes orphaned when
the SSH transport disappears.  Linux PR_SET_PDEATHSIG gives this process an
explicit owner: the kernel terminates it as soon as the sshd session process
exits.
"""

from __future__ import annotations

import ctypes
import os
import signal


PR_SET_PDEATHSIG = 1


def main() -> int:
    parent_pid = os.getppid()
    if parent_pid <= 1:
        return 0

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        return 1

    # The parent can disappear between getppid() and prctl(). In that race the
    # kernel cannot deliver a historical parent-death signal, so exit here.
    if os.getppid() != parent_pid:
        return 0

    while True:
        signal.pause()


if __name__ == "__main__":
    raise SystemExit(main())
