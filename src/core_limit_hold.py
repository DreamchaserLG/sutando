#!/usr/bin/env python3
"""Hold the core's relaunch while its provider refuses it for a limit.

A core that dies on a usage limit, rate limit or quota comes straight back to
the same refusal, so a supervisor that revives it on sight loops. This module
is the one decision. It reads two records of the refusal: the provider's, which
the credential proxy keeps in `state/quota-state.json`, and the CLI's own last
API error, from the core's session transcript. From them it classifies a limit,
records a hold in `state/core-limit-hold.json`, and mirrors the reason into
`core-status.json`. The hold lasts until the reset time the provider or the CLI
gave, else a bounded backoff.

Evidence never re-arms a hold it already caused, so the try after a hold always
happens: a cleared limit resumes on its own, and a limit that is still there
leaves fresh evidence and a longer hold. A crash with no limit evidence is
never held, so it is revived as before.

    core_limit_hold.py gate [--cwd DIR]   exit 0: launch; exit 75: held, reason on stdout
    core_limit_hold.py status             the hold record, if any
    core_limit_hold.py clear              lift the hold now (the next revive launches)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_wedge  # noqa: E402
import quota_availability as qa  # noqa: E402

from workspace_default import resolve_workspace, status_read_path, write_status  # noqa: E402

HOLD_FILE = "core-limit-hold.json"
HOLD_EXIT = 75  # EX_TEMPFAIL: not a failure, try again later
EVIDENCE_WINDOW_S = 15 * 60
BACKOFF_BASE_S = 5 * 60
BACKOFF_CAP_S = 60 * 60
RESET_GRACE_S = 60
MAX_RESET_WAIT_S = 8 * 24 * 3600
STRIKE_MEMORY_S = 2 * BACKOFF_CAP_S
TRANSCRIPT_TAIL_BYTES = 256 * 1024
# The 429's own headers land a moment after it; only a later response says the provider served.
SERVED_SLACK_S = 5

_CLI_LIMIT = tuple(rx for name, rx in cli_wedge.ABNORMAL_PATTERNS
                   if name in cli_wedge.PROVIDER_LIMIT_PATTERNS)
# The window-named banner ("5-hour limit reached") and the API's own words for a refusal.
_USAGE_EXTRA = re.compile(r"\b\d+-hour limit (?:reached|exceeded|hit)\b|\bquota\b.{0,20}\b(?:exceeded|exhausted)\b"
                          r"|\binsufficient_quota\b", re.I)
_RATE = re.compile(r"\brate_limit_error\b|\brate[ -]?limit(?:ed)?\b|\b429\b|\btoo many requests\b", re.I)

_RESET_EPOCH = re.compile(r"\|(\d{10})\b")
_RESET_CLOCK = re.compile(r"\bresets?\s+(?:at\s+|on\s+)?(?:([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b(?:\s*\(([A-Za-z_]+(?:/[A-Za-z0-9_+-]+)*)\))?", re.I)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_RESET_IN = re.compile(r"\bresets?\s+in\s+(\d+)\s*(h|hours?|m|mins?|minutes?)\b", re.I)
_RETRY_AFTER = re.compile(r"\bretry[- ]after\D{0,3}(\d+)\b", re.I)


@dataclass(frozen=True)
class Limit:
    """One observed refusal: `kind` usage-limit or rate-limit, `at` when it was seen,
    `reset_at` when the provider or CLI said it lifts, `source` which record said so."""
    kind: str
    at: float
    reset_at: Optional[float]
    model: Optional[str]
    source: str


def _zone(name: Optional[str]):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 -- an unknown zone reads as local time
        return None


def parse_reset(text: str, at: float) -> Optional[float]:
    """The lift time a refusal message states, as epoch seconds after `at`, else None."""
    m = _RESET_EPOCH.search(text)
    if m:
        return float(m.group(1))
    m = _RESET_CLOCK.search(text)
    if m:
        month, day, hour, minute, ampm, zone = m.groups()
        hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
        tz = _zone(zone)
        base = datetime.fromtimestamp(at, tz) if tz else datetime.fromtimestamp(at)
        when = base.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
        if month:
            try:
                when = when.replace(month=_MONTHS.index(month.lower()) + 1, day=int(day))
            except ValueError:
                return None
            if when.timestamp() <= at:
                when = when.replace(year=when.year + 1)
        elif when.timestamp() <= at:
            when += timedelta(days=1)
        return when.timestamp()
    m = _RESET_IN.search(text)
    if m:
        unit = 3600 if m.group(2).lower().startswith("h") else 60
        return at + int(m.group(1)) * unit
    m = _RETRY_AFTER.search(text)
    if m:
        return at + int(m.group(1))
    return None


def classify_text(text: str, at: float, model: Optional[str] = None,
                  source: str = "cli-transcript") -> Optional[Limit]:
    """A provider limit named by refusal text, or None for any other error."""
    if not text:
        return None
    if any(rx.search(text) for rx in _CLI_LIMIT) or _USAGE_EXTRA.search(text):
        kind = "usage-limit"
    elif _RATE.search(text):
        kind = "rate-limit"
    else:
        return None
    return Limit(kind, at, parse_reset(text, at), model, source)


def _record_reset(headers: dict, now: float) -> Optional[float]:
    """The latest reset among the windows the provider reports rejected, if any is ahead."""
    resets = []
    for window, (_u, status) in qa.limit_windows(headers).items():
        if status == "rejected":
            resets.append(headers.get(f"anthropic-ratelimit-unified-{window}-reset"))
    if headers.get("anthropic-ratelimit-unified-status") == "rejected":
        resets.append(headers.get("anthropic-ratelimit-unified-reset"))
    ahead = []
    for value in resets:
        try:
            when = float(value)
        except (TypeError, ValueError):
            continue
        if when > now:
            ahead.append(when)
    return max(ahead) if ahead else None


def record_evidence(payload, now: float) -> Optional[Limit]:
    """A recent 429 in the proxy's record, unless the provider served since then."""
    if not isinstance(payload, dict):
        return None
    ledger = payload.get("recent_rejections")
    newest = None
    for rej in ledger if isinstance(ledger, list) else []:
        if not isinstance(rej, dict) or rej.get("status") != 429:
            continue
        at = qa._parse_when(rej.get("ts"))
        if at is None or not (0 <= now - at <= EVIDENCE_WINDOW_S):
            continue
        if newest is None or at > newest[0]:
            newest = (at, rej)
    if newest is None:
        return None
    at, rej = newest
    state = qa.classify_limit_state(payload)
    checked = qa._parse_when(payload.get("last_checked"))
    if state != "rejected" and checked is not None and checked > at + SERVED_SLACK_S:
        return None
    headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
    model = qa._norm_model(rej.get("model")) or qa.record_model(payload)
    found = classify_text(str(rej.get("snippet") or ""), at, model, "provider-record")
    kind = "usage-limit" if state == "rejected" else (found.kind if found else "rate-limit")
    reset = _record_reset(headers, now) if state == "rejected" else None
    if reset is None and found is not None and found.reset_at and found.reset_at > now:
        reset = found.reset_at
    return Limit(kind, at, reset, model, "provider-record")


