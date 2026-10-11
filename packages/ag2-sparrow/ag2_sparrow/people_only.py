"""People-only messages: a Matrix message whose content carries
`"space.ag2.no_agent": true` is for people, never for an agent.

The ONE place the engine decides that. Every reader that hands room messages to
an agent or model (room-ops `read`/`history`, the events stream, the taskify
promotion) leaves flagged messages out, and every reactor (the 👀 observer)
stays silent on them. The broker refuses to taskify such events server-side; this
module is the engine-side half of the same contract.

Only a literal JSON `true` counts — a string, a number, a dict or `false` is not
the flag. The key is read on the object itself (a flattened gateway message item
or a Matrix content dict) and under its `content` (an event envelope). No other
module may spell the key or the literal-true rule; vendored verbatim into
ag2-sparrow by tools/sync_from_src.py.
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

NO_AGENT_KEY = "space.ag2.no_agent"


def is_people_only(obj) -> bool:
    """True only for a literal `true` flag on `obj` or on `obj["content"]`."""
    if not isinstance(obj, dict):
        return False
    if obj.get(NO_AGENT_KEY) is True:
        return True
    content = obj.get("content")
    return isinstance(content, dict) and content.get(NO_AGENT_KEY) is True


def split_people_only(items: Iterable) -> Tuple[List, int]:
    """`items` without the flagged ones, and how many were left out.

    The count is returned, not swallowed, so a reader can tell the agent
    "N people-only messages omitted" instead of presenting a quiet room."""
    kept: List = []
    omitted = 0
    for item in items or ():
        if is_people_only(item):
            omitted += 1
        else:
            kept.append(item)
    return kept, omitted


def omitted_note(count: int) -> str:
    """The one wording every reader uses for the omission line."""
    return f"{count} people-only message{'' if count == 1 else 's'} omitted"
