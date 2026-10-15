"""Keepalive for a Hatch agent: a listener watchdog and a probe, run as Hatch scheduled tasks.

The watchdog (every 5 minutes) restarts Sparrow when its status file says it is not
running. The probe (every 15 minutes) appends one line to the keepalive log: the UTC
time, whether the listener is up (observed, never restarted), whether the watchdog is
up, and how many times it restarted the listener since the previous line. A watchdog
that stopped running is restarted by the probe, which marks the line
`watchdog_restarted`. Gaps in the log longer than ~16 minutes are times the
scheduler, or the VM, did not run; `keepalive report` lists them."""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ... import _dirs
from . import cli as _cli

WATCHDOG_TASK, WATCHDOG_INTERVAL_MIN = "sparrow-watchdog", 5
PROBE_TASK, PROBE_INTERVAL_MIN = "keepalive-probe", 15
TASK_MARKER = "<!-- Written by ag2-sparrow-hatch install; re-run install to update it. -->"
# Two missed watchdog runs, plus a minute of scheduler jitter.
WATCHDOG_STALE_S = 2 * WATCHDOG_INTERVAL_MIN * 60 + 60
GAP_MINUTES = 16
# How long a spawned Sparrow must stay up to count as started (75 = lock held elsewhere).
SPAWN_SETTLE_S = 3.0
LOG_NAME = "keepalive-probe.log"


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


def state_path(cfg: dict) -> Path:
    """Written by the watchdog (and install) only."""
    return _state_dir(cfg) / "hatch-keepalive.json"


def probe_state_path(cfg: dict) -> Path:
    """Written by the probe only, so neither overwrites the other's record."""
    return _state_dir(cfg) / "hatch-keepalive-probe.json"


def task_file(name: str, minutes: int) -> str:
    return f"{name}__interval@{minutes}m.md"


TASKS = ((WATCHDOG_TASK, WATCHDOG_INTERVAL_MIN, "watchdog",
          "It restarts the AG2 Space listener (Sparrow) only if it is down."),
         (PROBE_TASK, PROBE_INTERVAL_MIN, "probe",
          "It appends one line to the keepalive log. Do not restart anything yourself."))


def render_task(name: str, minutes: int, sub: str, what: str, command: str) -> str:
    return (f"{TASK_MARKER}\n# {name} (every {minutes} minutes)\n\n"
            f"Run this command once, then stop. {what} Change nothing else, retry nothing, "
            "and post nothing anywhere.\n\n"
            f"```sh\n{command} keepalive {sub}\n```\n")


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


