"""The AG2 Space agent a bridge serves, and the one path segment a per-agent work root is named by.

`resolve` is the bridge's identity chain, in its order: the process env (`AGENT_MXID`, `AGENT_ID`),
then the channel .env files (`AGENT_MXID`, `AGENT_ID`, `AG2SPACE_USER_ID`), then the durable binding
`<state>/auth/ag2space.json`'s `agent_id`. The gateway bridge and the installers read it here, so an
install derives its root from exactly the identity the bridge will report. A .env file's lines
are parsed by the credential owner, gateway_credential_sources. Stdlib only.

`publish_lane` is the one writer of `<state>/auth/lane-identity[.<instance>].json`: the id the live
drain of that gateway instance judges rooms by, with its instance, pid and a timestamp, never a
credential. `published_lane_record` reads it back; `retract_lane` invalidates it. All three hold the
instance's `lane-identity[.<instance>].lock` beside it, and a record that cannot be removed is
tombstoned or emptied, so a failed invalidation reads as missing rather than stale.

`root_segment` maps an id to one path segment, lowercase `[a-z0-9._-]`, no leading dot, at most
`SEGMENT_MAX` bytes. A plain MXID `@local:server` whose parts are lowercase `[a-z0-9.-]` (first
character alphanumeric) becomes `local_server`: exactly one `_`, so the id reads back from it. Any
other id becomes a readable prefix plus `__` and 12 hex of its sha256, so it never equals a plain
one (which has no `__`) and two such ids differ by their digest.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path

from . import gateway_credential_sources as _sources
from .outbox import valid_pid
from .gateway_instance import suffix as _instance_suffix

ENV_KEYS = ("AGENT_MXID", "AGENT_ID")
FILE_KEYS = ("AGENT_MXID", "AGENT_ID", "AG2SPACE_USER_ID")
SEGMENT_MAX = 64
DIGEST_HEX = 12
_PLAIN = re.compile(r"@([a-z0-9][a-z0-9.-]*):([a-z0-9][a-z0-9.-]*)")
_UNSAFE = re.compile(r"[^a-z0-9._-]")


# Parsing has one owner, so the identity tiers cannot disagree with the credential tiers.
unquote_env = _sources.unquote
file_value = _sources.config_from_files


def env_files(paths) -> list:
    """Readable .env files in the given order, as [(path, {key: value})]; unreadable ones are skipped."""
    out = []
    for path in paths:
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        out.append((path, _sources.parse_env_text(text)))
    return out


def enrolled(state_dir) -> str:
    """The binding's `agent_id`, '' when absent, unreadable, malformed or not a string."""
    if state_dir is None:
        return ""
    try:
        rec = json.loads((Path(state_dir) / "auth" / "ag2space.json").read_text())
        v = rec.get("agent_id")
        return v.strip() if isinstance(v, str) else ""
    except Exception:
        return ""


try:
    import fcntl
except ImportError:  # Windows: no lane record; Hatch, the only reader, is POSIX-only
    fcntl = None
LANE_SUPPORTED = fcntl is not None
_LANE_MAX_BYTES = 4096


def _lane_name(instance: str) -> str:
    return f"lane-identity{_instance_suffix(instance)}.json"


def _auth_dir(state_dir, create: bool) -> "int | None":
    """An fd on `<state>/auth`, opened without following a symlink; None when it is absent."""
    if not LANE_SUPPORTED:
        raise OSError("no lane record on this platform")
    path = Path(state_dir) / "auth"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} is a symlink or not a directory")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if os.fstat(fd).st_ino != st.st_ino:
        os.close(fd)
        raise OSError(f"{path} changed while it was opened")
    return fd


@contextlib.contextmanager
def _lane_locked(dfd: int, instance: str):
    """Exclusive lock on the instance's own `.lock` beside its record: every publish, retraction and
    read of that record is serialized, so a check and the write or unlink after it are one step."""
    name = _lane_name(instance)[:-len(".json")] + ".lock"
    for attempt in range(5):
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            break
        except FileNotFoundError:  # APFS: racing O_CREATs of one name can see ENOENT
            if attempt == 4:
                raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_at(dfd: int, name: str) -> "dict | None":
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, _LANE_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _write_at(dfd: int, name: str, record: dict) -> None:
    """Write `record` to a fresh temp, fsync it, then replace `name`; OSError on failure."""
    tmp = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
    try:
        try:
            os.write(fd, json.dumps(record).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dfd)


