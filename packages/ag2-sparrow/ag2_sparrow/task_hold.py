"""A deliberate hold on an open task: a machine-readable marker naming why it waits.

An adapter places it when its worker parks a task on purpose; health readers read it
so a held task is reported as held, never as one nobody picked up. Only a whole,
committed marker counts: an absent, pending, partial or unreadable one is not a hold."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import gateway_instance, local_task_protocol, result_publish

HOLD_DIR = ".task-holds"
OWNER_GO = "owner-go"
PENDING, HELD = "pending", "held"
_REASON_RE = re.compile(r"[a-z0-9-]{1,40}")


def valid_tid(tid: str) -> bool:
    """A live task id, optionally under a named gateway instance (`task-<inst>~<id>`)."""
    if local_task_protocol.valid_task_id(tid):
        return True
    inst, sep, broker = (tid or "")[len("task-"):].partition("~")
    return (str(tid).startswith("task-") and bool(sep)
            and gateway_instance.INSTANCE_RE.fullmatch(inst) is not None
            and local_task_protocol.valid_task_id(broker))


def marker_path(result_dir: Path, tid: str) -> Path:
    """The marker's path, refusing any id that is not a task id or would leave the hold dir."""
    if not isinstance(tid, str) or not valid_tid(tid):
        raise ValueError(f"not a task id: {tid!r}")
    base = (Path(result_dir) / HOLD_DIR).resolve()
    path = (base / f"{tid}.json").resolve()
    if path.parent != base:
        raise ValueError(f"task id escapes {HOLD_DIR}: {tid!r}")
    return path


def _record(reason: str, state: str, since: float) -> str:
    return json.dumps({"reason": reason, "state": state, "since": since})


def reserve(result_dir: Path, tid: str, reason: str = OWNER_GO,
            now: "float | None" = None) -> bool:
    """Write a PENDING marker whole and exclusively; False when any marker already exists."""
    if not _REASON_RE.fullmatch(reason):
        raise ValueError(f"hold reason must be a [a-z0-9-]{{1,40}} slug: {reason!r}")
    path = marker_path(result_dir, tid)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    staged = result_publish.stage_text(path, _record(reason, PENDING,
                                                     time.time() if now is None else now), 0o600)
    try:
        os.link(staged, path)
    except FileExistsError:
        return False
    finally:
        staged.unlink(missing_ok=True)
    if os.name != "nt":
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return True


def commit(result_dir: Path, tid: str) -> None:
    """Turn this task's PENDING marker into a HELD one, atomically."""
    path = marker_path(result_dir, tid)
    record = _load(path)
    if record is None:
        raise ValueError(f"no readable hold marker for {tid}")
    result_publish.publish_text(path, _record(record["reason"], HELD, record["since"]), 0o600)


def place(result_dir: Path, tid: str, reason: str = OWNER_GO, now: "float | None" = None) -> bool:
    """Reserve and commit in one step; False when a marker already exists."""
    if not reserve(result_dir, tid, reason, now):
        return False
    commit(result_dir, tid)
    return True


def release(result_dir: Path, tid: str) -> None:
    marker_path(result_dir, tid).unlink(missing_ok=True)


def _load(path: Path) -> "dict | None":
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (isinstance(record, dict) and isinstance(record.get("reason"), str)
            and _REASON_RE.fullmatch(record["reason"])
            and record.get("state") in (PENDING, HELD)
            and isinstance(record.get("since"), (int, float))
            and not isinstance(record.get("since"), bool)):
        return None
    return {"reason": record["reason"], "state": record["state"],
            "since": float(record["since"])}


def state(result_dir: Path, tid: str) -> "dict | None":
    """The marker as written ({"reason", "state", "since"}), or None when absent or unreadable."""
    try:
        return _load(marker_path(result_dir, tid))
    except ValueError:
        return None


def read(result_dir: Path, tid: str) -> "dict | None":
    """{"reason", "since"} only for a committed hold; anything else is not held."""
    record = state(result_dir, tid)
    if record is None or record["state"] != HELD:
        return None
    return {"reason": record["reason"], "since": record["since"]}


def prune(result_dir: Path, task_dir: Path) -> None:
    """Drop the holds of tasks no longer in `task_dir` (answered and archived)."""
    for marker in list((Path(result_dir) / HOLD_DIR).glob("*.json")):
        if valid_tid(marker.stem) and not (Path(task_dir) / f"{marker.stem}.txt").exists():
            marker.unlink(missing_ok=True)
