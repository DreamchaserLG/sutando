#!/usr/bin/env python3
"""Run one command holding an exclusive kernel lock, so at most one copy runs per lock file.

    notifier_lock.py --lock PATH [--timeout S] [--log-every S] [--log-file F] -- CMD [ARGS...]

The lock is fcntl.flock(LOCK_EX) on PATH, held by a forked holder process and never by CMD, so
nothing CMD starts can inherit it. CMD runs in this process (exec), the holder's parent; the holder
exits once that parent is gone, kill -9 included, and the kernel drops the lock with it.
SUTANDO_NOTIFIER_LOCK_HOLDER=<holder pid> in CMD's environment names the holder.
A second caller waits, logging every --log-every seconds (to stderr and --log-file), and exits 75
after --timeout. While it waits, PATH.waiting holds "<pid> <epoch it began waiting>", for health-check.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import signal
import sys
import time

EXIT_BUSY = 75
POLL_S = 0.1


def stderr_and_file(log_file: "str | None"):
    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)
        if log_file and os.path.isdir(os.path.dirname(log_file) or "."):
            try:
                with open(log_file, "a") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")
            except OSError:
                pass
    return log


def acquire(path: str, timeout: float, log_every: float, *, clock=time.monotonic, sleep=time.sleep,
            log=stderr_and_file(None)) -> "int | None":
    """An fd holding LOCK_EX on `path`, or None when another holder kept it past `timeout`."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    waiting = path + ".waiting"
    start = clock()
    next_log = start + log_every
    announced = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                now = clock()
                if not announced:
                    _write_waiting(waiting)
                    announced = True
                if now - start >= timeout:
                    os.close(fd)
                    return None
                if now >= next_log:
                    log(f"notifier-lock: waiting {now - start:.0f}s for {path}; another notifier holds it")
                    next_log = now + log_every
                sleep(POLL_S)
    finally:
        try:
            with open(waiting) as fh:
                mine = fh.read().split()[:1] == [str(os.getpid())]
            if mine:
                os.unlink(waiting)
        except OSError:
            pass


def _write_waiting(waiting: str) -> None:
    try:
        with open(waiting, "w") as fh:
            fh.write(f"{os.getpid()} {int(time.time())}\n")
    except OSError:
        pass


def hold(fd: int, leader: int, *, getppid=os.getppid, sleep=time.sleep) -> None:
    """Keep `fd`'s lock while `leader` is this process's parent; signals that stop the leader
    gracefully must not drop the lock before the leader has actually exited."""
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGPIPE):
        signal.signal(sig, signal.SIG_IGN)
    while getppid() == leader:
        sleep(POLL_S)


def _detach_stdio(keep: int) -> None:
    null = os.open(os.devnull, os.O_RDWR)
    for std in (0, 1, 2):
        os.dup2(null, std)
    os.closerange(3, keep)
    os.closerange(keep + 1, 65536)


def main(argv: "list[str] | None" = None, *, execvpe=os.execvpe, fork=os.fork) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print(__doc__, file=sys.stderr)
        return 2
    split = argv.index("--")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lock", required=True)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--log-every", type=float, default=30.0)
    ap.add_argument("--log-file")
    args = ap.parse_args(argv[:split])
    cmd = argv[split + 1:]
    if not cmd:
        print(__doc__, file=sys.stderr)
        return 2
    log = stderr_and_file(args.log_file)
    fd = acquire(args.lock, args.timeout, args.log_every, log=log)
    if fd is None:
        log(f"notifier-lock: {args.lock} still held after {args.timeout:.0f}s; not starting")
        return EXIT_BUSY
    leader = os.getpid()
    holder = fork()
    if holder == 0:
        try:
            _detach_stdio(fd)
            hold(fd, leader)
        finally:
            os._exit(0)
    os.close(fd)
    execvpe(cmd[0], cmd, dict(os.environ, SUTANDO_NOTIFIER_LOCK_HOLDER=str(holder)))
    return 0  # only reached with a test's execvpe


if __name__ == "__main__":
    sys.exit(main())
