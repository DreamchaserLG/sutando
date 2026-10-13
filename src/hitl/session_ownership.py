"""Same-session card ownership: exactly one active requirement per session.

Two independent watchers of the same tmux pane — the worker-pool's wedge
ladder and core-input-watch's own monitor loop — can each decide, on their
own schedule, that a session needs a card. Dependency-light on purpose (only
`HitlManager.active()`): each caller is a separate adapter at its own edge
(a skill, core infrastructure) and neither should import the other's module
to ask "did you already card this." This is the one shared answer both ask.
"""
from __future__ import annotations

from typing import Any


def session_already_carded(manager: Any, session: str, *, by_source: str) -> bool:
    """Is there an active requirement for `session` from a source other than
    `by_source`? By effect (an active record), not by whether some other
    watcher's own process is merely alive. A record from `by_source` itself
    (including an older one of the caller's own episodes) never counts —
    that case is the caller's own dedup/staleness concern, not an ownership
    question between two different watchers."""
    if manager is None:
        return False
    try:
        records = manager.active()
    except Exception:  # noqa: BLE001 — an unreadable store is not proof of a duplicate
        return False
    return any((r.subject or {}).get("session") == session
              and (r.subject or {}).get("source") not in (None, by_source)
              for r in records)
