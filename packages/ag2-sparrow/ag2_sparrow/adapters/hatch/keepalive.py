"""Keepalive for a Hatch agent: one scheduled task, and the health row Sparrow reports.

Every 15 minutes Hatch's scheduler runs `keepalive keepawake` (the run is what keeps
the agent awake). It restarts Sparrow if Sparrow is not running and appends one line
to the keepalive log: `<utc> keepawake listener=up|down restarted=0|1`. A scheduler
that stops running the task cannot report that itself, so Sparrow does: it runs
`keepalive health` (REMOTE_HEALTH_COMMAND, which install puts in Sparrow's
environment) and sends the row on its heartbeat. The row is healthy while the last
keepawake line is under two intervals old, unknown while a task that has not run yet
is that young, else abnormal (`keepawake-not-running`).
`keepalive report` lists the log's gaps, and reads the lines of the earlier
watchdog-and-probe pair too."""
from __future__ import annotations

import datetime
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from ... import _dirs
from . import cli as _cli

TASK, INTERVAL_MIN = "sparrow-keepawake", 15
# Tasks an earlier install wrote; re-running install removes them.
OLD_TASKS = ("sparrow-watchdog", "keepalive-probe")
TASK_MARKER = "<!-- Written by ag2-sparrow-hatch install; re-run install to update it. -->"
STALE_S = 2 * INTERVAL_MIN * 60
NOT_RUNNING = "keepawake-not-running"
RUN_TIMEOUT_S = 120
GAP_MINUTES = 16
# How long a spawned Sparrow must stay up to count as started (75 = lock held elsewhere).
SPAWN_SETTLE_S = 3.0
LOG_NAME = "keepalive-probe.log"
TAIL_BYTES = 4096


def default_cron_dir() -> Path:
    return _cli._home() / "workspace" / "cron.d" / "minutely"


def cron_dir(cfg: dict) -> Path:
    return Path(cfg["CRON_DIR"]) if cfg.get("CRON_DIR") else default_cron_dir()


