"""A deliberate hold on an open task: a machine-readable marker naming why it waits.

An adapter places it when its worker parks a task on purpose; health readers read it
so a held task is reported as held, never as one nobody picked up."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

HOLD_DIR = ".task-holds"
OWNER_GO = "owner-go"
_REASON_RE = re.compile(r"[a-z0-9-]{1,40}")


def marker_path(result_dir: Path, tid: str) -> Path:
    return Path(result_dir) / HOLD_DIR / f"{tid}.json"


def place(result_dir: Path, tid: str, reason: str = OWNER_GO, now: "float | None" = None) -> bool:
    """Record the hold; False when `tid` is already held, so the first caller alone acts on it."""
    if not _REASON_RE.fullmatch(reason):
        raise ValueError(f"hold reason must be a [a-z0-9-]{{1,40}} slug: {reason!r}")
    path = marker_path(result_dir, tid)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"reason": reason, "since": time.time() if now is None else now}, fh)
    return True


def release(result_dir: Path, tid: str) -> None:
    marker_path(result_dir, tid).unlink(missing_ok=True)


def read(result_dir: Path, tid: str) -> "dict | None":
    """{"reason", "since"} while `tid` is held; an unreadable marker still counts as held."""
    path = marker_path(result_dir, tid)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        record = None
    if not (isinstance(record, dict) and isinstance(record.get("reason"), str)
            and isinstance(record.get("since"), (int, float))):
        return {"reason": "unknown", "since": None}
    return {"reason": record["reason"], "since": float(record["since"])}


def prune(result_dir: Path, task_dir: Path) -> None:
    """Drop the holds of tasks no longer in `task_dir` (answered and archived)."""
    try:
        markers = list((Path(result_dir) / HOLD_DIR).glob("*.json"))
    except OSError:
        return
    for marker in markers:
        if not (Path(task_dir) / f"{marker.stem}.txt").exists():
            marker.unlink(missing_ok=True)
