#!/usr/bin/env python3
"""One publisher for every `results/` body: staged beside the target, fsynced, renamed whole.

A result file is claimed on sight. `Path.write_text` and a shell `>` create the
name empty and fill it afterwards, so a drain woken by the creation reads a
zero-byte file or a prefix, delivers that, and archives the task as answered —
the real body written moments later is a "late duplicate" nobody sends (#3956).

Every in-repo writer of a result body publishes through here; adapters keep
only what to say and where. The staged name starts with a dot and ends in
`.tmp`, so no drain's `*.txt` glob can match it, and it is unique per call so
two publishers of one name never rename each other's bytes. Shell writers use
the CLI: `python3 src/result_publish.py <path>` with the body on stdin.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

__all__ = ["STAGED_SUFFIX", "REFUSED", "attributed_worker", "stage_text", "is_staged_for", "publish_staged",
           "publish_text", "main"]

STAGED_SUFFIX = ".tmp"


def stage_text(path: str | Path, text: str, mode: int = 0o666) -> Path:
    """Write `text` to a fresh sibling of `path` and fsync it; returns the staged file.

    `mode` is filtered by the umask, as `open()` would; pass 0o600 for a private record.

    Staging is separate from publishing because a sidecar the published file
    refers to may have to commit in between (the gateway's task media record).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL on a per-call name: honours the umask like a plain write would (mkstemp forces 0600).
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}{STAGED_SUFFIX}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def is_staged_for(name: str, target_name: str) -> bool:
    """True when `name` has the shape `stage_text` gives a staging of `target_name`."""
    prefix = f".{target_name}."
    if not (name.startswith(prefix) and name.endswith(STAGED_SUFFIX)):
        return False
    pid, _, token = name[len(prefix):-len(STAGED_SUFFIX)].partition(".")
    return (pid.isascii() and pid.isdigit() and len(token) == 16
            and all(c in "0123456789abcdef" for c in token))


def publish_staged(tmp: str | Path, path: str | Path) -> Path:
    """Rename a staged file onto `path` and fsync the directory, so the name survives a crash."""
    target = Path(path)
    os.replace(tmp, target)
    if os.name != "nt":
        dfd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return target


def publish_text(path: str | Path, text: str, mode: int = 0o666) -> Path:
    """Publish `text` at `path` whole: a reader sees the name absent or complete, never a prefix."""
    tmp = stage_text(path, text, mode)
    try:
        return publish_staged(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_HEX = frozenset("0123456789abcdef")


def _is_worker_id(value: str) -> bool:
    """The pool's instance-id grammar (pool_attribution.is_worker_id), kept dependency-light here."""
    return len(value) == 32 and set(value) <= _HEX


def attributed_worker(path: "str | Path") -> "str | None":
    """The pool worker a `results/task-<id>.txt` path's task is attributed to, or None.

    The router records `state/attribution/<task id>` (one line, the worker id) when it hands a task
    to a worker. Read beside the results dir, so a result written into another workspace is judged
    against that workspace. Anything unreadable or malformed reads as "no attribution": the guard
    is for the case the record is plainly there.
    """
    target = Path(path)
    if target.parent.name != "results" or not target.name.startswith("task-") or not target.name.endswith(".txt"):
        return None
    record = target.parent.parent / "state" / "attribution" / target.name[: -len(".txt")]
    try:
        value = record.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if _is_worker_id(value) else None


REFUSED = 3


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--force"]
    force = len(args) != len(argv) - 1
    if len(args) != 1 or args[0].startswith("-"):
        print("usage: result_publish.py [--force] <path>   (body on stdin)", file=sys.stderr)
        return 2
    path = args[0]
    # A task the router gave to a worker is answered by that worker. On 2026-10-22 the core read a
    # routed task out of tasks/, did it again, and overwrote the worker's result; the worker's own
    # session carries its id in SUTANDO_INSTANCE_ID, so only a mismatch is refused.
    worker = attributed_worker(path)
    me = os.environ.get("SUTANDO_INSTANCE_ID", "")
    if worker and worker != me and not force:
        who = f"worker {me}" if me else "the core"
        print(f"result_publish: {Path(path).name} is attributed to worker {worker}; this session is {who}, so it is "
              "not this session's to answer — not publishing. The worker writes it. (--force overrides.)", file=sys.stderr)
        return REFUSED
    publish_text(path, sys.stdin.read())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
