"""Where the proactive drain posts a body, as a dependency-light policy.

The drain and any adapter that queues a proactive file ask here, so an adapter
never accepts a destination the drain would skip."""
from __future__ import annotations

import contextlib
import os
import re
import time

from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

from .outbox import valid_pid
from .result_markers import parse_markers

# Destination FORMAT validation is the bridge's own job ("the bridge
# validates the id format for its platform when applying" — result_markers).
MATRIX_ROOM_RE = re.compile(r"^![^\s:]+:\S+$")
# Where the drain records a confirmed proactive delivery, keyed by the file's stem.
RECEIPT_DIR = ".outbox-ag2space-proactive"
# Where the drain parks a line it will never send, as `<stem>-<epoch>.txt`.
UNDELIVERED_DIR = "undelivered"
# The drain claims a queued `<stem>.txt` by renaming it to `<stem>.sending.<pid>`.
CLAIM_INFIX = ".sending."
# A producer withdraws a queued or claimed `<stem>.txt` by creating `<stem>.withdrawn` under
# send_lock; the drain checks for it, sends and settles under the same lock.
WITHDRAWN_SUFFIX = ".withdrawn"
# A directory of its own, so no reader listing the results dir sees a lock file.
SEND_LOCK_DIR = ".proactive-send-lock"
# The protocol a drain that honours send_lock and the sentinel publishes in its lane record; a
# producer relying on them refuses to act for a live drain without it.
SEND_LOCK_PROTOCOL = 1
# The drain's whole send under the lock: its POST deadline. The longest a producer waits for the
# lock is above it plus settlement; the drain waits briefly and hands the file back.
SEND_DEADLINE_S = 15.0
PRODUCER_LOCK_WAIT_S = 30.0
DRAIN_LOCK_WAIT_S = 2.0
_LOCK_POLL_S = 0.05
# `<stem>` holds how many sends of that proactive file were begun, across restarts.
ATTEMPTS_DIR = ".proactive-attempts"
# An attempts record that cannot be read proves nothing: it counts as attempted beyond any budget.
ATTEMPTS_UNREADABLE = 1 << 20
# Where a parked file's count goes, beside the live one, so a re-queue starts a fresh budget.
PARKED_SUFFIX = ".parked"


def claim_path(queued: Path, pid: int) -> Path:
    """The name the drain renames `queued` to while process `pid` sends it; ValueError for a pid
    outside `valid_pid`, whose name `claim_pid` would reject."""
    if valid_pid(pid) is None:
        raise ValueError(f"pid {pid!r} is outside the pid domain")
    return queued.with_suffix(f"{CLAIM_INFIX}{pid}")


def withdrawn_path(queued: Path) -> Path:
    """The sentinel that withdraws `queued` from the drain before it is sent."""
    return queued.with_name(queued.stem + WITHDRAWN_SUFFIX)


class SendLockUnavailable(OSError):
    """The send lock could not be taken; nothing under it ran."""