def log_path(cfg: dict, override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    return Path(cfg["KEEPALIVE_LOG"]) if cfg.get("KEEPALIVE_LOG") else _cli._home() / LOG_NAME


def _state_dir(cfg: dict) -> Path:
    return Path(cfg.get("STATE_DIR") or _dirs.state_dir())


def _activity_file(cfg: dict) -> Path:
    return Path(cfg.get("ACTIVITY_FILE") or _cli._home() / ".muse" / "last_active")


def task_file(name: str = TASK, minutes: int = INTERVAL_MIN) -> str:
    return f"{name}__interval@{minutes}m.md"


def health_command(command: str) -> str:
    """REMOTE_HEALTH_COMMAND for Sparrow: this adapter's health row."""
    return shlex.join([command, "keepalive", "health"])


def render_task(command: str) -> str:
    return (f"{TASK_MARKER}\n# {TASK} (every {INTERVAL_MIN} minutes)\n\n"
            "Run this command once, then stop. It keeps this agent awake, restarts the AG2 "
            "Space listener (Sparrow) only if it is down, and appends one line to the "
            "keepalive log. Change nothing else, retry nothing, and post nothing anywhere.\n\n"
            f"```sh\ntimeout {RUN_TIMEOUT_S} {command} keepalive keepawake\n```\n")


def _command(cfg: dict) -> str:
    return cfg.get("REPLY_COMMAND") or _cli.find_reply_command() or _cli.REPLY_COMMAND


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _parse_utc(text: str) -> float | None:
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _instance(cfg: dict) -> str:
    return (cfg["GATEWAY_INSTANCE"] if "GATEWAY_INSTANCE" in cfg
            else os.environ.get("GATEWAY_INSTANCE") or "").strip()


def listener_up(cfg: dict, now: float) -> bool:
    """Running, connected or not: a rejected token is not fixed by a restart."""
    code, _ = _cli.check_sparrow(_state_dir(cfg), now=now, instance=_instance(cfg))
    return code != _cli.SPARROW_DOWN


def _found(directory: Path, name: str) -> "list[Path]":
    found = sorted(directory.glob(f"{name}__*.md"))
    return found + ([directory / f"{name}.md"] if (directory / f"{name}.md").exists() else [])


def _ours(path: Path) -> bool:
    return path.read_text(encoding="utf-8", errors="replace").startswith(TASK_MARKER)


def _install_task(directory: Path, text: str, force: bool) -> bool:
    """Write the task; a same-named task install did not write is kept unless `force`.
    False = kept, ours not installed."""
    target = directory / task_file()
    others = [p for p in _found(directory, TASK) if p != target]
    foreign = [p for p in [target, *others] if p.exists() and not _ours(p)]
    if foreign and not force:
        for p in foreign:
            print(f"NOTE keepalive: {p} was not written by install (a hand-made {TASK}?), so "
                  f"it was kept and {target.name} was not installed; re-run with --force to "
                  "replace it")
        return False
    for p in others:
        p.unlink()
        print(f"replaced: {p}" + (" (hand-made)" if p in foreign else ""))
    if target in foreign:
        print(f"replaced: {target} (hand-made)")
        _cli._write(target, text, 0o644)
        return True
    return _cli._install_file(target, text, 0o644, refuse_drift=False, force=True)


def _remove_old_tasks(directory: Path, force: bool) -> None:
    for name in OLD_TASKS:
        for p in _found(directory, name):
            ours = _ours(p)
            if ours or force:
                p.unlink()
                print(f"removed: {p} (replaced by {TASK})" + ("" if ours else " (hand-made)"))
            else:
                print(f"NOTE keepalive: {p} was not written by install, so it was kept; "
                      f"{TASK} replaces it, so remove it, or re-run with --force")


def install_tasks(cfg: dict, force: bool) -> None:
    """Install the keepawake task and remove the ones it replaces. Never fails the install."""
    directory = cron_dir(cfg)
    if not directory.is_dir():
        print(f"NOTE keepalive: {directory} did not exist and was created; check that Hatch's "
              "scheduler runs the tasks in it")
    if _install_task(directory, render_task(_command(cfg)), force):
        print(f"keepalive: {TASK} scheduled every {INTERVAL_MIN} minutes; its log is "
              f"{log_path(cfg)}")
    _remove_old_tasks(directory, force)


def uninstall_tasks(cfg: dict) -> int:
    directory, rc = cron_dir(cfg), 0
    found = [p for name in (TASK, *OLD_TASKS) for p in _found(directory, name)]
    if not found:
        print(f"nothing to remove: no {TASK} task in {directory}")
    for p in found:
        if _ours(p):
            p.unlink()
            print(f"removed: {p}")
        else:
            print(f"error: {p} was not written by install; remove it yourself", file=sys.stderr)
            rc = 1
    for name in ("hatch-keepalive.json", "hatch-keepalive-probe.json"):
        (_state_dir(cfg) / name).unlink(missing_ok=True)
    print(f"The keepalive log {log_path(cfg)} was kept.")
    return rc


def _spawn_sparrow(cfg: dict) -> "tuple[bool, str]":
    exe = _cli._find_console_script(_cli.SPARROW_COMMAND)
    if not exe:
        return False, f"{_cli.SPARROW_COMMAND} not found beside {sys.executable} or on PATH"
    env = dict(os.environ, AG2_DEVICE_ENV=str(_cli.device_env_path()),
               REMOTE_HEALTH_COMMAND=health_command(_command(cfg)))
    for key, var in (("TASK_DIR", "AGENT_CONNECT_TASK_DIR"),
                     ("RESULT_DIR", "AGENT_CONNECT_RESULT_DIR"),
                     ("STATE_DIR", "AGENT_CONNECT_STATE_DIR")):
        if cfg.get(key):
            env[var] = cfg[key]
    if _instance(cfg):
        env["GATEWAY_INSTANCE"] = _instance(cfg)
    out = _state_dir(cfg) / "hatch-keepalive-sparrow.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("ab") as fh:
        proc = subprocess.Popen([exe], env=env, stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)
    try:
        code = proc.wait(timeout=SPAWN_SETTLE_S)
    except subprocess.TimeoutExpired:
        return True, f"started {exe} (pid {proc.pid}, output in {out})"
    why = "another Sparrow holds the lock" if code == 75 else f"see {out}"
    return False, f"{exe} exited {code} at once ({why})"


def restart_listener(cfg: dict) -> "tuple[bool, str]":
    """Through the user service install wrote when there is one, else a detached Sparrow."""
    unit = _cli.service_path()
    ours = unit.is_file() and unit.read_text(encoding="utf-8", errors="replace") \
        .startswith(_cli.SERVICE_MARKER)
    if ours and _cli.systemd_user_available():
        # A unit left failed by its start limit stays down until reset.
        _cli._systemd_run(["systemctl", "--user", "reset-failed", _cli.SERVICE_NAME])
        r = _cli._systemd_run(["systemctl", "--user", "restart", _cli.SERVICE_NAME])
        if _cli._ok(r):
            return True, f"systemctl --user restart {_cli.SERVICE_NAME}"
        return False, f"systemctl --user restart {_cli.SERVICE_NAME} failed ({_cli._why(r)})"
    return _spawn_sparrow(cfg)


def keepawake(cfg: dict, log: Path, now: float | None = None) -> int:
    now = time.time() if now is None else now
    up, restarted, rc = listener_up(cfg, now), 0, 0
    if not up:
        ok, how = restart_listener(cfg)
        if ok:
            restarted = 1
            print(f"listener was down; restarted: {how}")
        else:
            print(f"listener is down and the restart failed: {how}", file=sys.stderr)
            rc = 1
    line = f"{_utc(now)} keepawake listener={'up' if up else 'down'} restarted={restarted}"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line)
    return rc


