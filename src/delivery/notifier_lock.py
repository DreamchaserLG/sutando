#!/usr/bin/env python3
"""Run one command holding an exclusive kernel lock, so at most one copy runs per lock file.

    notifier_lock.py --lock PATH [--timeout S] [--log-every S] -- CMD [ARGS...]

The lock is fcntl.flock(LOCK_EX) on PATH, moved to fd 9 and inherited across exec, with
SUTANDO_NOTIFIER_LOCK_FD=9 in the environment: it lives exactly as long as CMD's process (and
anything still holding fd 9), and the kernel drops it when that process dies, kill -9 included.
A second caller waits, logging every --log-every seconds, and exits 75 after --timeout.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time

LOCK_FD = 9
EXIT_BUSY = 75
POLL_S = 0.1


def acquire(path: str, timeout: float, log_every: float, *, clock=time.monotonic, sleep=time.sleep,
            log=lambda msg: print(msg, file=sys.stderr, flush=True)) -> "int | None":
    """An fd holding LOCK_EX on `path`, or None when another holder kept it past `timeout`."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    start = clock()
    next_log = start + log_every
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            now = clock()
            if now - start >= timeout:
                os.close(fd)
                return None
            if now >= next_log:
                log(f"notifier-lock: waiting {now - start:.0f}s for {path}; another notifier holds it")
                next_log = now + log_every
            sleep(POLL_S)


def main(argv: "list[str] | None" = None, *, execvpe=os.execvpe) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print(__doc__, file=sys.stderr)
        return 2
    split = argv.index("--")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lock", required=True)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--log-every", type=float, default=30.0)
    args = ap.parse_args(argv[:split])
    cmd = argv[split + 1:]
    if not cmd:
        print(__doc__, file=sys.stderr)
        return 2
    fd = acquire(args.lock, args.timeout, args.log_every)
    if fd is None:
        print(f"notifier-lock: {args.lock} still held after {args.timeout:.0f}s; not starting", file=sys.stderr)
        return EXIT_BUSY
    if fd != LOCK_FD:
        os.dup2(fd, LOCK_FD)
        os.close(fd)
    os.set_inheritable(LOCK_FD, True)
    execvpe(cmd[0], cmd, dict(os.environ, SUTANDO_NOTIFIER_LOCK_FD=str(LOCK_FD)))
    return 0  # only reached with a test's execvpe


if __name__ == "__main__":
    sys.exit(main())