def _invalidate_at(dfd: int, name: str, instance: str) -> None:
    """Make the record read as missing: unlink it, else replace it with a tombstone, else empty it
    in place (which needs no free space); OSError only when all three fail."""
    try:
        os.unlink(name, dir_fd=dfd)
        return
    except FileNotFoundError:
        return
    except OSError:
        pass
    try:
        _write_at(dfd, name, {"instance": instance or "", "retracted": True})
        return
    except OSError:
        pass
    fd = os.open(name, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=dfd)
    os.close(fd)


def _valid_lane(rec: "dict | None", instance: str) -> "dict | None":
    if not isinstance(rec, dict) or rec.get("instance") != (instance or "") or rec.get("retracted"):
        return None
    pid, agent, ts = rec.get("pid"), rec.get("agent_id"), rec.get("ts")
    if not isinstance(agent, str) or valid_pid(pid) is None \
            or not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    return {**rec, "agent_id": agent.strip()}


def publish_lane(state_dir, instance: str, agent_id: str) -> dict:
    """Record, under the lane lock, the identity the live drain of `instance` resolved, so a reader
    that must not open the credential env sees that drain's lane; returns the record written.
    It holds the id, instance, pid and a timestamp only. On a failed write the old record is
    invalidated in the same locked step, and OSError is raised either way."""
    if valid_pid(os.getpid()) is None:
        raise OSError(f"pid {os.getpid()} is outside the pid domain readers accept")
    record = {"agent_id": (agent_id or "").strip(), "instance": instance or "",
              "pid": os.getpid(), "ts": time.time()}
    dfd = _auth_dir(state_dir, create=True)
    try:
        with _lane_locked(dfd, instance):
            try:
                _write_at(dfd, _lane_name(instance), record)
            except OSError:
                _invalidate_at(dfd, _lane_name(instance), instance)
                raise
    finally:
        os.close(dfd)
    return record


def retract_lane(state_dir, instance: str, pid: "int | None" = None, ts: "float | None" = None) -> None:
    """Invalidate `instance`'s record; with `pid` (and `ts`), only the record that publisher wrote,
    checked and removed under the lane lock. Never raises."""
    with contextlib.suppress(OSError):
        dfd = _auth_dir(state_dir, create=False)
        if dfd is None:
            return
        try:
            with _lane_locked(dfd, instance):
                if pid is not None:
                    rec = _valid_lane(_read_at(dfd, _lane_name(instance)), instance) or {}
                    if rec.get("pid") != pid or (ts is not None and rec.get("ts") != ts):
                        return
                _invalidate_at(dfd, _lane_name(instance), instance)
        finally:
            os.close(dfd)


def published_lane_record(state_dir, instance: str) -> "dict | None":
    """`instance`'s lane record, read under the lane lock, when well formed, not retracted and its
    own (agent_id str, instance equal, a valid_pid, numeric ts); None otherwise. Whether the
    pid is still the live drain is the reader's check."""
    if state_dir is None:
        return None
    try:
        dfd = _auth_dir(state_dir, create=False)
        if dfd is None:
            return None
        try:
            with _lane_locked(dfd, instance):
                rec = _read_at(dfd, _lane_name(instance))
        finally:
            os.close(dfd)
    except OSError:
        return None
    return _valid_lane(rec, instance)


def resolve(state_dir, channel_value, environ=None) -> str:
    """The agent id by the bridge's chain; `channel_value(key)` reads the channel .env tier. '' = unknown."""
    env = os.environ if environ is None else environ
    for key in ENV_KEYS:
        v = (env.get(key) or "").strip()
        if v:
            return v
    for key in FILE_KEYS:
        v = (channel_value(key) or "").strip()
        if v:
            return v
    return enrolled(state_dir)


def root_segment(agent_id: str) -> str:
    """The one path segment for `agent_id`; ValueError on an empty id."""
    raw = (agent_id or "").strip()
    if not raw:
        raise ValueError("the agent id is empty")
    m = _PLAIN.fullmatch(raw)
    if m:
        plain = f"{m.group(1)}_{m.group(2)}"
        if len(plain) <= SEGMENT_MAX:
            return plain
    readable = _UNSAFE.sub("-", raw.lower().lstrip("@").replace(":", "_")).lstrip("._-")
    readable = readable[:SEGMENT_MAX - DIGEST_HEX - 2].rstrip("._-") or "agent"
    return f"{readable}__{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:DIGEST_HEX]}"