def _entry_text(entry: dict) -> str:
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    parts = [c.get("text", "") for c in content if isinstance(c, dict)] if isinstance(content, list) else []
    return " ".join(p for p in parts if isinstance(p, str))


def transcript_evidence(projects_dir: Optional[Path], now: float) -> Optional[Limit]:
    """The CLI's own last API error in the core's newest transcript, when it names a limit."""
    if projects_dir is None or not projects_dir.is_dir():
        return None
    try:
        files = sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    except OSError:
        return None
    for path in files:
        try:
            if now - path.stat().st_mtime > EVIDENCE_WINDOW_S:
                continue
            with open(path, "rb") as fh:
                fh.seek(max(0, path.stat().st_size - TRANSCRIPT_TAIL_BYTES))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or not entry.get("isApiErrorMessage"):
                continue
            at = qa._parse_when(entry.get("timestamp"))
            if at is None or not (0 <= now - at <= EVIDENCE_WINDOW_S):
                break
            msg = entry.get("message")
            model = qa._norm_model(msg.get("model")) if isinstance(msg, dict) else None
            return classify_text(_entry_text(entry), at, None if model == "<synthetic>" else model)
    return None


# ---- the hold record ----------------------------------------------------------

def read_hold(workspace: Path) -> Optional[dict]:
    try:
        data = json.loads(status_read_path(HOLD_FILE, Path(workspace)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def active_hold(workspace: Path, now: Optional[float] = None) -> Optional[dict]:
    """The hold in force right now, else None. Read-only."""
    hold = read_hold(workspace)
    at = time.time() if now is None else now
    if hold and hold.get("status") == "held" and isinstance(hold.get("until"), (int, float)) and at < hold["until"]:
        return hold
    return None


def _when(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(epoch))


def status_line(hold: dict) -> str:
    """The reason in the words a user reads instead of "Connecting"."""
    what = "usage limit" if hold.get("kind") == "usage-limit" else "rate limit"
    model = hold.get("model") or "its model"
    if isinstance(hold.get("reset_at"), (int, float)):
        tail = f"resets {_when(hold['reset_at'])}"
    else:
        tail = f"retrying {_when(hold['until'])}" if isinstance(hold.get("until"), (int, float)) else "retrying later"
    return f"core paused: {what} on {model}, {tail}"


def _write_hold(workspace: Path, hold: dict) -> None:
    write_status(HOLD_FILE, hold, workspace=Path(workspace))


def _mirror_core_status(workspace: Path, status: str, step: str, now: float) -> None:
    try:
        write_status("core-status.json", {"status": status, "step": step, "ts": int(now)},
                     workspace=Path(workspace))
    except OSError:
        pass  # the hold record is the decision; the mirror only shows it


def _relevant(limit: Optional[Limit], launch_model: Optional[str]) -> bool:
    """A limit on another model family does not stop a core that runs this one."""
    if limit is None:
        return False
    mine, theirs = qa._model_family(launch_model), qa._model_family(limit.model)
    return not (mine and theirs and mine != theirs)


def _hold_until(limit: Limit, strikes: int, now: float) -> float:
    if limit.reset_at and now < limit.reset_at <= now + MAX_RESET_WAIT_S:
        return limit.reset_at + RESET_GRACE_S
    return now + min(BACKOFF_BASE_S * 2 ** (strikes - 1), BACKOFF_CAP_S)


@dataclass(frozen=True)
class Decision:
    launch: bool
    hold: Optional[dict]
    line: str


def decide(workspace: Path, now: Optional[float] = None, *, quota=None,
           projects_dir: Optional[Path] = None, launch_model: Optional[str] = None) -> Decision:
    """Launch, or hold and say why. The one writer of the hold record."""
    now = time.time() if now is None else now
    workspace = Path(workspace)
    hold = read_hold(workspace)
    if active_hold(workspace, now):
        return Decision(False, hold, status_line(hold))
    if quota is None:
        rec = qa.read_quota_record(workspace, now)
        quota = rec.payload if rec else None
    found = [lim for lim in (record_evidence(quota, now), transcript_evidence(projects_dir, now))
             if _relevant(lim, launch_model)]
    limit = max(found, key=lambda lim: lim.at) if found else None
    seen = hold.get("evidence_at") if hold else None
    if limit is not None and (not isinstance(seen, (int, float)) or limit.at > seen):
        last_end = hold.get("until") if hold else None
        recent = isinstance(last_end, (int, float)) and now - last_end < STRIKE_MEMORY_S
        strikes = int(hold.get("strikes") or 0) + 1 if hold and recent else 1
        new = {
            "status": "held", "kind": limit.kind, "model": limit.model, "source": limit.source,
            "reset_at": limit.reset_at, "evidence_at": limit.at, "set_at": now,
            "until": _hold_until(limit, strikes, now), "strikes": strikes,
        }
        new["reason"] = status_line(new)
        _write_hold(workspace, new)
        _mirror_core_status(workspace, "paused", new["reason"], now)
        return Decision(False, new, new["reason"])
    if hold and hold.get("status") == "held":
        hold = dict(hold, status="released", released_at=now)
        _write_hold(workspace, hold)
        _mirror_core_status(workspace, "starting", "resuming after a provider limit", now)
    return Decision(True, hold, "")


def clear(workspace: Path, now: Optional[float] = None) -> Optional[dict]:
    """Lift the hold. Its evidence stays recorded, so only a new refusal re-arms it."""
    hold = read_hold(workspace)
    if not hold or hold.get("status") != "held":
        return hold
    hold = dict(hold, status="cleared", released_at=time.time() if now is None else now)
    _write_hold(workspace, hold)
    return hold


def _projects_dir(cwd: Optional[str]) -> Optional[Path]:
    if not cwd:
        return None
    from util_paths import claude_home_path, claude_project_slug
    return claude_home_path("projects", claude_project_slug(cwd))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    gate = sub.add_parser("gate", help="decide whether the core may launch now")
    gate.add_argument("--cwd", default="", help="the core's working directory (finds its transcripts)")
    sub.add_parser("status", help="print the hold record")
    sub.add_parser("clear", help="lift the hold now")
    args = ap.parse_args(argv)
    workspace = resolve_workspace()
    if args.cmd == "status":
        hold = read_hold(workspace)
        print(json.dumps(hold or {}, indent=2))
        return 0
    if args.cmd == "clear":
        hold = clear(workspace)
        print("hold lifted" if hold and hold.get("status") == "cleared" else "no hold in force")
        return 0
    model = qa.seat_model(qa.SeatEnv(True, None, os.environ.get("CLAUDE_CONFIG_DIR")), workspace)
    decision = decide(workspace, projects_dir=_projects_dir(args.cwd), launch_model=model)
    if decision.launch:
        return 0
    print(decision.line)
    return HOLD_EXIT


if __name__ == "__main__":
    sys.exit(main())
