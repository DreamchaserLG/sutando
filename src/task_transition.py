#!/usr/bin/env python3
"""One owner of a live task's report-then-archive transition: publish the notice under an exclusive name, then move exactly the file generation that was planned.

A task file is a record another writer may replace, and a notice saying a task
is archived is a promise. `transition()` keeps it in three steps:

  1. Pre-flight, before anything is published: the archive dir exists and is
     not a symlink; every name is bare; every source is a regular file still at
     the generation the plan captured (`generation_of`). Any failure means
     nothing is published and nothing moves, so a re-run after the fix reports
     each task once.
  2. Publish the notice under a name no other publish can take (second, pid and
     a random token, committed with an exclusive link, never a replace), so two
     passes in one second leave two notices, not one. A body over
     PROACTIVE_MAX_BODY_B is refused here, before any move: the delivery
     adapter dead-letters it, which would archive the tasks unreported.
  3. Move each file by link + unlink. The archive name never overwrites (a
     taken name gets the `.N` tail task_archive mints); the source is checked
     again after the link and left alone if it was replaced meanwhile, so the
     new generation stays live and only the reported one is archived.

A failure inside step 3 is a race or an I/O error; it is reported per name,
the task stays live and the next pass reports it again: a duplicate notice
rather than a lost task.
"""
from __future__ import annotations

import os
import secrets
import stat
import time
from pathlib import Path

import result_publish

__all__ = ["PROACTIVE_MAX_BODY_B", "TransitionRefused", "generation_of", "is_bare_name",
           "preflight", "publish_exclusive", "archive_generation", "transition"]

# Mirrors ag2_sparrow.remote_gateway_bridge._PROACTIVE_MAX_BODY_B; a test pins the two.
PROACTIVE_MAX_BODY_B = 48 * 1024
_NAME_ATTEMPTS = 16


class TransitionRefused(Exception):
    """A precondition failed; nothing was changed for the item it names."""


def is_bare_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and Path(name).name == name


def generation_of(path: Path) -> str | None:
    """The identity of the bytes at `path` right now, or None when absent.

    Device, inode, size and mtime_ns, never following a symlink: a replaced
    file is a new generation even when it carries the same name, which is how
    the watcher tells a re-emitted task from a rewritten one."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def _source_problem(tasks_dir: Path, name: str, generation: str | None) -> str | None:
    if not is_bare_name(name):
        return "not a bare filename"
    if not generation:
        return "no generation was captured when it was planned"
    try:
        st = os.lstat(tasks_dir / name)
    except FileNotFoundError:
        return "no longer in tasks/"
    if stat.S_ISLNK(st.st_mode):
        return "a symlink, not a task file"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if generation_of(tasks_dir / name) != generation:
        return "changed since it was planned (a new generation is live under this name)"
    return None


def preflight(tasks_dir: Path, archive_dir: Path, moves: dict[str, str | None]) -> list[str]:
    """Every reason step 3 could not perform `moves` ({name: generation}), one
    line each; an empty list means the moves may be promised. Creates
    `archive_dir` when it is missing."""
    if not moves:
        return []
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [f"could not create {archive_dir}: {exc}"]
    if archive_dir.is_symlink():
        return [f"refused: {archive_dir} is a symlink"]
    return [f"refused to move {name!r}: {problem}"
            for name, problem in ((n, _source_problem(tasks_dir, n, g)) for n, g in moves.items())
            if problem]


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def publish_exclusive(results_dir: Path, prefix: str, body: str,
                      now: float | None = None) -> Path:
    """Publish `body` as `<prefix><second>-<pid>-<token>.txt` in `results_dir`,
    whole and under a name this call alone owns. Raises TransitionRefused for
    a body the adapter could never deliver; OSError propagates."""
    size = len(body.encode("utf-8"))
    if size > PROACTIVE_MAX_BODY_B:
        raise TransitionRefused(f"body is {size} bytes (> {PROACTIVE_MAX_BODY_B}); undeliverable")
    second = int(time.time() if now is None else now)
    results_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(_NAME_ATTEMPTS):
        target = results_dir / f"{prefix}{second}-{os.getpid()}-{secrets.token_hex(4)}.txt"
        tmp = result_publish.stage_text(target, body)
        try:
            os.link(tmp, target)  # refuses an existing name; never replaces
        except FileExistsError:
            tmp.unlink(missing_ok=True)
            continue
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.unlink(missing_ok=True)
        _fsync_dir(results_dir)
        return target
    raise OSError(f"could not reserve a result name under {results_dir}")


def archive_generation(tasks_dir: Path, name: str, generation: str | None,
                       archive_dir: Path) -> Path:
    """Move `tasks_dir/name` into `archive_dir` if it is still `generation`;
    returns where it landed. Raises TransitionRefused when a precondition
    fails (nothing changed); OSError propagates from the link or unlink."""
    problem = _source_problem(tasks_dir, name, generation)
    if problem:
        raise TransitionRefused(f"refused to move {name!r}: {problem}")
    src = tasks_dir / name
    dest, n = archive_dir / name, 0
    while True:
        try:
            os.link(src, dest)
            break
        except FileExistsError:
            n += 1
            dest = archive_dir / f"{name}.{n}"
    # Replaced between the link and here: the new file is a live task, not ours to remove.
    if generation_of(src) == generation:
        os.unlink(src)
    _fsync_dir(archive_dir)
    return dest


def transition(tasks_dir: Path, results_dir: Path, archive_dir: Path, *,
               moves: dict[str, str | None], body: str | None, prefix: str,
               now: float | None = None) -> dict:
    """Perform the whole transition. Returns `result` (the notice's path or
    None), `archived` ({name: landed name}) and `errors` (one line each)."""
    out: dict = {"result": None, "archived": {}, "errors": []}
    problems = preflight(tasks_dir, archive_dir, moves)
    if problems:
        out["errors"] = [f"{p}; nothing published, nothing moved" for p in problems]
        return out
    if body is not None:
        try:
            out["result"] = str(publish_exclusive(results_dir, prefix, body, now))
        except (OSError, TransitionRefused) as exc:
            out["errors"].append(f"result write failed ({exc}); nothing moved")
            return out
    for name, generation in moves.items():
        try:
            out["archived"][name] = archive_generation(tasks_dir, name, generation, archive_dir).name
        except TransitionRefused as exc:
            out["errors"].append(f"{exc}; still in tasks/, the next pass reports it again")
        except OSError as exc:
            out["errors"].append(f"move failed for {name}: {exc}")
    return out