def last_keepawake(log: Path) -> float | None:
    """The time of the log's last keepawake line, or None."""
    try:
        with log.open("rb") as fh:
            fh.seek(max(0, fh.seek(0, os.SEEK_END) - TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        parts = line.split()
        if len(parts) > 1 and parts[1] == "keepawake":
            ts = _parse_utc(parts[0])
            if ts is not None:
                return ts
    return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _within(now: float, ts: "float | None") -> bool:
    """`ts` at most STALE_S old; one from the future (a clock stepped back) is not."""
    return ts is not None and 0 <= now - ts <= STALE_S


def health_row(cfg: dict, log: Path, now: float | None = None) -> dict | None:
    """The core row for Sparrow's heartbeat; None while no keepawake task is installed.
    `alive` also counts the hook's activity marker."""
    now = time.time() if now is None else now
    tasks = _found(cron_dir(cfg), TASK)
    if not tasks:
        return None
    ran = last_keepawake(log)
    # Install leaves an unchanged task's mtime alone, so a task that never ran ages from it.
    last = ran if ran is not None else max(
        (m for m in map(_mtime, tasks) if m is not None), default=now)
    fresh = _within(now, last)
    alive = True if (fresh and ran is not None) or _within(now, _mtime(_activity_file(cfg))) \
        else None
    # Fresh but never run: no evidence either way yet.
    condition = "abnormal" if not fresh else "healthy" if ran is not None else "unknown"
    return {"alive": alive, "motion": "unknown", "condition": condition,
            "reason": NOT_RUNNING if condition == "abnormal" else None,
            "since": last + STALE_S if condition == "abnormal" else None}


def status_line(cfg: dict, now: float | None = None) -> "tuple[str, dict | None]":
    """What keepalive status prints, and the health row. Observes only."""
    now = time.time() if now is None else now
    log = log_path(cfg)
    last, row = last_keepawake(log), health_row(cfg, log, now)
    health = ("none" if row is None else row["condition"]
              + (f" ({row['reason']})" if row["reason"] else ""))
    return (f"{_utc(now)} listener={'up' if listener_up(cfg, now) else 'down'} "
            f"last_keepawake={_utc(last) if last is not None else 'never'} health={health}", row)


def report(log: Path, gap_minutes: float = GAP_MINUTES) -> int:
    """Print the log's gaps over `gap_minutes`, its listener-down lines and restarts."""
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        print(f"error: cannot read {log}: {e}", file=sys.stderr)
        return 1
    rows, unreadable = [], 0
    for line in lines:
        parts = line.split()
        ts = _parse_utc(parts[0]) if parts else None
        if ts is None:
            unreadable += bool(parts)
            continue
        rows.append((ts, dict(p.split("=", 1) for p in parts[1:] if "=" in p), parts[1:]))
    if not rows:
        print(f"{log}: no keepalive lines")
        return 0
    rows.sort(key=lambda r: r[0])
    print(f"{log}: {len(rows)} lines, {_utc(rows[0][0])} to {_utc(rows[-1][0])}"
          + (f" ({unreadable} unreadable lines skipped)" if unreadable else ""))
    gaps = [(a[0], b[0]) for a, b in zip(rows, rows[1:]) if b[0] - a[0] > gap_minutes * 60]
    print(f"gaps over {gap_minutes:g} min: {len(gaps)}"
          + (" (the scheduler or the VM did not run)" if gaps else ""))
    for start, end in gaps:
        print(f"  {_utc(start)} -> {_utc(end)}  {round((end - start) / 60)} min")
    restarts = 0
    for _ts, fields, _words in rows:
        for key in ("restarted", "listener_restarts"):
            try:
                restarts += int(fields.get(key, 0))
            except ValueError:
                pass
    print(f"listener down at a run: {sum(f.get('listener') == 'down' for _, f, _ in rows)}")
    print(f"listener restarts: {restarts}")
    old = [(f, w) for _, f, w in rows if "watchdog" in f]
    if old:
        print(f"earlier probe lines: {len(old)}; watchdog down at one: "
              f"{sum(f['watchdog'] == 'down' for f, _ in old)}; restarted by the probe: "
              f"{sum('watchdog_restarted' in w for _, w in old)}")
    return 0


def main(args) -> int:
    cfg = _cli._installed_config()
    log = log_path(cfg, args.log)
    if args.action == "keepawake":
        return keepawake(cfg, log)
    if args.action == "health":
        row = health_row(cfg, log)
        if row is not None:
            print(json.dumps(row, sort_keys=True))
        return 0
    if args.action == "status":
        print(status_line(cfg)[0])
        return 0
    return report(log, args.gap_minutes)
