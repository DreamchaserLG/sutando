#!/usr/bin/env python3
"""How a task reaches the Codex core: typed into its pane, or queued with `codex queue`.

`core.codex.delivery` (sutando_config.resolve_codex_delivery) picks the mode; send-keys is the
default. In queue mode the notifier resolves the core's thread once and hands the same prompt
text to `codex queue --thread <id> --message <text>` as one argv entry, never through a shell;
any failure here is the caller's cue to fall back to send-keys.

CLI (exit codes are the contract task-notifier.sh reads):
    codex_queue.py mode                                   -> prints send-keys | queue
    codex_queue.py thread --engine R --socket S --session N
        -> prints the thread id; 3 no core thread, 4 no persisted turn, 1 lookup failed
    codex_queue.py send --thread T --message M
        -> 0 queued; 5 certainly not queued (safe to type); 6 unknown (may be queued: never type)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NO_THREAD, NO_TURN, QUEUE_FAILED, QUEUE_UNKNOWN = 3, 4, 5, 6
ACK = "Queued message"
TIMEOUT_S = 30


def delivery(repo_root: Path | None = None) -> dict:
    sys.path.insert(0, str(REPO))
    from src.sutando_config import resolve_codex_delivery
    return resolve_codex_delivery(repo_root)


def queue_argv(thread: str, message: str) -> list[str]:
    return ["codex", "queue", "--thread", thread, "--message", message]


def observer_argv(engine: str, socket: str, session: str, node: str) -> list[str]:
    observer = str(Path(engine) / "src" / "agent" / "codex" / "cli" / "codex-observer.mjs")
    return [node, observer, "--core-thread", "--engine", engine, "--tmux-socket", socket, "--session", session]


def resolve_thread(engine: str, socket: str, session: str, run=subprocess.run) -> tuple[int, str]:
    """(0, id) or (code, reason). A configured core.codex.thread wins over discovery."""
    configured = delivery().get("thread", "")
    if configured:
        return 0, configured
    node = shutil.which("node")
    if not node:
        return 1, "node not on PATH; cannot read the core's rollout"
    try:
        r = run(observer_argv(engine, socket, session, node), capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"thread lookup failed: {exc}"
    if r.returncode == 0 and r.stdout.strip():
        return 0, r.stdout.strip()
    code = r.returncode if r.returncode in (NO_THREAD, NO_TURN) else 1
    return code, (r.stderr or r.stdout).strip() or f"thread lookup exited {r.returncode}"


def send(thread: str, message: str, run=subprocess.run) -> tuple[int, str]:
    if not thread or not message:
        return QUEUE_FAILED, "empty thread or message"
    try:
        r = run(queue_argv(thread, message), capture_output=True, text=True, timeout=TIMEOUT_S)
    except OSError as exc:
        return QUEUE_FAILED, f"codex queue did not start: {exc}"
    except subprocess.TimeoutExpired:
        return QUEUE_UNKNOWN, f"codex queue still running after {TIMEOUT_S}s; it may have queued"
    said = f"{r.stdout or ''}\n{r.stderr or ''}".strip()
    if r.returncode == 0:
        return 0, (r.stdout or "").strip()
    if ACK in said:
        return QUEUE_UNKNOWN, f"codex queue exited {r.returncode} after acknowledging: {said}"
    return QUEUE_FAILED, said or f"codex queue exited {r.returncode}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mode")
    t = sub.add_parser("thread")
    t.add_argument("--engine", default=str(REPO))
    t.add_argument("--socket", required=True)
    t.add_argument("--session", required=True)
    s = sub.add_parser("send")
    s.add_argument("--thread", required=True)
    s.add_argument("--message", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "mode":
        print(delivery()["delivery"], end="")
        return 0
    if args.cmd == "thread":
        code, out = resolve_thread(args.engine, args.socket, args.session)
    else:
        code, out = send(args.thread, args.message)
    print(out, end="", file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
