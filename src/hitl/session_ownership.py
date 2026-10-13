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


def session_already_carded(manager: Any, session: str, *, by_source: str,
                           effect: "str | None" = None, legacy_match=None) -> bool:
    """Is there an active requirement for `session` from a source other than
    `by_source`? By effect (an active record), not by whether some other
    watcher's own process is merely alive. A record from `by_source` itself
    (including an older one of the caller's own episodes) never counts —
    that case is the caller's own dedup/staleness concern, not an ownership
    question between two different watchers.

    `effect` narrows the match to records tagged with the SAME effect (e.g.
    "gate"): two watchers can each independently decide a session needs a
    card for two genuinely different, co-existing questions (a frozen turn
    AND a later, distinct permission gate), and one must not suppress the
    other just because they share a session. Omitting `effect` matches on
    session alone, for a caller that has only one kind of card to arbitrate.

    `legacy_match`, when given, is an extra `subject -> bool` predicate
    checked ONLY for a record that has no "effect" field at all: a record
    created by a prior deployed writer, before effect-tagging existed, that
    is nonetheless recognizably the SAME kind of concern by its own
    established shape (the caller knows its counterpart's shape; this
    module stays agnostic). It is never applied to a record that already
    carries a (possibly different) effect tag -- an effectless record is
    the only ambiguous case, and only the caller who knows what a legacy
    record of its counterpart looks like gets to resolve it. Other
    effectless records (a FROZEN/CAUSE card, legitimately untagged) are
    simply not matched by this clause, since `legacy_match` says what COUNTS
    as the caller's own legacy shape, nothing more."""
    if manager is None:
        return False
    try:
        records = manager.active()
    except Exception:  # noqa: BLE001 — an unreadable store is not proof of a duplicate
        return False

    def _matches_effect(subj):
        if effect is None:
            return True
        tag = subj.get("effect")
        if tag == effect:
            return True
        return tag is None and legacy_match is not None and legacy_match(subj)

    return any((r.subject or {}).get("session") == session
              and (r.subject or {}).get("source") not in (None, by_source)
              and _matches_effect(r.subject or {})
              for r in records)


def claim_session(manager: Any, session: str, *, by_source: str, effect: "str | None",
                  build_requirement, legacy_match=None) -> Any:
    """Atomically check-and-create: under ONE lock acquisition, so two
    independent watchers (different processes, different schedules) cannot
    each observe "unclaimed" before either writes, which a separate check
    then create cannot prevent. `build_requirement` is a no-arg callable
    that constructs the HumanRequirement WITHOUT creating it — the actual
    `manager.create()` happens here, inside the same lock the check used.
    `legacy_match`: see `session_already_carded`. Returns the created
    record, or None when another source already owns this session (for
    this `effect`, or its recognized legacy shape)."""
    if manager is None:
        return None
    with manager.store.locked():
        if session_already_carded(manager, session, by_source=by_source, effect=effect,
                                  legacy_match=legacy_match):
            return None
        return manager.create(build_requirement())
