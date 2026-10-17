"""Owner-reply plumbing for a launcher gate that runs while no core is up to read the owner's DM.

A gate that refuses to start the core can still talk to the owner. It queues one
DM under a fixed `results/proactive-*.txt` name, reads the owner's bare reply
from `tasks/`, answers that reply in its own conversation, and archives it so a
core that comes up later never acts on it a second time.

This module holds only that shape, so a second gate reuses it instead of copying
it. The decision of what a reply means stays with the caller.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator, Optional, Tuple

_TRAILING = re.compile(r"[\s.!]+$")


def _ws(workspace: Optional[Path]) -> Path:
    from workspace_default import resolve_workspace
    return Path(workspace) if workspace else Path(resolve_workspace())


def normalize(text: str) -> str:
    """Lower case, inner spaces collapsed, trailing punctuation dropped."""
    return _TRAILING.sub("", " ".join((text or "").split()).lower())


def owner_reply_text(path: Path) -> Optional[str]:
    """The reply body of an owner-tier task, without quoted reply context; else None."""
    from local_task_protocol import parse_task_headers_lenient, split_reply_context
    from policy.egress.result import resolve_access_tier
    if resolve_access_tier(path) != "owner":
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return split_reply_context(parse_task_headers_lenient(text).body)[1].strip()


def matching_replies(workspace: Optional[Path], phrases: Iterable[str],
                     skip: Iterable[str] = ()) -> Iterator[Tuple[Path, str]]:
    """(task, phrase) for each unanswered owner task whose whole message is one phrase."""
    wanted = {normalize(p): p for p in phrases}
    skip = set(skip)
    workspace = _ws(workspace)
    for path in sorted((workspace / "tasks").glob("task-*.txt")):
        if path.stem in skip or (workspace / "results" / path.name).exists():
            continue
        body = owner_reply_text(path)
        if body is not None and normalize(body) in wanted:
            yield path, wanted[normalize(body)]


def queue_notice(workspace: Optional[Path], name: str, body: str) -> bool:
    """Write one owner DM under a fixed results/ name. A retry rewrites the same name."""
    from local_record import write_text_whole
    from result_markers import neutralize_markers
    try:
        write_text_whole(_ws(workspace) / "results" / name, "[dm-only]\n" + neutralize_markers(body))
    except OSError:
        return False
    return True


def answer(workspace: Optional[Path], path: Path, text: str) -> None:
    """Reply in the task's own conversation, then move the task out of the live queue."""
    from local_record import write_text_whole
    from task_archive import archive_file
    workspace = _ws(workspace)
    write_text_whole(workspace / "results" / path.name, text.rstrip("\n") + "\n")
    archive_file(path, "tasks", path.stem, tasks_dir=workspace / "tasks" / "archive",
                 results_dir=workspace / "results" / "archive", log=lambda _m: None)