def load_state(cfg: dict, path: Path | None = None) -> dict:
    try:
        state = json.loads((path or state_path(cfg)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(cfg: dict, state: dict, path: Path | None = None) -> None:
    _cli._write(path or state_path(cfg), json.dumps(state, sort_keys=True) + "\n", 0o600)


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _instance(cfg: dict) -> str:
    return (cfg["GATEWAY_INSTANCE"] if "GATEWAY_INSTANCE" in cfg
            else os.environ.get("GATEWAY_INSTANCE") or "").strip()


def listener_up(cfg: dict, now: float) -> bool:
    """Running, connected or not: a rejected token is not fixed by a restart."""
    code, _ = _cli.check_sparrow(_state_dir(cfg), now=now, instance=_instance(cfg))
    return code != _cli.SPARROW_DOWN


def watchdog_up(state: dict, now: float) -> bool:
    seen = max(_num(state.get("last_run")), _num(state.get("installed_at")))
    return now - seen <= WATCHDOG_STALE_S


def _status(cfg: dict, now: float) -> "tuple[str, bool, int]":
    state = load_state(cfg)
    wd_up = watchdog_up(state, now)
    total = int(_num(state.get("restart_total")))
    seen = int(_num(load_state(cfg, probe_state_path(cfg)).get("restart_total_seen")))
    # A smaller total means the watchdog's record was reset; count it from zero.
    count = total - seen if total >= seen else total
    line = (f"{_utc(now)} listener={'up' if listener_up(cfg, now) else 'down'} "
            f"watchdog={'up' if wd_up else 'down'} listener_restarts={count}")
    return line, wd_up, total


def status_line(cfg: dict, now: float | None = None) -> "tuple[str, bool]":
    """The probe's line for `now`, and whether the watchdog is up. Observes only."""
    line, wd_up, _total = _status(cfg, time.time() if now is None else now)
    return line, wd_up


def _install_task(directory: Path, name: str, minutes: int, text: str, force: bool) -> bool:
    """Write one scheduled task; a same-named task install did not write is kept unless
    `force`. False = kept, ours not installed."""
    target = directory / task_file(name, minutes)
    others = sorted(p for p in directory.glob(f"{name}__*.md") if p != target)
    if (directory / f"{name}.md").exists():
        others.append(directory / f"{name}.md")
    foreign = [p for p in [target, *others] if p.exists()
               and not p.read_text(encoding="utf-8", errors="replace").startswith(TASK_MARKER)]
    if foreign and not force:
        for p in foreign:
            print(f"NOTE keepalive: {p} was not written by install (a hand-made {name}?), so "
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


def install_tasks(cfg: dict, force: bool, now: float | None = None) -> None:
    """Install the watchdog and probe scheduled tasks. Never fails the install."""
    directory = cron_dir(cfg)
    if not directory.is_dir():
        print(f"NOTE keepalive: {directory} did not exist and was created; check that Hatch's "
              "scheduler runs the tasks in it")
    command = _command(cfg)
    installed = [name for name, minutes, sub, what in TASKS
                 if _install_task(directory, name, minutes,
                                  render_task(name, minutes, sub, what, command), force)]
    if WATCHDOG_TASK in installed:
        state = load_state(cfg)
        state["installed_at"] = time.time() if now is None else now
        _save_state(cfg, state)
    if installed:
        print(f"keepalive: {', '.join(installed)} scheduled; the probe log is {log_path(cfg)}")


def uninstall_tasks(cfg: dict) -> int:
    directory, rc = cron_dir(cfg), 0
    for name, _minutes, _sub, _what in TASKS:
        found = sorted(directory.glob(f"{name}__*.md")) + \
            ([directory / f"{name}.md"] if (directory / f"{name}.md").exists() else [])
        if not found:
            print(f"nothing to remove: no {name} task in {directory}")
        for p in found:
            if p.read_text(encoding="utf-8", errors="replace").startswith(TASK_MARKER):
                p.unlink()
                print(f"removed: {p}")
            else:
                print(f"error: {p} was not written by install; remove it yourself",
                      file=sys.stderr)
                rc = 1
    state_path(cfg).unlink(missing_ok=True)
    probe_state_path(cfg).unlink(missing_ok=True)
    print(f"The probe log {log_path(cfg)} was kept.")
    return rc


def _spawn_sparrow(cfg: dict) -> "tuple[bool, str]":
    exe = _cli._find_console_script(_cli.SPARROW_COMMAND)
    if not exe:
        return False, f"{_cli.SPARROW_COMMAND} not found beside {sys.executable} or on PATH"
    env = dict(os.environ, AG2_DEVICE_ENV=str(_cli.device_env_path()))
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


def watchdog(cfg: dict, now: float | None = None) -> int:
    now = time.time() if now is None else now
    state = load_state(cfg)
    state["last_run"] = now
    code, detail = _cli.check_sparrow(_state_dir(cfg), now=now, instance=_instance(cfg))
    rc = 0
    if code != _cli.SPARROW_DOWN:
        print(f"listener up: {detail}")
    else:
        ok, how = restart_listener(cfg)
        if ok:
            state["restart_total"] = int(_num(state.get("restart_total"))) + 1
            state["last_restart"] = now
            print(f"listener was down; restarted: {how}")
        else:
            print(f"listener is down and the restart failed: {how}", file=sys.stderr)
            rc = 1
    _save_state(cfg, state)
    return rc


def probe(cfg: dict, log: Path, now: float | None = None) -> int:
    line, wd_up, total = _status(cfg, time.time() if now is None else now)
    if not wd_up:
        directory = cron_dir(cfg)
        name, minutes, sub, what = TASKS[0]
        target = directory / task_file(name, minutes)
        before = target.stat().st_mtime if target.exists() else None
        if _install_task(directory, name, minutes,
                         render_task(name, minutes, sub, what, _command(cfg)), force=False):
            after = target.stat().st_mtime if target.exists() else None
            # _install_task returns True for an identical, untouched file too (cli.py's
            # old == text branch only chmods) — only a changed mtime is a real rewrite.
            line += " watchdog_restarted" if before != after else " watchdog_task_present"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    _save_state(cfg, {"restart_total_seen": total}, probe_state_path(cfg))
    print(line)
    return 0


def report(log: Path, gap_minutes: float = GAP_MINUTES) -> int:
    """Print the log's gaps over `gap_minutes` and its restart counts."""
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
        fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        rows.append((ts, fields, "watchdog_restarted" in parts[1:]))
    if not rows:
        print(f"{log}: no probe lines")
        return 0
    rows.sort(key=lambda r: r[0])
    print(f"{log}: {len(rows)} probe lines, {_utc(rows[0][0])} to {_utc(rows[-1][0])}"
          + (f" ({unreadable} unreadable lines skipped)" if unreadable else ""))
    gaps = [(a[0], b[0]) for a, b in zip(rows, rows[1:]) if b[0] - a[0] > gap_minutes * 60]
    print(f"gaps over {gap_minutes:g} min: {len(gaps)}"
          + (" (the scheduler or the VM did not run)" if gaps else ""))
    for start, end in gaps:
        print(f"  {_utc(start)} -> {_utc(end)}  {round((end - start) / 60)} min")
    restarts = 0
    for _ts, fields, _wr in rows:
        try:
            restarts += int(fields.get("listener_restarts", 0))
        except ValueError:
            pass
    print(f"listener down at a probe: {sum(f.get('listener') == 'down' for _, f, _ in rows)}")
    print(f"listener restarts by the watchdog: {restarts}")
    print(f"watchdog down at a probe: {sum(f.get('watchdog') == 'down' for _, f, _ in rows)}; "
          f"restarted by the probe: {sum(wr for _, _, wr in rows)}")
    return 0


def main(args) -> int:
    cfg = _cli._installed_config()
    if args.action == "watchdog":
        return watchdog(cfg)
    log = log_path(cfg, args.log)
    if args.action == "probe":
        return probe(cfg, log)
    if args.action == "status":
        print(status_line(cfg)[0])
        return 0
    return report(log, args.gap_minutes)
