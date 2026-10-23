"""A deliberate hold on an open task: a machine-readable marker naming why it waits.

An adapter places it when its worker parks a task on purpose; health readers read it
so a held task is reported as held, never as one nobody picked up. Every operation is
confined to the literal RESULT_DIR/.task-holds directory and never follows a symlink;
only a whole, well-formed marker counts, anything else is not a hold."""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
import time
from pathlib import Path

from . import gateway_instance, local_task_protocol

HOLD_DIR = ".task-holds"
OWNER_GO = "owner-go"
_REASON_RE = re.compile(r"[a-z0-9-]{1,40}")
_MAX_BYTES = 4096
# Epoch seconds before year 5138: bounds `since` to a sane, finite range.
_MAX_EPOCH = 1e11


def valid_tid(tid: str) -> bool:
    """A live task id, optionally under a named gateway instance (`task-<inst>~<id>`)."""
    if not isinstance(tid, str):
        return False
    if local_task_protocol.valid_task_id(tid):
        return True
    inst, sep, broker = tid[len("task-"):].partition("~")
    return (tid.startswith("task-") and bool(sep)
            and gateway_instance.INSTANCE_RE.fullmatch(inst) is not None
            and local_task_protocol.valid_task_id(broker))


def _name(tid: str) -> str:
    if not valid_tid(tid):
        raise ValueError(f"not a task id: {tid!r}")
    return f"{tid}.json"


def _valid_since(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= _MAX_EPOCH)


def _open_dir(result_dir: Path, create: bool) -> "int | None":
    """An fd on the literal hold dir; None when it does not exist. A symlink or a
    non-directory there raises ValueError: nothing is ever read or written through it."""
    path = Path(result_dir) / HOLD_DIR
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"{path} is a symlink or not a directory")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if os.fstat(fd).st_ino != st.st_ino:
        os.close(fd)
        raise ValueError(f"{path} changed while it was opened")
    return fd


def _read_at(dfd: int, name: str) -> "dict | None":
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, _MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        record = json.loads(raw.decode("utf-8"), parse_constant=lambda c: None)
    except (UnicodeDecodeError, ValueError):
        return None
    if not (isinstance(record, dict) and isinstance(record.get("reason"), str)
            and _REASON_RE.fullmatch(record["reason"]) and _valid_since(record.get("since"))
            and isinstance(record.get("token"), str) and record["token"]):
        return None
    return record


def place(result_dir: Path, tid: str, reason: str = OWNER_GO,
          now: "float | None" = None) -> "str | None":
    """Write the marker whole and exclusively in one step; its token, or None when one exists."""
    name = _name(tid)
    since = time.time() if now is None else now
    if not _REASON_RE.fullmatch(reason):
        raise ValueError(f"hold reason must be a [a-z0-9-]{{1,40}} slug: {reason!r}")
    if not _valid_since(since):
        raise ValueError(f"hold since must be a finite epoch: {since!r}")
    token = secrets.token_hex(16)
    body = json.dumps({"reason": reason, "since": since, "token": token}).encode("utf-8")
    return token if _create_at(result_dir, name, body) else None


def _held_dir(result_dir: Path, create: bool) -> int:
    """`_open_dir`, but a dir that is absent (or vanished mid-open) raises: a relative
    operation never falls back to the process CWD."""
    dfd = _open_dir(result_dir, create)
    if dfd is None:
        raise FileNotFoundError(f"{Path(result_dir) / HOLD_DIR} is absent")
    return dfd


def _create_at(result_dir: Path, name: str, body: bytes) -> bool:
    """Write `name` whole and exclusively under the held dir fd; False when it exists."""
    dfd = _held_dir(result_dir, create=True)
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=dfd)
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except FileExistsError:
            return False
        os.fsync(dfd)
        return True
    finally:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError:
            pass
        os.close(dfd)


def read(result_dir: Path, tid: str) -> "dict | None":
    """{"reason", "since", "token"} for a whole, well-formed marker; None for anything else."""
    try:
        name = _name(tid)
        dfd = _open_dir(result_dir, create=False)
    except (ValueError, OSError):
        return None
    if dfd is None:
        return None
    try:
        return _read_at(dfd, name)
    finally:
        os.close(dfd)


def release(result_dir: Path, tid: str, token: "str | None" = None) -> bool:
    """Remove the marker; with `token`, only the hold that token placed. True when removed.
    The marker is first renamed to a private tombstone, so the check and the removal act
    on one inode and a successor's marker is never the one deleted."""
    name = _name(tid)
    dfd = _open_dir(result_dir, create=False)
    if dfd is None:
        return False
    tomb = f".{name}.{secrets.token_hex(8)}.released"
    try:
        try:
            os.rename(name, tomb, src_dir_fd=dfd, dst_dir_fd=dfd)
        except FileNotFoundError:
            return False
        if token is not None and (_read_at(dfd, tomb) or {}).get("token") != token:
            try:
                os.link(tomb, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except FileExistsError:
                pass  # a newer hold took the name meanwhile; it stands, this one is gone
            os.unlink(tomb, dir_fd=dfd)
            return False
        os.unlink(tomb, dir_fd=dfd)
        return True
    finally:
        os.close(dfd)


def mark_notice(result_dir: Path, tid: str) -> bool:
    """Record, durably and once, that this task's one notice was queued; False when it was."""
    return _create_at(result_dir, f"{_name(tid)[:-len('.json')]}.notice", b"queued\n")


def clear_notice(result_dir: Path, tid: str) -> None:
    """Forget a notice record whose notice was never queued."""
    dfd = _held_dir(result_dir, create=False)
    try:
        os.unlink(f"{_name(tid)[:-len('.json')]}.notice", dir_fd=dfd)
    except FileNotFoundError:
        pass
    finally:
        os.close(dfd)


def notice_marked(result_dir: Path, tid: str) -> bool:
    """Whether a notice was ever queued for this task; an unreadable dir counts as queued."""
    name = f"{_name(tid)[:-len('.json')]}.notice"
    try:
        dfd = _open_dir(result_dir, create=False)
    except (OSError, ValueError):
        return True
    if dfd is None:
        return False
    try:
        os.stat(name, dir_fd=dfd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True
    finally:
        os.close(dfd)


def prune(result_dir: Path, task_dir: Path) -> None:
    """Drop the holds of tasks no longer in `task_dir` (answered and archived)."""
    dfd = _open_dir(result_dir, create=False)
    if dfd is None:
        return
    try:
        for name in os.listdir(dfd):
            tid = next((name[:-len(x)] for x in (".json", ".notice") if name.endswith(x)), "")
            if valid_tid(tid) and not (Path(task_dir) / f"{tid}.txt").exists():
                try:
                    os.unlink(name, dir_fd=dfd)
                except FileNotFoundError:
                    pass
    finally:
        os.close(dfd)
