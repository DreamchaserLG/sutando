"""The core's health row from a command an adapter injects (REMOTE_HEALTH_COMMAND).

Sparrow runs the command with no shell and a timeout, at most once per `ttl` seconds,
and reads one JSON object from its stdout: the heartbeat's `health` row. The broker
refuses a whole heartbeat whose row is malformed, so a row that does not match its
contract (alive: bool|null; motion: idle|moving|unknown; condition:
healthy|abnormal|unknown; reason: a [a-z0-9-]{1,40} slug or null; since: epoch
seconds or null) is dropped and logged, never sent. Empty stdout means no row.
This module names no adapter: what the command measures is the adapter's business."""
from __future__ import annotations

import json
import math
import re
import shlex
import subprocess
import time
from collections.abc import Callable

MOTIONS = ("idle", "moving", "unknown")
CONDITIONS = ("healthy", "abnormal", "unknown")
REASON_RE = re.compile(r"[a-z0-9-]{1,40}")
TIMEOUT_S = 10.0


def _epoch(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def validate(value) -> "tuple[dict | None, str | None]":
    """(row, None) for a row the broker accepts, else (None, why)."""
    if not isinstance(value, dict):
        return None, "not a JSON object"
    row = {k: value.get(k) for k in ("alive", "motion", "condition", "reason", "since")}
    if row["alive"] is not None and not isinstance(row["alive"], bool):
        return None, "alive must be true, false or null"
    if row["motion"] not in MOTIONS:
        return None, f"motion must be one of {', '.join(MOTIONS)}"
    if row["condition"] not in CONDITIONS:
        return None, f"condition must be one of {', '.join(CONDITIONS)}"
    if row["reason"] is not None and not (isinstance(row["reason"], str)
                                          and REASON_RE.fullmatch(row["reason"])):
        return None, "reason must be a [a-z0-9-]{1,40} slug or null"
    if row["since"] is not None and not _epoch(row["since"]):
        return None, "since must be epoch seconds or null"
    return row, None


def run(command: str, timeout: float = TIMEOUT_S,
        env: "dict[str, str] | None" = None) -> "tuple[dict | None, str | None]":
    """(row, None), (None, None) when the command printed nothing, or (None, why)."""
    try:
        r = subprocess.run(shlex.split(command), stdin=subprocess.DEVNULL, env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout:g}s"
    except (OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        return None, f"exited {r.returncode}" + (f": {tail[-1]}" if tail else "")
    out = r.stdout.strip()
    if not out:
        return None, None
    try:
        value = json.loads(out)
    except ValueError:
        return None, "stdout is not JSON"
    return validate(value)


class HealthCommand:
    """`run(command)` reused for `ttl` seconds; logs each new error once. `env`, when
    given, builds the command's whole environment for each run."""

    def __init__(self, command: str, ttl: float, log: Callable[[str], None],
                 env: "Callable[[], dict[str, str]] | None" = None):
        self.command, self.ttl, self.log, self.env = command.strip(), ttl, log, env
        self._at: "float | None" = None
        self._row: "dict | None" = None
        self._error: "str | None" = None

    def __bool__(self) -> bool:
        return bool(self.command)

    def row(self) -> "dict | None":
        if not self.command:
            return None
        if self._at is not None and time.monotonic() - self._at < self.ttl:
            return self._row
        row, error = run(self.command, env=self.env() if self.env else None)
        if error and error != self._error:
            self.log(f"health command gave no row: {error}")
        self._at, self._row, self._error = time.monotonic(), row, error
        return row