@contextlib.contextmanager
def send_lock(result_dir: Path, wait: "float | None" = None):
    """Exclusive lock on the fixed file `result_dir`/SEND_LOCK_DIR/lock, which is never unlinked, so
    every process and restart locks the same inode. The drain holds it from its withdrawal check
    through the send's settlement, and a producer holds it while it withdraws; acquisition is the
    linearization point. Released on every exit; SendLockUnavailable when it cannot be taken
    within `wait` seconds. A platform without flock (no Hatch there) skips it."""
    if fcntl is None:
        yield
        return
    wait = PRODUCER_LOCK_WAIT_S if wait is None else wait
    try:
        lock_dir = Path(result_dir) / SEND_LOCK_DIR
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(lock_dir / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise SendLockUnavailable(str(e)) from e
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SendLockUnavailable(f"held by another sender past {wait:g}s") from None
                time.sleep(_LOCK_POLL_S)
            except OSError as e:
                raise SendLockUnavailable(str(e)) from e
        yield
    finally:
        os.close(fd)


def _attempts_path(result_dir: Path, stem: str) -> Path:
    if not stem or "/" in stem or stem.startswith("."):
        raise ValueError(f"not a proactive file stem: {stem!r}")
    return Path(result_dir) / ATTEMPTS_DIR / stem


def _read_count(path: Path) -> int:
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return ATTEMPTS_UNREADABLE
    return int(text) if text.isdigit() and len(text) < 8 else ATTEMPTS_UNREADABLE


def _write_count(path: Path, count: int) -> None:
    """Temp file, fsync, replace, then the directory; OSError when it is not on disk."""
    path.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, str(count).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    if os.name == "posix":  # a directory fd cannot be fsynced on Windows
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def attempts_begun(result_dir: Path, stem: str) -> int:
    """How many sends of `stem` began since it was last queued (its retry budget's count); 0 for
    none, ATTEMPTS_UNREADABLE when the record exists but cannot be read."""
    return _read_count(_attempts_path(result_dir, stem))


def attempts_ever(result_dir: Path, stem: str) -> int:
    """Every begun send of `stem`, parked budgets included: what a report of its outcome counts."""
    live = attempts_begun(result_dir, stem)
    parked = _read_count(_attempts_path(result_dir, stem).with_name(stem + PARKED_SUFFIX))
    return ATTEMPTS_UNREADABLE if ATTEMPTS_UNREADABLE in (live, parked) else live + parked


def record_attempt(result_dir: Path, stem: str) -> int:
    """Durably count one more begun send of `stem` and return the count; OSError when it is not
    on disk, and then the send must not begin. An unreadable record is kept, never overwritten."""
    begun = attempts_begun(result_dir, stem)
    if begun >= ATTEMPTS_UNREADABLE:
        raise OSError(f"the attempt record of {stem} is unreadable; it is kept, not overwritten")
    _write_count(_attempts_path(result_dir, stem), begun + 1)
    return begun + 1


def park_attempts(result_dir: Path, stem: str) -> None:
    """At a park: move `stem`'s count into its parked record, so a file re-queued later gets a
    fresh budget while attempts_ever still counts what began. An unreadable count stays where it
    is and keeps refusing sends. Best effort: a count left in place only keeps the line parked."""
    live = attempts_begun(result_dir, stem)
    if live == 0 or live >= ATTEMPTS_UNREADABLE:
        return
    path = _attempts_path(result_dir, stem)
    parked = path.with_name(stem + PARKED_SUFFIX)
    with contextlib.suppress(OSError):
        before = _read_count(parked)
        _write_count(parked, live + (0 if before >= ATTEMPTS_UNREADABLE else before))
        path.unlink()


def forget_attempts(result_dir: Path, stem: str) -> None:
    """Drop `stem`'s records, live and parked, once its delivery is confirmed; best effort."""
    with contextlib.suppress(OSError, ValueError):
        path = _attempts_path(result_dir, stem)
        path.unlink(missing_ok=True)
        path.with_name(stem + PARKED_SUFFIX).unlink(missing_ok=True)


def claim_pid(name: str) -> "int | None":
    """The pid of a drain claim name; None for any other name, so no reader treats a
    file the drain neither creates nor recovers as a claim."""
    parts = name.rsplit(CLAIM_INFIX, 1)
    if len(parts) != 2 or not (parts[1].isascii() and parts[1].isdigit()):
        return None
    pid = valid_pid(int(parts[1]))
    # Only the spelling claim_path writes: no leading zeros.
    return pid if pid is not None and str(pid) == parts[1] else None


def claim_base(name: str) -> "str | None":
    """The queued stem a drain claim name came from (the inverse of claim_path); None for any name
    claim_path never writes. `<stem>.sending.x.sending.<pid>` is a claim of `<stem>.sending.x`."""
    if claim_pid(name) is None:
        return None
    return name.rsplit(CLAIM_INFIX, 1)[0]


def route(body: str, skip_after_channel: bool = True) -> "tuple[str, str | None, str]":
    """('send', room_or_None, stripped-body) | ('foreign', None, '') |
    ('drop', None, '').

    Marker grammar comes SOLELY from parse_markers() (no private parser —
    CLAUDE.md result-marker contract); this function only applies the actions
    this transport supports:
      * skip markers   → 'drop' (archive silently, deliver nothing)
      * [dm-only]      → parse_markers already suppressed any redirect, so the
                         body falls through to the default (owner) room
      * [channel: !r:s]→ 'send' to that room, marker stripped
      * [channel: C…/digits] → 'foreign' — that bridge owns the file (review
                         blocker: claiming it here would leak the raw body)
      * attach markers → stripped by the parser; uploads are unsupported on
                         the room-message op, so the actions are ignored
      * [thread: $root]→ stripped here; the send reads it via
                         _proactive_thread_root and posts in that thread
      * bare [thread]  → task results only; stripped and ignored here
    """
    parsed = parse_markers(body, skip_after_channel=skip_after_channel)
    if any(a.kind == "skip" for a in parsed.actions):
        return ("drop", None, "")
    redirect = next((a for a in parsed.actions if a.kind == "redirect"), None)
    if redirect is not None:
        dest = redirect.value
        if MATRIX_ROOM_RE.match(dest):
            return ("send", dest, parsed.body)
        return ("foreign", None, "")
    return ("send", None, parsed.body)


def deliverable_here(room: str, own_homeserver: str) -> bool:
    """A gateway posts only to rooms on its own homeserver. Another lane's room
    must be left for that lane: claiming it here fails at the gateway and the
    retry budget then parks deliverable work as undeliverable."""
    if not own_homeserver:
        return True  # identity unknown: today's behaviour, deliberately
    if ":" not in room:
        return False  # names no server: deliverable nowhere, strands visibly
    return room.split(":", 1)[1] == own_homeserver


def homeserver_of(mxid: str) -> str:
    """The server part of `@user:server`; FIRST colon, since it may carry a port or IPv6."""
    return mxid.split(":", 1)[1] if mxid.startswith("@") and ":" in mxid else ""


def drain_room(body: str, own_homeserver: str) -> "str | None":
    """The room the drain would post `body` to, or None when it would not post it to a room
    it names: dropped, another bridge's, unaddressed, or on another homeserver."""
    kind, room, _ = route(body)
    if kind != "send" or room is None or not deliverable_here(room, own_homeserver):
        return None
    return room
